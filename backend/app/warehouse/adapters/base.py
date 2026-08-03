"""Dialect Adapter 抽象基类。

引擎特定逻辑**只允许**存在于本包的各 Adapter 实现中。
"""

from __future__ import annotations

import re
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

    # ---------- 全量落地：staging + 原子切换（M15） ----------
    #
    # 现状的全量装载是先删后写（SeaTunnel DROP_DATA），搬到一半失败=正式表被清空、
    # 原数据没了（§1.2 最贵的一种不稳定）。M15 改成：搬进一张 staging 表 → 校验 →
    # **原子切换**到正式表，失败时正式表原封不动。切换语法各引擎不同，故落在 Adapter
    # 里（与建表 DDL 同处），**不进 runner**（runner 只把数据写进 staging）。
    #
    # ⚠ 需实施前验证（MATERIALIZE_SYNC_STABILITY.md §8.3）：各引擎切换的原子性与代价
    # 需在真实实例上核实——本层只保证生成的语句形状正确、可 golden，不代表已在目标库跑通。

    def _sanitize_run(self, run_id: str) -> str:
        """run_id → 可作表名后缀的 token。Airflow run_id 常含冒号/加号/减号，须清洗。"""
        return re.sub(r"[^0-9A-Za-z]+", "_", run_id or "").strip("_") or "run"

    def staging_table_name(self, table: LogicalTable, run_id: str) -> str:
        """staging 表裸名 ``<表>__stg_<run>``。带 run_id：重跑/补数不撞表（§3.3 幂等）。"""
        return f"{table.name}__stg_{self._sanitize_run(run_id)}"

    def _qual(self, database: str | None, name: str) -> str:
        """``库.表`` 或 ``表``，按本引擎的引用规则加引号。"""
        q = self.quote_identifier
        return f"{q(database)}.{q(name)}" if database else q(name)

    def render_create_staging(self, table: LogicalTable, run_id: str) -> str:
        """建一张与正式表同构的空 staging 表。默认 ``CREATE TABLE ... LIKE``。"""
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return f"CREATE TABLE IF NOT EXISTS {stg} LIKE {orig};"

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """把 staging 原子切换成正式表。

        默认实现是 **rename 两步**（正式→old，staging→正式，再删 old）：正式表被改名走后、
        staging 改名前有一个**短暂窗口**，不是真正原子——这是没有原生原子切换的引擎的下限。
        有 ``REPLACE`` / ``SWAP`` / ``EXCHANGE`` 的引擎覆写本方法换成单语句原子操作。
        """
        run = self._sanitize_run(run_id)
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        old = self._qual(table.database, f"{table.name}__old_{run}")
        return [
            f"DROP TABLE IF EXISTS {old};",
            f"ALTER TABLE {orig} RENAME TO {old};",
            f"ALTER TABLE {stg} RENAME TO {orig};",
            f"DROP TABLE IF EXISTS {old};",
        ]

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
