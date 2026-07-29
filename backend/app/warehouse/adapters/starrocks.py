"""StarRocks Adapter（M8）。

StarRocks 同走 MySQL 线协议，与 Doris 大体同构，但三处按官方文档核实后有别：

1. **Primary Key 模型**（非 Doris 的 Unique Key）：Delete+Insert 实现，实时 UPSERT/DELETE、
   支持部分列更新。有主键时用 ``PRIMARY KEY(...)``，Key 列须前导。
2. **声明式外键**：可写进 ``PROPERTIES("foreign_key_constraints"=...)`` 供优化器做 Join 改写
   （不强制完整性），故 ``foreign_key=DECLARATIVE``——这正是 StarRocks 与 Doris 的关键差异。
3. **标识符上限 1024**（非 64）：System_limit 文档明确表/列名可达 1024 字符。

无 SQL ``MERGE INTO``（UPSERT 走 Primary Key 表的 ``__op`` 导入），故 SCD2 拉链在渲染前报错。
能力矩阵已对照 StarRocks 官方文档逐项核实（``verified=True``）。
"""

from __future__ import annotations

from app.warehouse.adapters.base import DialectAdapter
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="starrocks",
    # Primary Key 模型：每个 Key 保留一条最新记录，写入强制唯一。
    # docs/table_design/table_types/primary_key_table/
    primary_key=ConstraintSupport.ENFORCED,
    # 可声明（非强制）供优化器 Join 改写：PROPERTIES("foreign_key_constraints"=...)。
    foreign_key=ConstraintSupport.DECLARATIVE,
    primary_key_model="primary_key",
    supports_table_comment=True,
    supports_column_comment=True,
    supports_partition=True,  # 表达式分区（v3.1+）/ Range / List
    supports_bucketing=True,  # DISTRIBUTED BY HASH(...)，BUCKETS 可省（v2.5.7+ 自动）
    # 无 SQL MERGE INTO；UPSERT 走 __op 导入，不等价 SCD2。
    supports_scd2_merge=False,
    supports_alter_add_column=True,
    supports_alter_drop_column=True,
    # RENAME COLUMN 需 v3.3.2+。
    supports_alter_rename_column=True,
    # 表/列名上限 1024 字符（System_limit）。
    max_identifier_length=1024,
    verified=True,
)


def _q(name: str) -> str:
    return f"`{name}`"


def _c(text: str) -> str:
    """StarRocks/MySQL 字符串字面量，双引号包裹并转义。"""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class StarRocksAdapter(DialectAdapter):
    name = "starrocks"

    def capabilities(self) -> Capabilities:
        return _CAPS

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → StarRocks 类型（与 Doris 同：无 TIMESTAMP，泛字符串落 VARCHAR）。"""
        dt = (data_type or "").lower()
        st = (semantic_type or "").lower()
        if st == "date" or dt == "date":
            return "DATE"
        if st in {"datetime", "time"} or "time" in dt or "date" in dt:
            return "DATETIME"
        if st == "amount" or dt in {"decimal", "numeric", "money"}:
            return "DECIMAL(18,4)"
        if st in {"number", "count"} or dt in {"int", "integer", "smallint"}:
            return "INT"
        if dt in {"bigint", "long"}:
            return "BIGINT"
        if dt in {"float", "double", "real"}:
            return "DOUBLE"
        if st == "flag" or dt in {"bool", "boolean"}:
            return "BOOLEAN"
        return "VARCHAR(1024)"

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_q(table.database)}.{_q(table.name)}"
        return _q(table.name)

    def _key_columns(self, table: LogicalTable) -> list[str]:
        pk = table.primary_key
        return [c for c in pk.columns if table.column(c)] if pk else []

    def _ordered_columns(self, table: LogicalTable) -> list[LogicalColumn]:
        """Key 列前导（Primary Key 模型硬约束），其余保持本体属性序。"""
        keys = self._key_columns(table)
        lead = [table.column(k) for k in keys]
        rest = [c for c in table.columns if c.name not in set(keys)]
        return [c for c in lead if c] + rest

    def _render_column(self, col: LogicalColumn) -> str:
        line = f"  {_q(col.name)} {self.map_type(col.data_type, col.semantic_type)}"
        if col.comment:
            line += f" COMMENT {_c(col.comment)}"
        return line

    def _partition_clause(self, table: LogicalTable) -> str | None:
        if not table.partition_key:
            return None
        col = table.column(table.partition_key)
        part_type = self.map_type(col.data_type, col.semantic_type) if col else ""
        # 表达式分区（v3.1+）：日期列按天自动建区；其余按列值分区。
        if part_type in {"DATE", "DATETIME"}:
            return f"PARTITION BY date_trunc('day', {_q(table.partition_key)})"
        return f"PARTITION BY ({_q(table.partition_key)})"

    def _foreign_key_property(self, table: LogicalTable) -> str | None:
        """外键 → 优化器约束声明（不强制完整性），供 Join 改写。"""
        parts: list[str] = []
        for fk in table.foreign_keys:
            if not fk.columns or not fk.ref_table:
                continue
            ref_cols = ", ".join(fk.ref_columns) if fk.ref_columns else fk.columns[0]
            cols = ", ".join(fk.columns)
            parts.append(f"({cols}) REFERENCES {fk.ref_table}({ref_cols})")
        return "; ".join(parts) if parts else None

    def render_create_table(self, table: LogicalTable) -> str:
        self.guard(table)
        columns = self._ordered_columns(table)
        keys = self._key_columns(table)
        lines = [
            f"CREATE TABLE IF NOT EXISTS {self._qualified(table)} (",
            ",\n".join(self._render_column(c) for c in columns),
            ")",
        ]
        if keys:
            lines.append(f"PRIMARY KEY({', '.join(_q(k) for k in keys)})")
        else:
            lines.append(f"DUPLICATE KEY({_q(columns[0].name)})")
        if table.comment:
            lines.append(f"COMMENT {_c(table.comment)}")
        partition = self._partition_clause(table)
        if partition:
            lines.append(partition)
        bucket = keys[0] if keys else columns[0].name
        lines.append(f"DISTRIBUTED BY HASH({_q(bucket)})")
        fk_prop = self._foreign_key_property(table)
        if fk_prop:
            lines.append(f'PROPERTIES (\n  "foreign_key_constraints" = "{fk_prop}"\n)')
        return "\n".join(lines) + ";"

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        self.guard(after)
        stmts: list[str] = []
        qualified = self._qualified(after)
        before_names = {c.name for c in before.columns}
        after_names = {c.name for c in after.columns}

        for col in after.columns:
            if col.name not in before_names:
                stmts.append(
                    f"ALTER TABLE {qualified} ADD COLUMN {self._render_column(col).strip()};"
                )
        for name in sorted(before_names - after_names):
            stmts.append(f"ALTER TABLE {qualified} DROP COLUMN {_q(name)};")
        for col in after.columns:
            old = before.column(col.name)
            if old is None:
                continue
            new_type = self.map_type(col.data_type, col.semantic_type)
            old_type = self.map_type(old.data_type, old.semantic_type)
            if new_type != old_type or (col.comment or "") != (old.comment or ""):
                comment = f" COMMENT {_c(col.comment)}" if col.comment else ""
                stmts.append(
                    f"ALTER TABLE {qualified} MODIFY COLUMN {_q(col.name)} {new_type}{comment};"
                )
        return stmts

    def translate_sql(self, sql: str) -> str:
        """StarRocks 兼容 MySQL 日期函数，原样返回。"""
        return sql
