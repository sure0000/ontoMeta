"""ClickHouse Adapter（M8，MergeTree 家族）。

三处与标准 SQL / Hive 迥异的语义，正是引擎逻辑必须下沉的原因：

1. **ORDER BY 键不是主键语义**——不去重、不唯一，只是稀疏排序索引，故主键只能是
   ``DECLARATIVE``（以 ORDER BY 表达），且**无 MERGE INTO**（UPSERT 靠 ReplacingMergeTree /
   mutations），SCD2 拉链做不到 → 契约要求时在渲染前报错。
2. **列默认非空**——可空列须显式 ``Nullable(T)`` 包裹；但 ORDER BY 键列不包 Nullable。
3. **无任意 TBLPROPERTIES**——只有表/列 COMMENT 可承载语义，故外键无处声明（``foreign_key=NONE``）；
   主外键血缘由权威副本 Hive 承载（单一写入路径）。

能力矩阵已对照 ClickHouse 官方文档核实（``verified=True``）；唯一未由文档确认的是标识符
长度上限（文档只给格式正则、无固定长度上限，取大值表示不设限）。
"""

from __future__ import annotations

import re

from app.warehouse.adapters.base import DialectAdapter, base_type
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="clickhouse",
    # PRIMARY KEY / ORDER BY 是稀疏索引，不强制唯一。
    primary_key=ConstraintSupport.DECLARATIVE,
    # 仅 CHECK 约束，无外键概念。
    foreign_key=ConstraintSupport.NONE,
    primary_key_model="none",
    supports_table_comment=True,  # ENGINE 后置 COMMENT '...'
    supports_column_comment=True,  # 列内联 COMMENT '...'
    supports_partition=True,  # PARTITION BY <expr>（MergeTree）
    supports_bucketing=False,  # 无 bucket 数概念（分布式靠 Distributed 引擎分片）
    # 无 MERGE INTO；UPSERT 靠 ReplacingMergeTree / mutations。
    supports_scd2_merge=False,
    supports_alter_add_column=True,
    supports_alter_drop_column=True,
    supports_alter_rename_column=True,
    # 无文档化的名称长度上限，取大值表示不设限。
    max_identifier_length=65535,
    verified=True,
)


def _q(name: str) -> str:
    return f"`{name}`"


