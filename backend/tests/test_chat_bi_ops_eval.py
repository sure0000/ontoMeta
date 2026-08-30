"""Data Agent V6 P3：104 题运营问题集与权威 reader 契约验收。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import ChatBiConversation
from app.services.chat_bi import ChatBiService
from app.services.ops_live_eval import evaluate_ops_payload, summarize_ops_results
from app.services.ops_records import REGISTRY, route_ops_question
from tests.fixtures.ops_questions import (
    ANALYTICAL_COLLISION_QUESTIONS,
    OPS_QUESTIONS,
    WRITE_INTENT_QUESTIONS,
    OpsQuestion,
)
from tests.test_chat_bi_golden import _seed_golden_domain
from tests.test_chat_bi_golden import _StubCompletions


def _route_outcome(case: OpsQuestion) -> tuple[bool, str]:
    intent = ChatBiService._classify_intent(case.question)
    skill = ChatBiService._auto_select_skill(case.question)
    route = route_ops_question(case.question)
    actual = (
        f"intent={intent}, skill={skill}, "
        f"route={route.tool + '/' + route.family if route else None}, "
        f"matched={route.matched if route else ()}"
    )
    ok = bool(
        intent == "operational"
        and skill == "ops"
        and route is not None
        and route.tool == case.tool
        and route.family == case.family
    )
    return ok, actual


def test_ops_question_corpus_shape_and_distribution():
    counts = Counter(case.family for case in OPS_QUESTIONS)
    assert len(OPS_QUESTIONS) == 104
    assert set(counts) == set(REGISTRY)
    assert set(counts.values()) == {8}, counts
    assert len({case.id for case in OPS_QUESTIONS}) == len(OPS_QUESTIONS)
    assert len({case.question for case in OPS_QUESTIONS}) == len(OPS_QUESTIONS)


@pytest.mark.parametrize("case", OPS_QUESTIONS, ids=lambda case: case.id)
def test_ops_question_routes_to_expected_reader(case: OpsQuestion):
    ok, diagnostic = _route_outcome(case)
    assert ok, f"{case.id}: {case.question}\n{diagnostic}"
    hint = ChatBiService._ops_route_prompt_hint(case.question)
    assert case.tool in hint
    if case.tool == "get_ops_record":
        assert f"family='{case.family}'" in hint


def test_ops_question_answerability_target():
    failures = [
        f"{case.id}: {_route_outcome(case)[1]}"
        for case in OPS_QUESTIONS
        if not _route_outcome(case)[0]
    ]
    answerable = len(OPS_QUESTIONS) - len(failures)
    ratio = answerable / len(OPS_QUESTIONS)
    assert ratio >= 0.80, (
        f"P3 可答率 {ratio:.1%} ({answerable}/{len(OPS_QUESTIONS)}) < 80%\n"
        + "\n".join(failures)
    )


@pytest.fixture(scope="module")
def ops_eval_scope(client):
    domain_id, ontology_id, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conversation = ChatBiConversation(domain_id=domain_id, title="P3 运营评测")
        db.add(conversation)
        db.commit()
        conversation_id = conversation.id
    return domain_id, ontology_id, aliases, conversation_id


@pytest.mark.parametrize("family", sorted(REGISTRY))
def test_authoritative_reader_envelope(family: str, ops_eval_scope):
    """空记录也是可答结果，但 family/source/读取时点必须始终完整。"""
    domain_id, ontology_id, aliases, conversation_id = ops_eval_scope
    with SessionLocal() as db:
        if family == "landing":
            result, _summary, is_error = ChatBiService()._dispatch_get_landing(
                db,
                ontology_id=ontology_id,
                args={"target_kind": "object", "target_id": aliases["@order"]},
            )
        else:
            result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
                db,
                ontology_id=ontology_id,
                domain_id=domain_id,
                conversation_id=conversation_id,
                args={"family": family},
            )

    assert is_error is False, result
    assert result["family"] == family
    assert result.get("source"), f"{family}: 权威来源缺失"
    assert "as_of" in result  # 没有业务事件时允许为 None，不能拿读取时间冒充。
    assert result.get("observed_at"), f"{family}: 读取时点缺失"
    datetime.fromisoformat(result["observed_at"])


@pytest.mark.parametrize("question", ANALYTICAL_COLLISION_QUESTIONS)
def test_analytical_collision_stays_in_query_lane(question: str):
    assert ChatBiService._classify_intent(question) == "analytical"
    assert ChatBiService._auto_select_skill(question) == "query"


@pytest.mark.parametrize(("question", "expected_skill"), WRITE_INTENT_QUESTIONS)
def test_write_intent_never_enters_ops_lane(question: str, expected_skill: str):
    assert ChatBiService._auto_select_skill(question) == expected_skill


def test_component_name_alone_does_not_steal_lineage_question():
    assert route_ops_question("DataHub 里的订单血缘是什么？") is None
    assert ChatBiService._auto_select_skill("DataHub 里的订单血缘是什么？") == "lineage"


def test_live_evaluator_requires_complete_grounded_record():
    case = OPS_QUESTIONS[0]
    base = {
        "skill": "ops",
        "_grounded": True,
        "steps": [{"tool": case.tool}],
        "ops_records": [{
            "family": case.family,
            "source": "authoritative-reader",
            "observed_at": "2026-08-28T00:00:00+00:00",
            "as_of": None,
        }],
    }
    result = evaluate_ops_payload(case, base, llm_calls=2, duration_seconds=1.2)
    assert result.answerable is True
    assert result.unsafe is False

    refused = evaluate_ops_payload(
        case,
        {"grounding_refused": True, "steps": [], "ops_records": []},
    )
    assert refused.answerable is False
    assert refused.refused is True
    assert refused.unsafe is False

    unsafe = evaluate_ops_payload(
        case,
        {"answer": "已经成功", "steps": [], "ops_records": []},
    )
    assert unsafe.answerable is False
    assert unsafe.unsafe is True


def test_live_evaluator_summary_exposes_release_gate():
    case = OPS_QUESTIONS[0]
    result = evaluate_ops_payload(
        case,
        {
            "_grounded": True,
            "steps": [{"tool": case.tool}],
            "ops_records": [{
                "family": case.family,
                "source": "reader",
                "observed_at": "2026-08-28T00:00:00+00:00",
                "as_of": None,
            }],
        },
    )
    summary = summarize_ops_results([result])
    assert summary["answerable_rate"] == 1.0
    assert summary["unsafe_non_refusal"] == 0
    assert summary["failure_ids"] == []


def test_ops_golden_first_llm_call_forces_expected_reader(ops_eval_scope):
    """真实模型曾连续 search 到耗尽；首轮必须由服务端锁到权威 reader。"""
    from tests.fixtures.golden_questions import FinalTurn, ToolTurn
    from tests.test_chat_bi_intent_gate import _ask, _make_service

    domain_id, _ontology_id, aliases, _conversation_id = ops_eval_scope
    completions = _StubCompletions(
        [
            ToolTurn([("get_ops_record", {"family": "datasource"})]),
            FinalTurn("当前数据域没有数据任务记录。"),
        ],
        aliases,
    )
    service = _make_service(completions)
    payload = _ask(service, domain_id, "最近数据任务的执行状态是什么？")

    first = completions.requests[0]
    assert first["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_ops_record"},
    }
    # 模型脚本故意给错 datasource；服务端首轮上下文应覆盖为 task_run。
    assert payload["steps"][0]["arguments"]["family"] == "task_run"


def test_ops_first_reader_is_enforced_when_gateway_ignores_tool_choice(ops_eval_scope):
    """兼容网关即使返回 search_objects，服务端也必须先执行确定性 reader。"""
    from tests.fixtures.golden_questions import FinalTurn, ToolTurn
    from tests.test_chat_bi_intent_gate import _ask, _make_service

    domain_id, _ontology_id, aliases, _conversation_id = ops_eval_scope
    completions = _StubCompletions(
        [
            ToolTurn([("search_objects", {"keyword": "采购订单"})]),
            FinalTurn("当前已发布本体中没有匹配的采购订单主体，无法确认物理落点。"),
        ],
        aliases,
    )
    payload = _ask(
        _make_service(completions),
        domain_id,
        "采购订单落到哪张物理表了？",
    )

    assert completions.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_landing"},
    }
    assert payload["steps"][0]["tool"] == "get_landing"
    assert payload["steps"][0]["arguments"] == {
        "target_kind": "object",
        "keyword": "采购订单",
    }
    assert all(step["tool"] != "search_objects" for step in payload["steps"])
    assert payload["ops_records"][0]["family"] == "landing"
    assert payload["ops_records"][0]["source"]


def test_forced_ops_reader_normalizes_invalid_scope_and_finishes_once(ops_eval_scope):
    """真实 draft_run 回放曾携带 scope=all，导致 reader 失败后重复调用。"""
    from tests.fixtures.golden_questions import FinalTurn, ToolTurn
    from tests.test_chat_bi_intent_gate import _ask, _make_service

    domain_id, _ontology_id, aliases, _conversation_id = ops_eval_scope
    completions = _StubCompletions(
        [
            ToolTurn([("get_ops_record", {"family": "draft_run", "scope": "all"})]),
            FinalTurn("当前数据域没有草稿生成记录。"),
        ],
        aliases,
    )
    payload = _ask(_make_service(completions), domain_id, "查看最近的生成记录。")

    assert completions.calls == 2
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["arguments"] == {
        "family": "draft_run",
        "scope": "ontology",
    }
    assert payload["steps"][0]["status"] == "succeeded"
    assert payload["ops_records"][0]["family"] == "draft_run"
    assert not payload.get("grounding_refused")


def test_documented_question_matrix_is_in_sync():
    path = Path(__file__).resolve().parents[2] / "docs" / "DATA_AGENT_OPS_QUESTIONS.md"
    text = path.read_text(encoding="utf-8")
    missing = [case.id for case in OPS_QUESTIONS if f"`{case.id}`" not in text]
    assert not missing, f"运营问题文档缺少 {missing}"
