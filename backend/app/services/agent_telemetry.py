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
            }


_AGG = _Aggregate()


def record(run: RunTelemetry) -> None:
    _AGG.record(run)


def snapshot() -> dict:
    return _AGG.snapshot()


def reset() -> None:
    _AGG.reset()


__all__ = ["RunTelemetry", "record", "snapshot", "reset"]
