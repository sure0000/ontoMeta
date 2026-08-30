"""Hive Adapter（M2 唯一实现）。

Hive 是权威写入路径，其余引擎从它派生（单一写入路径，避免多副本双写不一致）。

两个 Hive 特有语义，正是引擎逻辑必须下沉到 Adapter 的原因：

1. **分区列不能出现在列清单里**——必须从 columns 中剔除并单独放进 PARTITIONED BY。
2. **没有真外键**——主外键以 ``TBLPROPERTIES`` 声明式记录（``ConstraintSupport.DECLARATIVE``），
   供 DataHub 回采成血缘，而不是假装 Hive 能约束。

PoC 阶段按非事务 ORC 外部表规划（Hive 3.1.3 + Spark 3.2 的 ACID 兼容限制），
因此 ``supports_scd2_merge=False``——契约若要求 SCD2 会在渲染前报错，不静默降级。
"""

from __future__ import annotations

import re

from app.warehouse.adapters.base import DialectAdapter, base_type
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="hive",
    primary_key=ConstraintSupport.DECLARATIVE,
    foreign_key=ConstraintSupport.DECLARATIVE,
    primary_key_model="none",
    supports_table_comment=True,
    supports_column_comment=True,
    supports_partition=True,
    supports_bucketing=True,
    # 非事务 ORC 外部表不支持 MERGE；启用 ACID 表后可放开。
    supports_scd2_merge=False,
    supports_alter_add_column=True,
    supports_alter_drop_column=False,  # Hive 只能 REPLACE COLUMNS，语义不等价
    supports_alter_rename_column=True,
    max_identifier_length=128,
    verified=True,
)


def _quote(identifier: str) -> str:
    return f"`{identifier}`"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


class HiveAdapter(DialectAdapter):
    name = "hive"

    def capabilities(self) -> Capabilities:
        return _CAPS

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → Hive 类型。

        判定顺序遵循本体语义优先于物理类型，并落到 Hive 原生类型。
        """
        # 去参数再判：INTEGER(11) / DECIMAL(21, 9) 这类原样类型精确查表命中不了，
        # 会被整体误判成文本列（见 base.base_type）。
        dt = base_type(data_type)
        st = (semantic_type or "").lower()
        if st in {"date", "datetime", "time"} or "date" in dt or "time" in dt:
            return "TIMESTAMP"
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
        return "STRING"

    def _render_column(self, col: LogicalColumn) -> str:
        line = f"  {_quote(col.name)} {self.map_type(col.data_type, col.semantic_type)}"
        if col.comment:
            line += f" COMMENT '{_escape(col.comment)}'"
        return line

    def render_create_table(self, table: LogicalTable) -> str:
        self.guard(table)

        # Hive：分区列不在列清单中，单独声明。
        body_cols = [c for c in table.columns if c.name != table.partition_key]
        lines = [
            f"CREATE EXTERNAL TABLE IF NOT EXISTS {self._qualified(table)} (",
            ",\n".join(self._render_column(c) for c in body_cols),
            ")",
        ]
        if table.comment:
            lines.append(f"COMMENT '{_escape(table.comment)}'")
        if table.partition_key:
            part = table.column(table.partition_key)
            part_type = (
                self.map_type(part.data_type, part.semantic_type) if part else "STRING"
            )
            part_comment = (
                f" COMMENT '{_escape(part.comment)}'" if part and part.comment else ""
            )
            lines.append(
                f"PARTITIONED BY ({_quote(table.partition_key)} {part_type}{part_comment})"
            )
        lines.append("STORED AS ORC")

        props = self._table_properties(table)
        if props:
            rendered = ",\n".join(
                f"  '{k}'='{_escape(v)}'" for k, v in sorted(props.items())
            )
            lines.append(f"TBLPROPERTIES (\n{rendered}\n)")
        return "\n".join(lines) + ";"

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_quote(table.database)}.{_quote(table.name)}"
        return _quote(table.name)

    def _table_properties(self, table: LogicalTable) -> dict[str, str]:
        """主外键在 Hive 无法强制，以 TBLPROPERTIES 声明式记录，供 DataHub 回采。"""
        props: dict[str, str] = {"ontometa.layer": table.layer}
        pk = table.primary_key
        if pk and pk.columns:
            props["ontometa.primary_key"] = ",".join(pk.columns)
        for fk in table.foreign_keys:
            if not fk.columns or not fk.ref_table:
                continue
            key = f"ontometa.foreign_key.{'_'.join(fk.columns)}"
            ref_cols = ",".join(fk.ref_columns) if fk.ref_columns else ""
            props[key] = f"{fk.ref_table}({ref_cols})" if ref_cols else fk.ref_table
        if table.scd_type != "none":
            props["ontometa.scd_type"] = table.scd_type
        return props

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER。

        Hive 无法逐列 DROP（只有语义不等价的 REPLACE COLUMNS），因此删列返回
        重建指引而不是硬凑一条 ALTER——物理表本就是本体的投影，重建是正当手段。
        """
        self.guard(after)
        stmts: list[str] = []
        qualified = self._qualified(after)
        before_names = {c.name for c in before.columns}
        after_names = {c.name for c in after.columns}

        for col in after.columns:
            if col.name in before_names or col.name == after.partition_key:
                continue
            stmts.append(
                f"ALTER TABLE {qualified} ADD COLUMNS ({self._render_column(col).strip()});"
            )

        dropped = sorted(before_names - after_names)
        if dropped:
            stmts.append(
                f"-- 需重建：Hive 不支持逐列 DROP（缺失列：{', '.join(dropped)}）。"
                f"物理表是本体的投影，按新本体重建即可。"
            )

        for col in after.columns:
            old = before.column(col.name)
            if old is None or col.name == after.partition_key:
                continue
            new_type = self.map_type(col.data_type, col.semantic_type)
            old_type = self.map_type(old.data_type, old.semantic_type)
            if new_type != old_type or (col.comment or "") != (old.comment or ""):
                comment = f" COMMENT '{_escape(col.comment)}'" if col.comment else ""
                stmts.append(
                    f"ALTER TABLE {qualified} CHANGE COLUMN {_quote(col.name)} "
                    f"{_quote(col.name)} {new_type}{comment};"
                )
        return stmts

    def translate_sql(self, sql: str) -> str:
        """最小方言翻译，范围比照 ``services/data_app_executor.py::_translate_dialect``。"""
        sql = re.sub(
            r"DATE_SUB\(\s*CURDATE\(\)\s*,\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
            r"date_sub(current_date(), \1)",
            sql,
            flags=re.IGNORECASE,
        )
        return re.sub(r"CURDATE\(\)", "current_date()", sql, flags=re.IGNORECASE)

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """Hive 无原子 rename-swap；用 ``INSERT OVERWRITE`` 目录级替换全表数据，再删 staging。

        列序与正式表一致（staging 由 ``CREATE TABLE LIKE`` 建），故 ``SELECT *`` 可直接对位。
        ⚠ 分区表的按分区 OVERWRITE 与原子性需真实实例核实（§8.3）；非分区表 OVERWRITE
        在 FS 目录级替换，failure 语义与事务库不同。
        """
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return [
            f"INSERT OVERWRITE TABLE {orig} SELECT * FROM {stg};",
            f"DROP TABLE IF EXISTS {stg};",
        ]
