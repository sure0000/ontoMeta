"""Apache Doris Adapter（M8）。

Doris 走 MySQL 线协议，与 Hive 有三处关键语义差异，正是引擎逻辑必须下沉到 Adapter 的原因：

1. **分区列留在列清单里**——与 Hive 相反，分区键既是普通列又参与 ``PARTITION BY``。
2. **表模型决定去重语义**——有主键时用 **Unique Key** 模型（写入去重、保留最新），
   否则用 **Duplicate Key**（不去重）。Key 列必须是**前导列**，故渲染时把主键列排到最前。
3. **无 MERGE INTO（目标版本 < 4.1）**——SCD2 拉链做不到，契约若要求会在渲染前报错，
   不静默降级；Unique Key 的 UPSERT 不等价于 SCD2。

能力矩阵已对照 Doris 3.x 官方文档逐项核实（``verified=True``），依据见各字段注释。
一处**有意不声明**：Doris 原生表的「声明式外键（供优化器）」在官方文档未找到可引用的
建表约束页，故 ``foreign_key=NONE``——不臆造未核实的能力。
"""

from __future__ import annotations

from app.warehouse.adapters.base import DialectAdapter, base_type
from app.warehouse.capabilities import Capabilities, ConstraintSupport
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

_CAPS = Capabilities(
    engine="doris",
    # Unique Key 模型写入去重、保留最新（Merge-on-Write，2.1 起默认）。
    # docs/3.x/table-design/data-model/unique/
    primary_key=ConstraintSupport.ENFORCED,
    # 原生表声明式外键（供优化器）文档未核实 → 不声明，取 NONE。
    foreign_key=ConstraintSupport.NONE,
    primary_key_model="unique",
    supports_table_comment=True,  # COMMENT "..."（CREATE-TABLE 参考）
    supports_column_comment=True,
    supports_partition=True,  # PARTITION BY RANGE/LIST + AUTO 分区
    supports_bucketing=True,  # DISTRIBUTED BY HASH(...) BUCKETS n（必填）
    # MERGE INTO 到 4.1.0 才有且目标须 Unique Key 表；目标版本未定，保守取 False。
    supports_scd2_merge=False,
    supports_alter_add_column=True,
    supports_alter_drop_column=True,
    # 值列增删/改名走 light schema change（2.0 起默认开）；改名 KEY 列为重操作。
    supports_alter_rename_column=True,
    # 表名上限 64 字节（列名可到 256，取表名上限作保守闸门）。
    max_identifier_length=64,
    verified=True,
)

# PoC 分桶数；生产应按数据量与 BE 数调整。
_BUCKETS = 10

# 副本数上限。Doris 的推荐生产值就是 3（FE 的 default_replication_num 默认值），
# 再多不会提高可用性，只是多占存储。
_MAX_REPLICATION = 3


def _q(name: str) -> str:
    return f"`{name}`"


