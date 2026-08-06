"""搬运工具的选择策略——**唯一决策处**。

**为什么把这件事从物化弹窗收回来**：搬运工具是**部署事实**，不是每次物化的业务选择。
在默认的 runner 通道下它甚至不参与执行——可搬性门禁按 runner 如实声明的 capabilities 判
（``job_planner._runner_reject``），执行档由 runner 逐表自选（native 优先，搬不了的交
seatunnel，见 ``sync_runner/backends/__init__.py``），``_build_runner`` 根本不接 ``tool``。
弹窗里那个下拉因此只能表达一件与执行无关的事，还会主动误导：选 datax 会被「没有可用镜像」
拦住，而 runner 通道压根不用镜像。

故：**选择归这里，界面只呈现结果**。docker 旧通道下工具仍是真变量（决定镜像与运行命令），
由本模块按「所需装载方式 ∩ 工具能力 ∩ 镜像可用」挑；设置页可强制指定，指定即不再自动。

**不静默降级**：挑出来的工具覆盖不了某些装载方式时，如实记进 ``uncovered_modes``——
那些表随后会被 planner 列进 ``unsupported``，preflight 提前把这句话说出来，而不是让人
在回执里自己发现少搬了几张表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.warehouse.jobs import (
    DEFAULT_SYNC_TOOL,
    SyncImageUnavailableError,
    UnknownSyncToolError,
    available_sync_tools,
    get_job_adapter,
    resolve_docker_image,
)

# 设置页里「自动」的取值（空串）。约定成常量，免得各处各写各的判空。
AUTO = ""

# docker 通道下的挑选优先级。seatunnel 在前：仓库既有 SyncExecutor 已在产它的配置、
# 且有官方镜像；datax 在后：无官方镜像，除非部署方配过 SYNC_TOOL_IMAGES 否则不可用。
# **flink 不在此列**：同步一律走 SeaTunnel / DataX，flink 已退出搬运序列（见 _NON_SYNC_TOOLS）。
_PRIORITY = (DEFAULT_SYNC_TOOL, "datax")

# 已退出搬运序列的工具（2026-08-06 决策）：同步不论是否同源都走 SeaTunnel / DataX；
# flink 专职做计算（transform/metric 的 Flink SQL），不再作搬运工具——既不被 auto 选中，
# 也不接受被强制指定为搬运工具。FlinkAdapter 仍留在注册表供计算侧复用其类与命令形态，
# 故这里用工具名黑名单表达「退出搬运」，而非从注册表删除。
_NON_SYNC_TOOLS = frozenset({"flink"})


class SyncToolResolutionError(RuntimeError):
    """选不出可用的搬运工具。面向用户可读，由调用方转成物化错误/preflight 阻断项。"""


@dataclass(frozen=True)
class SyncToolChoice:
    """本次物化实际会用什么搬。``tool=None`` 表示不由 ontoMeta 指定（runner 逐表自选）。"""

    channel: str
    tool: str | None
    # 是否为自动决策。False = 设置页（或调用方）强制指定，界面据此说明「已被指定」。
    auto: bool
    # 一句人读的结论，直接进 preflight 与回执——自动决策必须可解释，否则出问题无从查。
    detail: str
    # 选中的工具覆盖不了的装载方式。这些表会进 unsupported，需在提交前说清。
    uncovered_modes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """展示名。runner 通道下没有「ontoMeta 选的工具」这回事，故显式说 auto。"""
        return self.tool or "auto"


def required_modes(contracts: Iterable) -> tuple[str, ...]:
    """这批契约一共要求哪几种装载方式（去重排序）。选工具的输入之一。

    取契约而不是自己查库：调用方本来就拿着这批契约（``list_selected``），多查一遍既慢
    又会多出一处需要在测试里 patch 的 seam。
    """
    return tuple(sorted({(c.load_strategy or "full").strip().lower() for c in contracts}))


def resolve_sync_tool(
    airflow,
    *,
    modes: tuple[str, ...] | list[str] = (),
    forced: str | None = None,
) -> SyncToolChoice:
    """定下本次用什么搬。

    ``forced`` 是调用方的显式指定（物化请求里的 ``sync_tool``，为兼容保留）；为空时退到
    设置页的 ``sync_tool``；再为空即自动。``modes`` 是本次要求的装载方式集合。
    """
    channel = (airflow.sync_channel or "runner").lower()
    pinned = (forced or getattr(airflow, "sync_tool", "") or AUTO).strip().lower()

    # flink 已退出搬运：把它强制指定为搬运工具是矛盾指令，两条通道下都直接拒，
    # 不静默照收（照收会让人以为「换 flink 搬」是个有效选择）。
    if pinned in _NON_SYNC_TOOLS:
        raise SyncToolResolutionError(
            f"「{pinned}」已不作搬运工具：同步一律走 SeaTunnel / DataX，"
            f"{pinned} 专职计算（transform/metric 的 Flink SQL）。"
            "请改用 seatunnel 或 datax，或设为自动。"
        )

    if channel != "docker":
        if pinned:
            return SyncToolChoice(
                channel,
                pinned,
                auto=False,
                detail=(
                    f"设置页指定了「{pinned}」，但当前是 runner 通道：该指定不影响执行，"
                    "档位仍由 runner 逐表自选。"
                ),
            )
        return SyncToolChoice(
            channel,
            None,
            auto=True,
            detail=(
                "runner 通道：由执行侧逐表自选档位（native 优先，它搬不了的交 seatunnel），"
                "能不能搬按 runner 声明的能力判。"
            ),
        )

    overrides = airflow.sync_tool_images
    wanted = tuple(dict.fromkeys(m for m in modes if m))

    if pinned:
        try:
            adapter = get_job_adapter(pinned)
        except UnknownSyncToolError as exc:
            raise SyncToolResolutionError(str(exc)) from exc
        try:
            image = resolve_docker_image(adapter, overrides)
        except SyncImageUnavailableError as exc:
            raise SyncToolResolutionError(str(exc)) from exc
        uncovered = tuple(m for m in wanted if not adapter.supports(m))
        return SyncToolChoice(
            channel,
            adapter.name,
            auto=False,
            detail=f"设置页指定「{adapter.name}」，执行镜像 {image}。",
            uncovered_modes=uncovered,
        )

    # flink 即便有官方镜像也不作搬运候选：在源头剔除，auto 序列与兜底都不会再碰它。
    available = [t for t in available_sync_tools(overrides) if t not in _NON_SYNC_TOOLS]
    if not available:
        raise SyncToolResolutionError(
            "docker 通道下没有任何搬运工具有可用镜像："
            f"{', '.join(sorted(_PRIORITY))} 均未配置。在设置页的「搬运工具镜像」里"
            "填 工具名=镜像，或把执行通道改回 runner。"
        )
    # 优先级内的排前面，之后按名字排——新增工具即便没进优先级表也不会被漏掉。
    ordered = [t for t in _PRIORITY if t in available]
    ordered += [t for t in available if t not in _PRIORITY]

    for name in ordered:
        adapter = get_job_adapter(name)
        if all(adapter.supports(m) for m in wanted):
            return SyncToolChoice(
                channel,
                name,
                auto=True,
                detail=(
                    f"docker 通道：按镜像可用性与装载方式自动选中「{name}」"
                    f"（本次需要 {', '.join(wanted) or '全量'}）。"
                ),
            )

    # 谁也覆盖不全：取覆盖最多的，并如实说清搬不了哪些——那些表会进 unsupported。
    best = max(ordered, key=lambda n: sum(get_job_adapter(n).supports(m) for m in wanted))
    adapter = get_job_adapter(best)
    uncovered = tuple(m for m in wanted if not adapter.supports(m))
    return SyncToolChoice(
        channel,
        best,
        auto=True,
        detail=(
            f"docker 通道：可用工具里「{best}」覆盖最多，但它不支持 "
            f"{', '.join(uncovered)}——这些表不会产搬运作业。"
        ),
        uncovered_modes=uncovered,
    )


def engine_modes(
    airflow, engine: str, *, choice_tool: str | None
) -> tuple[list[str] | None, str]:
    """目标引擎在**执行侧**真正支持的装载方式。问不到返回 ``(None, 原因)``。

    **宁可返回 None 也不猜**：这个值用于置灰/过滤「同步方式」，猜错的代价是让人选不了
    一个其实能跑的方式，或选了一个其实跑不了的。

    与 ``resolve_sync_tool`` 同住这里，是因为二者答的是同一个问题的两半（用什么搬 /
    搬得动哪些方式），且物化弹窗与 Data Agent 的建数表单必须拿到同一个答案——此前它
    只存在于 ``api/warehouse`` 的一个私有函数里，agent 那条路便无从得知。
    """
    channel = (airflow.sync_channel or "runner").lower()
    key = (engine or "").lower()
    if channel != "runner":
        if not choice_tool or choice_tool == "auto":
            return None, "docker 通道且未定下工具，无法确定可用的装载方式。"
        adapter = get_job_adapter(choice_tool)
        return (
            [m for m in ("full", "incremental", "cdc") if adapter.supports(m)],
            f"取自 {adapter.name} 适配器声明的能力。",
        )
    if not airflow.sync_runner_endpoint:
        return None, "未配置 sync-runner 地址，问不到执行侧能力。"
    from app.connectors.sync_runner import SyncRunnerClient, SyncRunnerError

    client = SyncRunnerClient(
        airflow.sync_runner_endpoint,
        token=airflow.sync_runner_token,
        # 调用方（弹窗打开 / agent 循环内）都在等这一次同步调用，超时要短：
        # 问不到只是不置灰，不该让界面或对话卡住。
        timeout=5.0,
    )
    try:
        caps = client.capabilities()
    except SyncRunnerError as exc:
        return None, f"sync-runner 不可达，问不到执行侧能力（{exc}）。"
    finally:
        client.close()
    modes = (caps.get("sink_modes") or {}).get(key)
    if modes is None:
        sinks = ", ".join(caps.get("sinks") or []) or "无"
        return [], f"runner 不支持写入 {engine}（它声明的目标：{sinks}）。"
    return list(modes), f"取自 sync-runner 声明的 {engine} 能力。"
