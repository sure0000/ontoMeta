"""Dialect Adapter 抽象基类。

引擎特定逻辑**只允许**存在于本包的各 Adapter 实现中。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.warehouse.capabilities import (
    Capabilities,
    CapabilityError,
    CapabilityGap,
    check_table,
)
from app.warehouse.logical_schema import LogicalTable


class DialectAdapter(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """本引擎能表达什么。未核实的条目须置 ``verified=False``。"""

    @abstractmethod
    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → 引擎原生类型。"""

    @abstractmethod
    def render_create_table(self, table: LogicalTable) -> str:
        """渲染建表语句。实现须先调用 ``self.guard(table)``。"""

    @abstractmethod
    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER 语句；无法用 ALTER 表达时返回重建指引。"""

    def translate_sql(self, sql: str) -> str:
        """SQL 方言翻译。默认原样返回。"""
        return sql

    def quote_identifier(self, name: str) -> str:
        """引擎标识符引用规则。默认反引号；ANSI 双引号引擎须覆写。

        引号规则是引擎特定逻辑，必须住在 Adapter 里——生成器只能委托本方法，
        绝不允许自己判 ``if engine == ...``（遗留2 下沉的正是此处）。
        """
        return f"`{name}`"

    # ---------- 能力校验 ----------

    def check(self, table: LogicalTable) -> list[CapabilityGap]:
        """返回全部缺口（含 warning）。Validation Gate 调这个做前置校验。"""
        return check_table(table, self.capabilities())

    def guard(self, table: LogicalTable) -> list[CapabilityGap]:
        """error 级缺口直接抛出；warning 级返回给调用方呈现，**不得吞掉**。"""
        gaps = self.check(table)
        errors = [g for g in gaps if g.is_error]
        if errors:
            raise CapabilityError(self.name, errors)
        return gaps


class UnimplementedAdapter(DialectAdapter):
    """只声明能力、尚未实现渲染的占位 Adapter（M8 补齐）。

    刻意保留 ``capabilities()``：即便还不能渲染，Validation Gate 也能提前告诉用户
    「这个引擎表达不了你要的东西」，而不是等到实现完才发现。
    """

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")

    def render_create_table(self, table: LogicalTable) -> str:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")
