"""搬运工具的选择策略——统一执行架构下已无选择，一律 Flink SQL on YARN。

**历史**：此前有 runner/docker 多通道，docker 下按 SeaTunnel/DataX 能力 ∩ 镜像可用性
自动挑工具。统一执行架构废除多通道，搬运 = Flink SQL（与 transform/metric 同一执行
路径），不再有工具选择这回事。

**为什么保留这个文件**：`resolve_sync_tool` 与 `engine_modes` 仍被 preflight /
物化 API / Data Agent 调用（它们要问「本次用什么搬」「这个引擎支持哪些装载方式」）。
统一架构下答案恒定，但调用方暂未重构，故保留本模块作适配层，返回 Flink 的恒定答案。

后续可直接内联到调用处，删除本文件（见 H/I 阶段）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class SyncToolResolutionError(RuntimeError):
    """选不出可用的搬运工具（统一架构下已不会抛，为兼容保留）。"""


@dataclass(frozen=True)
class SyncToolChoice:
    """本次物化实际会用什么搬——统一架构下恒为 flink_on_yarn。"""

    channel: str  # 已废弃，固定 "flink_on_yarn"
    tool: str | None  # 固定 "flink"
    auto: bool  # 固定 True（无可选，架构决定）
    detail: str
    uncovered_modes: tuple[str, ...] = ()  # 恒为空（flink 支持 full/incremental/cdc 全集）

    @property
    def label(self) -> str:
        """展示名：flink。"""
        return self.tool or "flink"


def required_modes(contracts: Iterable) -> tuple[str, ...]:
    """这批契约一共要求哪几种装载方式（去重排序）。

    统一架构下已无实际选择逻辑，但 preflight 仍调此函数汇总 modes，为兼容保留。
    """
    return tuple(sorted({(c.load_strategy or "full").strip().lower() for c in contracts}))


def resolve_sync_tool(
    airflow,
    *,
    modes: tuple[str, ...] | list[str] = (),
    forced: str | None = None,
) -> SyncToolChoice:
    """定下本次用什么搬——统一架构下恒为 Flink SQL on YARN，不再有选择。

    ``airflow`` / ``modes`` / ``forced`` 参数为兼容保留（调用方未重构），实际不再使用。
    Flink 支持 full/incremental/cdc 全集，故 uncovered_modes 恒为空。
    """
    return SyncToolChoice(
        channel="flink_on_yarn",
        tool="flink",
        auto=True,
        detail=(
            "统一执行架构：搬运一律走 Flink SQL on YARN（与 transform/metric 同一执行路径），"
            "支持全量/增量/CDC 全集。"
        ),
        uncovered_modes=(),
    )


def engine_modes(
    airflow, engine: str, *, choice_tool: str | None = None
) -> tuple[list[str] | None, str]:
    """目标引擎在执行侧真正支持的装载方式——统一架构下 Flink 支持所有引擎的 full/incremental/cdc。

    ``airflow`` / ``choice_tool`` 参数为兼容保留（调用方未重构），实际不再使用。
    Flink SQL generator 可生成任意引擎（hive/postgres/doris/...）的 sink DDL，
    故这里不再按 runner capabilities 或工具适配器能力过滤，恒返回全集。
    """
    return (
        ["full", "incremental", "cdc"],
        "Flink SQL 支持所有引擎的全量/增量/CDC（sink DDL 由 flink_sql_generator 生成）。",
    )