def _c(text: str) -> str:
    """ClickHouse 字符串字面量，单引号包裹并转义。"""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class ClickHouseAdapter(DialectAdapter):
    name = "clickhouse"

    def capabilities(self) -> Capabilities:
        return _CAPS

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → ClickHouse 基础类型（不含 Nullable 包裹，包裹在渲染时决定）。"""
        # 去参数再判：INTEGER(11) / DECIMAL(21, 9) 这类原样类型精确查表命中不了，
        # 会被整体误判成文本列（见 base.base_type）。
        dt = base_type(data_type)
        st = (semantic_type or "").lower()
        if st == "date" or dt == "date":
            return "Date"
        if st in {"datetime", "time"} or "time" in dt or "date" in dt:
            return "DateTime64(3)"
        if st == "amount" or dt in {"decimal", "numeric", "money"}:
            return "Decimal(18, 4)"
        if st in {"number", "count"} or dt in {"int", "integer", "smallint"}:
            return "Int32"
        if dt in {"bigint", "long"}:
            return "Int64"
        if dt in {"float", "double", "real"}:
            return "Float64"
        if st == "flag" or dt in {"bool", "boolean"}:
            return "Bool"
        return "String"

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_q(table.database)}.{_q(table.name)}"
        return _q(table.name)

    def _order_by(self, table: LogicalTable) -> list[str]:
        """排序键取主键列（存在且在列清单中）；否则空 → tuple()。"""
        pk = table.primary_key
        return [c for c in pk.columns if table.column(c)] if pk else []

    def _column_type(self, col: LogicalColumn, *, key_cols: set[str]) -> str:
        base = self.map_type(col.data_type, col.semantic_type)
        # 排序键列不可空；其余按 nullable 包裹 Nullable(T)。
        if col.nullable and col.name not in key_cols:
            return f"Nullable({base})"
        return base

    def _render_column(self, col: LogicalColumn, *, key_cols: set[str]) -> str:
        line = f"  {_q(col.name)} {self._column_type(col, key_cols=key_cols)}"
        if col.comment:
            line += f" COMMENT {_c(col.comment)}"
        return line

    def _partition_clause(self, table: LogicalTable) -> str | None:
        if not table.partition_key:
            return None
        col = table.column(table.partition_key)
        base = self.map_type(col.data_type, col.semantic_type) if col else ""
        # 日期/时间列按月分区（避免每天一个分区爆炸）；其余按列值。
        if base in {"Date", "DateTime64(3)"}:
            return f"PARTITION BY toYYYYMM({_q(table.partition_key)})"
        return f"PARTITION BY {_q(table.partition_key)}"

    def render_create_table(self, table: LogicalTable) -> str:
        self.guard(table)
        keys = self._order_by(table)
        key_set = set(keys)
        lines = [
            f"CREATE TABLE IF NOT EXISTS {self._qualified(table)} (",
            ",\n".join(self._render_column(c, key_cols=key_set) for c in table.columns),
            ")",
            "ENGINE = MergeTree",
        ]
        partition = self._partition_clause(table)
        if partition:
            lines.append(partition)
        order = ", ".join(_q(k) for k in keys) if keys else ""
        lines.append(f"ORDER BY ({order})")
        if table.comment:
            lines.append(f"COMMENT {_c(table.comment)}")
        return "\n".join(lines) + ";"

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        self.guard(after)
        stmts: list[str] = []
        qualified = self._qualified(after)
        key_set = set(self._order_by(after))
        before_names = {c.name for c in before.columns}
        after_names = {c.name for c in after.columns}

        for col in after.columns:
            if col.name not in before_names:
                stmts.append(
                    f"ALTER TABLE {qualified} ADD COLUMN "
                    f"{self._render_column(col, key_cols=key_set).strip()};"
                )
        for name in sorted(before_names - after_names):
            stmts.append(f"ALTER TABLE {qualified} DROP COLUMN {_q(name)};")
        for col in after.columns:
            old = before.column(col.name)
            if old is None:
                continue
            new_type = self._column_type(col, key_cols=key_set)
            old_type = self._column_type(old, key_cols=key_set)
            if new_type != old_type:
                stmts.append(
                    f"ALTER TABLE {qualified} MODIFY COLUMN {_q(col.name)} {new_type};"
                )
            if (col.comment or "") != (old.comment or "") and col.comment:
                stmts.append(
                    f"ALTER TABLE {qualified} COMMENT COLUMN {_q(col.name)} {_c(col.comment)};"
                )
        return stmts

    def translate_sql(self, sql: str) -> str:
        """ClickHouse 日期方言：CURDATE()→today()，DATE_SUB(CURDATE(), INTERVAL n DAY)→today() - n。"""
        sql = re.sub(
            r"DATE_SUB\(\s*CURDATE\(\)\s*,\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
            r"today() - \1",
            sql,
            flags=re.IGNORECASE,
        )
        return re.sub(r"CURDATE\(\)", "today()", sql, flags=re.IGNORECASE)

    def render_load(self, target: str, select_body: str, *, overwrite: bool) -> str:
        """ClickHouse 同样没有 ``INSERT OVERWRITE``：覆盖装载 = ``TRUNCATE`` + ``INSERT``。

        ⚠ 与建表能力矩阵同理，未在真实实例逐项核实（``verified=False``）。
        """
        insert = f"INSERT INTO {target}\n{select_body};"
        return f"TRUNCATE TABLE {target};\n{insert}" if overwrite else insert

    def render_create_staging(self, table: LogicalTable, run_id: str) -> str:
        """ClickHouse 无 ``CREATE TABLE LIKE``；``CREATE TABLE ... AS <orig>`` 复制结构建空表。"""
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return f"CREATE TABLE IF NOT EXISTS {stg} AS {orig};"

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """ClickHouse 原子切换：``EXCHANGE TABLES a AND b``（原子交换），再删 staging。

        ⚠ 需 Atomic 数据库引擎（默认）；原子性/代价需真实实例核实（§8.3）。
        """
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return [
            f"EXCHANGE TABLES {orig} AND {stg};",
            f"DROP TABLE IF EXISTS {stg};",
        ]
