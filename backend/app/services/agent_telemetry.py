"""Data Agent 改造期遥测（P0）：给每一期改造提供**可对照的基线**。

**为什么存在**：DATA_AGENT_V2_PLAN 的每一期（语义工具化 / 上下文架构 / 口径编译器 /
分层编排）都声称能降拒绝率、省步数、减 LLM 调用。没有计数器，这些只能靠感觉判断。
这里记录四组数：步数、工具分布、拒绝码分布、LLM 调用次数——正是各期验收标准里的量。

**刻意不落库**：这是改造期的对照工具，不是生产可观测性。进程内计数器、进程重启即清零，
改造收尾后可整体摘除而不留 schema 债。要长期可观测性时再接正经的 metrics 出口。

用法：
    run = RunTelemetry()            # 每次问答开一个收集器
    run.llm_call(); run.tool("get_object", is_error=False); ...
    record(run)                     # 收尾提交进全局聚合
    snapshot()                      # GET /api/chat-bi/telemetry 读它
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RunTelemetry:
    """单次问答的计数器。只加不减，收尾时一次性提交给全局聚合。"""

    llm_calls: int = 0
    steps: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: Counter = field(default_factory=Counter)
    # SQL 语义证明拒绝码（unknown_column / undeclared_join / fanout_risk / ...）
    rejection_codes: Counter = field(default_factory=Counter)
    # run_sql 三态：executed / suggest_only（无数据源或权限不足）/ rejected
    run_sql_outcomes: Counter = field(default_factory=Counter)
    refused: bool = False
    refuse_kind: str | None = None  # ungrounded | unverified
    # P4.3 自愈回环：触发了几次重写、其中几次把答案救回来了。
    # 「repairs 高但 repaired_ok 低」说明重写指令没用，是要调的信号。
    repairs: int = 0
    repairs_succeeded: int = 0
    # P4.1 澄清反问。**与拒答分开计**：拒答是「答不了」，澄清是「先确认再答」，
    # 混在一起会让拒答率失去意义。
    clarifications: int = 0
    # P4.2 检索子 agent：它的 LLM 调用与步数**单独计**，
    # 因为它换的是「用更多 LLM 调用，换更小的主上下文」——两个数要能分开看，
    # 否则只会看到 avg_llm_calls 涨了，看不到主上下文省了多少。
    subagent_runs: int = 0
    subagent_llm_calls: int = 0
    subagent_steps: int = 0
    subagent_isolated_chars: int = 0
    # V4 O6.3 上下文预算：每次 LLM 调用前 messages 的字符估算累加，
    # 除以调用次数得 context_chars_per_call——看 O1 compaction / O2 离场的收益。
    context_chars: int = 0
    context_calls: int = 0
    # V4 O1 compaction：是否触发了摘要、摘了几轮。
    compaction_triggered: bool = False
    compaction_summarized_turns: int = 0
    # V4 O6.2 skill 路由：选中了哪个技能、是否“路对”（用上了该技能族的工具）。
    # misroute = 选了技能却没用上它解锁的任何工具——路由又白加一轮的信号。
    # V5 F1：另分出 skill_no_entity——「路对了但目标实体不存在、未及调解锁工具就拒答」，
    # 不该计作 misroute（那是域里没这个东西，不是路由选错）。
    skill_routed: str | None = None
    skill_matched: bool | None = None
    skill_no_entity: bool = False
    # V4 O2 大结果离场：被移出上下文的字符数（全量 JSON − 样例 JSON）。
    offloaded_chars: int = 0
    offload_count: int = 0
    # V5.1 ReAct：统计思考内容（thinking 标签）的使用情况
    thinking_count: int = 0  # 有多少次调用前有 thinking
    thinking_chars: int = 0  # 思考内容总字符数

    def offload(self, chars: int) -> None:
        self.offloaded_chars += max(0, int(chars))
        self.offload_count += 1

    def context(self, chars: int) -> None:
        self.context_chars += max(0, int(chars))
        self.context_calls += 1

    def compaction(self, *, triggered: bool, summarized_turns: int) -> None:
        self.compaction_triggered = triggered
        self.compaction_summarized_turns = summarized_turns

    def route(self, skill: str) -> None:
        self.skill_routed = skill

    def route_outcome(self, matched: bool, *, no_entity: bool = False) -> None:
        self.skill_matched = matched
        self.skill_no_entity = no_entity

    def clarification(self) -> None:
        self.clarifications += 1

    def subagent(self, *, llm_calls: int, steps: int, isolated_chars: int) -> None:
        self.subagent_runs += 1
        self.subagent_llm_calls += llm_calls
        self.subagent_steps += steps
        self.subagent_isolated_chars += isolated_chars

    def llm_call(self) -> None:
        self.llm_calls += 1

    def repair(self) -> None:
        self.repairs += 1

    def repair_succeeded(self) -> None:
        self.repairs_succeeded += 1

    def tool(self, name: str, *, is_error: bool) -> None:
        self.steps += 1
        self.tool_calls[name] += 1
        if is_error:
            self.tool_errors[name] += 1

    def rejection(self, code: str) -> None:
        self.rejection_codes[code] += 1

    def run_sql_outcome(self, outcome: str) -> None:
        self.run_sql_outcomes[outcome] += 1

    def refuse(self, kind: str) -> None:
        self.refused = True
        self.refuse_kind = kind

    def thinking(self, chars: int) -> None:
        """记录一次 ReAct 思考（V5.1）。"""
        self.thinking_count += 1
        self.thinking_chars += chars


class _Aggregate:
    """进程内全局聚合。加锁——ask/ask_stream 跑在不同事件循环任务里。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.runs = 0
            self.refused_runs = 0
            self.refuse_kinds: Counter = Counter()
            self.llm_calls = 0
            self.steps = 0
            self.tool_calls: Counter = Counter()
            self.tool_errors: Counter = Counter()
            self.rejection_codes: Counter = Counter()
            self.run_sql_outcomes: Counter = Counter()
            self.repairs = 0
            self.repairs_succeeded = 0
            self.clarifications = 0
            self.subagent_runs = 0
            self.subagent_llm_calls = 0
            self.subagent_steps = 0
            self.subagent_isolated_chars = 0
            self.context_chars = 0
            self.thinking_count = 0
            self.thinking_chars = 0
            self.context_calls = 0
            self.compaction_runs = 0
            self.compaction_summarized_turns = 0
            self.skill_routed: Counter = Counter()
            self.skill_misrouted = 0
            self.skill_no_entity = 0
            self.offloaded_chars = 0
            self.offload_count = 0

    def record(self, run: RunTelemetry) -> None:
        with self._lock:
            self.runs += 1
            self.llm_calls += run.llm_calls
            self.steps += run.steps
            self.repairs += run.repairs
            self.repairs_succeeded += run.repairs_succeeded
            self.clarifications += run.clarifications
            self.subagent_runs += run.subagent_runs
            self.subagent_llm_calls += run.subagent_llm_calls
            self.subagent_steps += run.subagent_steps
            self.subagent_isolated_chars += run.subagent_isolated_chars
            self.context_chars += run.context_chars
            self.thinking_count += run.thinking_count
            self.thinking_chars += run.thinking_chars
            self.context_calls += run.context_calls
            if run.compaction_triggered:
                self.compaction_runs += 1
                self.compaction_summarized_turns += run.compaction_summarized_turns
            if run.skill_routed:
                self.skill_routed[run.skill_routed] += 1
                if run.skill_no_entity:
                    self.skill_no_entity += 1
                elif run.skill_matched is False:
                    self.skill_misrouted += 1
            self.offloaded_chars += run.offloaded_chars
            self.offload_count += run.offload_count
            self.tool_calls.update(run.tool_calls)
            self.tool_errors.update(run.tool_errors)
            self.rejection_codes.update(run.rejection_codes)
            self.run_sql_outcomes.update(run.run_sql_outcomes)
            if run.refused:
                self.refused_runs += 1
                self.refuse_kinds[run.refuse_kind or "unknown"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            runs = self.runs or 1  # 防除零；runs=0 时各均值本就为 0
            return {
                "runs": self.runs,
                "refused_runs": self.refused_runs,
                "refusal_rate": round(self.refused_runs / runs, 4) if self.runs else 0.0,
                "refuse_kinds": dict(self.refuse_kinds),
                "avg_steps": round(self.steps / runs, 2) if self.runs else 0.0,
                "avg_llm_calls": round(self.llm_calls / runs, 2) if self.runs else 0.0,
                "tool_calls": dict(self.tool_calls),
                "tool_errors": dict(self.tool_errors),
                "rejection_codes": dict(self.rejection_codes),
                "run_sql_outcomes": dict(self.run_sql_outcomes),
                "repairs": self.repairs,
                "repairs_succeeded": self.repairs_succeeded,
                "clarifications": self.clarifications,
                "subagent_runs": self.subagent_runs,
                "subagent_llm_calls": self.subagent_llm_calls,
                "subagent_steps": self.subagent_steps,
                # 被隔离掉、**没有**进主上下文的字符数——P4.2 的收益就是这个数
                "subagent_isolated_chars": self.subagent_isolated_chars,
                # V4 O6.3：每次 LLM 调用平均上下文字符数（O1/O2 降它）
                "context_chars_per_call": (
                    round(self.context_chars / self.context_calls, 1)
                    if self.context_calls else 0.0
                ),
                # V5.1 ReAct：思考内容统计
                "thinking_count": self.thinking_count,
                "thinking_chars": self.thinking_chars,
                "avg_thinking_chars": (
                    round(self.thinking_chars / self.thinking_count, 1)
                    if self.thinking_count else 0.0
                ),
                # V4 O1：触发摘要的运行数与累计被摘要轮数
                "compaction_runs": self.compaction_runs,
                "compaction_summarized_turns": self.compaction_summarized_turns,
                # V4 O6.2 / V5 F1：技能路由分布、“路错率”与“路对但无实体”。
                # misroute 率只统计「真路错」（分母排除 no_entity）——否则会把「域里没这个对象」造成的拒答误计为路由缺陷。
                "skill_routed": dict(self.skill_routed),
                "skill_no_entity_runs": self.skill_no_entity,
                "skill_misroute_rate": (
                    round(
                        self.skill_misrouted
                        / max(1, sum(self.skill_routed.values()) - self.skill_no_entity),
                        4,
                    )
                    if (sum(self.skill_routed.values()) - self.skill_no_entity) > 0 else 0.0
                ),
                # V4 O2：大结果离场——被移出上下文的总字符数与离场次数
                "offloaded_chars": self.offloaded_chars,
                "offload_count": self.offload_count,
            }


_AGG = _Aggregate()


def record(run: RunTelemetry) -> None:
    _AGG.record(run)


def snapshot() -> dict:
    return _AGG.snapshot()


def reset() -> None:
    _AGG.reset()


__all__ = ["RunTelemetry", "record", "snapshot", "reset"]
