"""引擎无关的逻辑表示（Logical Schema）。

本体是一级源数据、物理表是二级投影。生成器把「本体 + 物化契约」编译成这里的
LogicalSchema，再由 Dialect Adapter 渲染成各引擎的 DDL。

**约束**：本模块与各 Adapter 之外的代码都不得包含任何引擎特定逻辑——一旦
`if engine == "hive"` 出现在生成器里，多引擎同构就名存实亡了。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LogicalColumn:
    """一列。``comment`` 来自本体的 display_name / description——源库通常零注释，
    业务语义正是由本体反补到物理层的。"""

    name: str
    data_type: str | None = None
    semantic_type: str | None = None
    comment: str | None = None
    nullable: bool = True


@dataclass(frozen=True)
class LogicalConstraint:
    kind: str  # "primary_key" | "foreign_key"
    columns: tuple[str, ...] = ()
    ref_table: str | None = None
    ref_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogicalTable:
    name: str
    database: str | None = None
    layer: str = "dim"
    comment: str | None = None
    columns: tuple[LogicalColumn, ...] = ()
    constraints: tuple[LogicalConstraint, ...] = ()
    partition_key: str | None = None
    scd_type: str = "none"
    # 该表自身的装载方式（来自物化契约）。生成 ETL 时逐表生效，除非调用方给了全局覆盖。
    load_strategy: str = "full"
    # 承载该表的本体实体技术名。``name`` 是物理表名，可被人工改写（物化弹窗里指定表名），
    # 而回溯本体（取源表 / 字段映射）必须用实体名，故两者分开存。空 = 与 ``name`` 同。
    entity_name: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.name}" if self.database else self.name

    @property
    def source_name(self) -> str:
        """回溯本体实体时用的键（物理表名被改写后仍指得回原实体）。"""
        return self.entity_name or self.name

    def column(self, name: str) -> LogicalColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def primary_key(self) -> LogicalConstraint | None:
        return next((c for c in self.constraints if c.kind == "primary_key"), None)

    @property
    def foreign_keys(self) -> tuple[LogicalConstraint, ...]:
        return tuple(c for c in self.constraints if c.kind == "foreign_key")


@dataclass(frozen=True)
class LogicalSchema:
    ontology_id: str
    tables: tuple[LogicalTable, ...] = field(default_factory=tuple)

    def table(self, name: str) -> LogicalTable | None:
        return next((t for t in self.tables if t.name == name), None)
