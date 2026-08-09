"""Apache Iceberg Adapter（M8，Spark SQL 方言）。

Iceberg 的价值在于**原生 schema evolution**（增/删/改名靠稳定 field-id，均安全无需重写数据）
与 **MERGE INTO**——这直接缓解「一次本体变更放大成 N 个引擎迁移」的风险，是唯一支持
SCD2 拉链的目标引擎。

与 Hive 的两处差异：
1. **分区列留在 schema 里**——``PARTITIONED BY`` 用 identity 变换引用列，列不从清单剔除。
2. **建表语法为 Spark ``USING iceberg``**，类型用 Spark 名（INT/BIGINT/STRING/...）。

主外键 Iceberg 不强制，但用 ``TBLPROPERTIES`` 声明式记录（与 Hive 同理，供 DataHub 回采）
——这是有记录的声明，不是假装能约束。能力矩阵已对照 Iceberg + Spark 官方文档核实
（``verified=True``）；唯一未由文档确认的是标识符长度上限（Iceberg 无文档化上限，取大值表示不设限）。
"""

from __future__ import annotations

import re

from app.warehouse.adapters.base import DialectAdapter, base_type
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="iceberg",
    # 不强制，用 identifier fields / 表属性声明。
    primary_key=ConstraintSupport.DECLARATIVE,
    foreign_key=ConstraintSupport.DECLARATIVE,
    primary_key_model="none",
    supports_table_comment=True,  # 表级 + 列级 COMMENT（Spark DDL）
    supports_column_comment=True,
    supports_partition=True,  # PARTITIONED BY（identity/bucket/truncate/时间变换）
    supports_bucketing=True,  # bucket(N, col) 分区变换
    # MERGE INTO（Spark）+ 行级删除，SCD2 可行。
    supports_scd2_merge=True,
    supports_alter_add_column=True,
    supports_alter_drop_column=True,
    supports_alter_rename_column=True,  # field-id 稳定，改名安全
    # Iceberg 无文档化的名称长度上限，取大值表示不设限。
    max_identifier_length=65535,
    verified=True,
)


def _q(name: str) -> str:
    return f"`{name}`"


def _c(text: str) -> str:
    """Spark 字符串字面量，单引号包裹并转义。"""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class IcebergAdapter(DialectAdapter):
    name = "iceberg"

    def capabilities(self) -> Capabilities:
        return _CAPS

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → Spark/Iceberg 类型。"""
        # 去参数再判：INTEGER(11) / DECIMAL(21, 9) 这类原样类型精确查表命中不了，
        # 会被整体误判成文本列（见 base.base_type）。
        dt = base_type(data_type)
        st = (semantic_type or "").lower()
        if st == "date" or dt == "date":
            return "DATE"
        if st in {"datetime", "time"} or "time" in dt or "date" in dt:
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

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_q(table.database)}.{_q(table.name)}"
        return _q(table.name)

    def _render_column(self, col: LogicalColumn) -> str:
        line = f"  {_q(col.name)} {self.map_type(col.data_type, col.semantic_type)}"
        if col.comment:
            line += f" COMMENT {_c(col.comment)}"
        return line

    def _table_properties(self, table: LogicalTable) -> dict[str, str]:
        """主外键不强制，以表属性声明式记录（与 Hive 同理，供 DataHub 回采）。"""
        props: dict[str, str] = {
            "ontometa.layer": table.layer,
            # v2 支持行级删除与 MERGE。
            "format-version": "2",
        }
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

    def render_create_table(self, table: LogicalTable) -> str:
        self.guard(table)
        # Iceberg 分区列不从列清单剔除（与 Hive 相反）。
        lines = [
            f"CREATE TABLE IF NOT EXISTS {self._qualified(table)} (",
            ",\n".join(self._render_column(c) for c in table.columns),
            ")",
            "USING iceberg",
        ]
        if table.partition_key:
            lines.append(f"PARTITIONED BY ({_q(table.partition_key)})")
        if table.comment:
            lines.append(f"COMMENT {_c(table.comment)}")
        props = self._table_properties(table)
        rendered = ",\n".join(
            f"  {_c(k)}={_c(v)}" for k, v in sorted(props.items())
        )
        lines.append(f"TBLPROPERTIES (\n{rendered}\n)")
        return "\n".join(lines) + ";"

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER。Iceberg 原生支持增/删/改列，无需重建。"""
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
            if new_type != old_type:
                stmts.append(
                    f"ALTER TABLE {qualified} ALTER COLUMN {_q(col.name)} TYPE {new_type};"
                )
            if (col.comment or "") != (old.comment or "") and col.comment:
                stmts.append(
                    f"ALTER TABLE {qualified} ALTER COLUMN {_q(col.name)} COMMENT {_c(col.comment)};"
                )
        return stmts

    def translate_sql(self, sql: str) -> str:
        """Spark SQL 方言：与 Hive 同族（current_date() / date_sub）。"""
        sql = re.sub(
            r"DATE_SUB\(\s*CURDATE\(\)\s*,\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
            r"date_sub(current_date(), \1)",
            sql,
            flags=re.IGNORECASE,
        )
        return re.sub(r"CURDATE\(\)", "current_date()", sql, flags=re.IGNORECASE)