def _c(text: str) -> str:
    """Doris/MySQL 字符串字面量，双引号包裹并转义。"""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class DorisAdapter(DialectAdapter):
    name = "doris"

    def __init__(self, replication_num: int | None = None) -> None:
        #: 建表要写死的副本数。None = 不写，沿用 FE 的 default_replication_num。
        #: 注册表里那个实例是共享单例，故这个值只能由 ``for_storage_nodes`` 产出的
        #: **副本**持有，绝不在共享实例上就地改。
        self._replication_num = replication_num

    def capabilities(self) -> Capabilities:
        return _CAPS

    def for_storage_nodes(self, count: int | None) -> DorisAdapter:
        """实测 BE 数 → 建表的 ``replication_num``（封顶 3）。

        Doris 要求副本数 ≤ 存活 BE 数，否则建表直接被 FE 拒（errCode 2）。单 BE 的开发
        实例上，不写这个属性就等于每条建表语句都跑不了。读不到 BE 数（count=None）时
        不写属性——沿用引擎默认，而不是拿一个猜的数字去建表。
        """
        if not isinstance(count, int) or count < 1:
            return self
        return DorisAdapter(replication_num=min(count, _MAX_REPLICATION))

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → Doris 类型。判定顺序与 hive.py 一致（语义优先于物理类型）。

        注意 Doris **没有 TIMESTAMP 类型**，日期时间落 DATETIME；泛字符串落
        VARCHAR 而非 STRING——STRING 不能作 Key/分区列，会让主键模型建表失败。
        """
        # 去参数再判：INTEGER(11) / DECIMAL(21, 9) 这类原样类型精确查表命中不了，
        # 会被整体误判成文本列（见 base.base_type）。
        dt = base_type(data_type)
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
        # 无长度元数据时的保守默认；宽文本需在本体显式标注类型。
        return "VARCHAR(1024)"

    def _qualified(self, table: LogicalTable) -> str:
        if table.database:
            return f"{_q(table.database)}.{_q(table.name)}"
        return _q(table.name)

    def _key_columns(self, table: LogicalTable) -> list[str]:
        pk = table.primary_key
        return [c for c in pk.columns if table.column(c)] if pk else []

    def _ordered_columns(self, table: LogicalTable) -> list[LogicalColumn]:
        """Key 列必须前导（Doris 表模型硬约束），其余保持本体属性序。"""
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
        # 日期分区用 AUTO RANGE + date_trunc（按天）；其余用 AUTO LIST——
        # 二者都允许空分区列表，避免臆造具体分区边界。
        if part_type in {"DATE", "DATETIME"}:
            return f"AUTO PARTITION BY RANGE (date_trunc({_q(table.partition_key)}, 'day')) ()"
        return f"AUTO PARTITION BY LIST ({_q(table.partition_key)}) ()"

    def render_create_table(self, table: LogicalTable) -> str:
        return self._create_table_body(table) + self._properties_clause([]) + ";"

    def _create_table_body(self, table: LogicalTable) -> str:
        """建表语句的主体（到 ``DISTRIBUTED BY`` 为止，无 PROPERTIES、无分号）。

        ``render_ingestion_table`` 还要往同一条语句上并入 Merge-on-Write / sequence 列，
        而 Doris 只接受**一个** PROPERTIES 块——故主体与属性分开渲染，由调用方合并一次。
        """
        self.guard(table)
        columns = self._ordered_columns(table)
        keys = self._key_columns(table)
        # Doris requires at least one physical column.  An empty ontology object
        # is still materializable as a schema placeholder; later schema change
        # can replace it once properties are confirmed.
        if not columns:
            from app.warehouse.logical_schema import LogicalColumn
            columns = [LogicalColumn(name="__ontometa_placeholder", data_type="varchar")]
        lines = [
            f"CREATE TABLE IF NOT EXISTS {self._qualified(table)} (",
            ",\n".join(self._render_column(c) for c in columns),
            ")",
        ]
        if keys:
            lines.append(f"UNIQUE KEY({', '.join(_q(k) for k in keys)})")
        else:
            # 无主键 → Duplicate 模型，以首列作 Key（不去重）。
            lines.append(f"DUPLICATE KEY({_q(columns[0].name)})")
        if table.comment:
            lines.append(f"COMMENT {_c(table.comment)}")
        partition = self._partition_clause(table)
        if partition:
            lines.append(partition)
        bucket = keys[0] if keys else columns[0].name
        lines.append(f"DISTRIBUTED BY HASH({_q(bucket)}) BUCKETS {_BUCKETS}")
        return "\n".join(lines)

    def _properties_clause(self, props: list[tuple[str, str]]) -> str:
        """``PROPERTIES (...)`` 片段（不带结尾分号）；没有任何属性时返回空串。

        副本数在这里统一并进来：它对**每一张** Doris 表都成立（建表、ODS 摄取表都要），
        由 ``for_storage_nodes`` 按目标实例的实测 BE 数定下。
        """
        merged = list(props)
        if self._replication_num is not None and not any(
            key == "replication_num" for key, _ in merged
        ):
            merged.append(("replication_num", str(self._replication_num)))
        if not merged:
            return ""
        rendered = ",\n  ".join(f'"{key}" = "{value}"' for key, value in merged)
        return f"\nPROPERTIES (\n  {rendered}\n)"

    def render_ingestion_table(
        self, table: LogicalTable, *, sequence_column: str | None = None
    ) -> str:
        """Render a Doris ODS table for Flink ingestion.

        Unique-key ODS tables explicitly enable Merge-on-Write. CDC sequence
        ordering is a physical Doris property and therefore belongs here, not
        in Flink SQL or an LLM-produced Spec.
        """
        keys = self._key_columns(table)
        if sequence_column and table.column(sequence_column) is None:
            raise ValueError(f"sequence column {sequence_column!r} 不在 ODS 列清单中")
        base = self._create_table_body(table)
        props: list[tuple[str, str]] = []
        if keys:
            props.append(("enable_unique_key_merge_on_write", "true"))
        if sequence_column:
            if not keys:
                raise ValueError("sequence column 只能用于 Doris Unique Key ODS 表")
            props.append(("function_column.sequence_col", sequence_column))
        return base + self._properties_clause(props) + ";"

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER。Doris 支持逐列增/删/改（值列为 light schema change）。"""
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
        """Doris 兼容 MySQL 日期函数（CURDATE / DATE_SUB(..., INTERVAL n DAY)），原样返回。"""
        return sql

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """Doris 原子切换：``ALTER TABLE ... REPLACE WITH TABLE``（单语句、原子）。

        ``swap="false"`` 表示替换后**丢弃** staging（否则会把旧数据换到 staging 名下）。
        docs/3.x：Alter/replace-table。

        **第二张表只能写裸表名**：Doris 的语法是
        ``ALTER TABLE [库.]目标 REPLACE WITH TABLE staging``，staging 隐含同库。带上库
        前缀会被 FE 的解析器拒掉——``ParseException: no viable alternative at input
        'ALTER TABLE `库`.`表` REPLACE'``，错误位置指在 REPLACE 上，看着像不支持这条语句，
        其实只是多写了个库名（2.1.0 实测）。
        """
        stg = _q(self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return [f'ALTER TABLE {orig} REPLACE WITH TABLE {stg} PROPERTIES("swap" = "false");']
