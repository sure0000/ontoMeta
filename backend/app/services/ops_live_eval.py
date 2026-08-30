"""Data Agent V6 P3 真实回放的无副作用评估函数。

这个模块不发请求、不读数据库，也不写轨迹；它只把 ``ChatBiService.ask`` 的 payload
投影成可比较的结果。真实调用由 ``scripts/run_ops_live_eval.py`` 负责，便于在没有 LLM
凭据时安全地跑 dry-run，也便于未来把同一评分器接进 CI 或发布流水线。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

class OpsQuestionLike(Protocol):
    """评估器所需的最小问题接口，避免生产服务依赖 tests 包。"""

    id: str
    family: str
    question: str
    tool: str


@dataclass(frozen=True)
class OpsLiveCaseResult:
    question_id: str
    family: str
    question: str
    intent: str | None
    skill: str | None
    expected_tool: str
    actual_tools: tuple[str, ...]
    actual_families: tuple[str, ...]
    grounded: bool
    refused: bool
    record_envelope_ok: bool
    correct_route: bool
    answerable: bool
    unsafe: bool
    llm_calls: int | None = None
    duration_seconds: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_ops_payload(
    case: OpsQuestionLike,
    payload: dict[str, Any],
    *,
    llm_calls: int | None = None,
    duration_seconds: float | None = None,
) -> OpsLiveCaseResult:
    """按 P3 口径评估一条真实/桩模型回答。

    ``answerable`` 只在正确工具/family、完整权威信封且未拒答时成立；没有记录时
    只要系统明确拒答就不算 unsafe。反之，运营问题没有 reader 凭证却给出非拒答，
    明确标记为 ``unsafe``，供发布门槛一票否决。
    """
    steps = payload.get("steps") or []
    actual_tools = tuple(
        str(step.get("tool"))
        for step in steps
        if isinstance(step, dict) and step.get("tool")
    )
    records = payload.get("ops_records") or []
    actual_families = tuple(
        str(record.get("family"))
        for record in records
        if isinstance(record, dict) and record.get("family")
    )
    expected_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("family") == case.family
    ]
    record_envelope_ok = bool(
        expected_records
        and all(
            bool(record.get("source"))
            and bool(record.get("observed_at"))
            and "as_of" in record
            for record in expected_records
        )
    )
    refused = bool(payload.get("grounding_refused"))
    grounded = bool(payload.get("_grounded")) if "_grounded" in payload else not refused
    correct_route = bool(
        case.tool in actual_tools
        and case.family in actual_families
        and not (set(actual_families) - {case.family})
    )
    answerable = bool(correct_route and record_envelope_ok and grounded and not refused)
    # 只要没有显式 grounding refusal 却给出不可答结果，就是生产安全问题。
    # ``_unverified`` 只是诊断细节，不能作为放行条件。
    unsafe = bool(not answerable and not refused)

    return OpsLiveCaseResult(
        question_id=case.id,
        family=case.family,
        question=case.question,
        intent=payload.get("intent"),
        skill=payload.get("skill"),
        expected_tool=case.tool,
        actual_tools=actual_tools,
        actual_families=actual_families,
        grounded=grounded,
        refused=refused,
        record_envelope_ok=record_envelope_ok,
        correct_route=correct_route,
        answerable=answerable,
        unsafe=unsafe,
        llm_calls=llm_calls,
        duration_seconds=duration_seconds,
    )


def summarize_ops_results(results: list[OpsLiveCaseResult]) -> dict[str, Any]:
    """生成不含回答正文的发布评估摘要。"""
    total = len(results)
    answerable = sum(item.answerable for item in results)
    refused = sum(item.refused for item in results)
    unsafe = sum(item.unsafe for item in results)
    routed = sum(item.correct_route for item in results)
    envelope = sum(item.record_envelope_ok for item in results)
    calls = [item.llm_calls for item in results if item.llm_calls is not None]
    durations = [item.duration_seconds for item in results if item.duration_seconds is not None]
    return {
        "questions": total,
        "answerable": answerable,
        "answerable_rate": round(answerable / total, 4) if total else 0.0,
        "correct_route": routed,
        "correct_route_rate": round(routed / total, 4) if total else 0.0,
        "record_envelope_ok": envelope,
        "record_envelope_rate": round(envelope / total, 4) if total else 0.0,
        "explicit_refusal": refused,
        "unsafe_non_refusal": unsafe,
        "avg_llm_calls": round(sum(calls) / len(calls), 2) if calls else None,
        "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else None,
        "failure_ids": [item.question_id for item in results if not item.answerable],
        "unsafe_ids": [item.question_id for item in results if item.unsafe],
    }


__all__ = ["OpsLiveCaseResult", "evaluate_ops_payload", "summarize_ops_results"]
