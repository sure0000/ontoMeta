"""专用 Data Agent 运行记录工具回归。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.models import ChatBiConversation, ChatBiConversationTask
from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.models.chat_bi_ledger import ChatBiDecisionRecord
from app.models.governance import GovernanceStandardRecord
from app.models.ontology import VersionRecord
from app.services import chat_bi_ledger
from app.services.agent_grounding import FactLedger
from app.services.chat_bi import ChatBiService
from app.services.ops_records import ledger_names, ledger_values
from app.services.task_pipeline import TaskPipelineService
from tests.test_chat_bi_golden import _seed_golden_domain


def test_get_landing_returns_explicit_not_landed_fact(db):
    _domain_id, ontology_id, aliases = _seed_golden_domain()
    service = ChatBiService()

    result, summary, is_error = service._dispatch_get_landing(
        db,
        ontology_id=ontology_id,
        args={"target_kind": "object", "target_id": aliases["@order"]},
    )

    assert is_error is False
    assert "订单" in summary
    assert result["family"] == "landing"
    assert result["subject"] == "订单"
    assert result["facts"][0]["key"] == "state"
    assert result["facts"][0]["value"] == "not_landed"
    assert result["as_of"] is None
    assert result["observed_at"]


def test_get_landing_keyword_requires_unique_subject(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    service = ChatBiService()

    result, _summary, is_error = service._dispatch_get_landing(
        db,
        ontology_id=ontology_id,
        args={"target_kind": "object", "keyword": "不存在的对象"},
    )

    assert is_error is False
    assert result["family"] == "landing"
    assert result["candidates"] == []
    assert "没有匹配" in result["note"]


def test_ops_record_reader_is_groundable_and_scoped(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
        db,
        ontology_id=ontology_id,
        args={"family": "task_run", "limit": 5},
    )

    assert is_error is False
    assert result["family"] == "task_run"
    assert result.get("items", []) == []
    assert result["note"] == "这个范围里没有任何数据任务。"
    assert result["observed_at"]


def test_ops_record_scope_all_and_conversation_are_honored(db):
    domain_a, ontology_a, _aliases_a = _seed_golden_domain()
    ontology_b = "other-ontology"
    service = ChatBiService()
    mine = GovernanceArtifact(
        kind="materialize",
        name="本会话物化",
        ontology_id=ontology_a,
        intent="i",
        spec_json="{}",
        status=ArtifactStatus.SUCCEEDED.value,
    )
    other = GovernanceArtifact(
        kind="transform",
        name="另一域加工",
        ontology_id=ontology_b,
        intent="i",
        spec_json="{}",
        status=ArtifactStatus.FAILED.value,
    )
    db.add_all([mine, other])
    db.commit()
    conversation = ChatBiConversation(domain_id=domain_a, title="运行记录")
    db.add(conversation)
    db.commit()
    service.link_conversation_task(db, conversation.id, mine.id, kind=mine.kind)

    try:
        all_records, _summary, is_error = service._dispatch_get_ops_record(
            db,
            ontology_id=ontology_a,
            args={"family": "task_run", "scope": "all", "limit": 10},
        )
        assert is_error is False
        assert {item["artifact_id"] for item in all_records["items"]} >= {mine.id, other.id}

        conversation_records, _summary, is_error = service._dispatch_get_ops_record(
            db,
            ontology_id=ontology_a,
            args={"family": "task_run", "scope": "conversation", "limit": 10},
            conversation_id=conversation.id,
        )
        assert is_error is False
        assert {item["artifact_id"] for item in conversation_records["items"]} == {mine.id}
    finally:
        db.query(ChatBiConversationTask).filter(
            ChatBiConversationTask.conversation_id == conversation.id
        ).delete(synchronize_session=False)
        db.delete(conversation)
        db.delete(mine)
        db.delete(other)
        db.commit()


def test_ops_record_rejects_unknown_scope_and_hides_cross_ontology_artifact(db):
    _domain_a, ontology_a, _aliases_a = _seed_golden_domain()
    ontology_b = "other-ontology"
    service = ChatBiService()
    artifact = GovernanceArtifact(
        kind="materialize",
        name="别域物化",
        ontology_id=ontology_b,
        intent="i",
        spec_json="{}",
        status=ArtifactStatus.SUCCEEDED.value,
    )
    db.add(artifact)
    db.commit()

    try:
        _result, _summary, is_error = service._dispatch_get_ops_record(
            db,
            ontology_id=ontology_a,
            args={"family": "task_run", "scope": "invalid"},
        )
        assert is_error is True

        result, _summary, is_error = service._dispatch_get_ops_record(
            db,
            ontology_id=ontology_a,
            args={"family": "task_run", "artifact_id": artifact.id},
        )
        assert is_error is False
        assert result.get("items", []) == []
        assert "不属于当前数据域" in (result.get("note") or "")
    finally:
        db.delete(artifact)
        db.commit()


def test_ops_ledger_names_only_contains_declared_facts():
    result = {
        "family": "landing",
        "subject": "订单",
        "facts": [
            {"key": "state", "value": "landed"},
            {"key": "ods_table", "value": "ods.ods_order"},
            {"key": "queryable", "value": True},
        ],
        "items": [],
    }
    assert set(ledger_names(result)) == {"订单", "ods.ods_order"}

    nested = {
        "family": "standard",
        "facts": [
            {
                "key": "enforced_rules",
                "value": [{"code": "require_owner", "description": "必须声明 owner"}],
            }
        ],
    }
    assert set(ledger_names(nested)) == {"require_owner", "必须声明 owner"}


def test_pipeline_reader_returns_whole_chain_and_enforces_ontology_boundary(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    service = TaskPipelineService()
    pipeline = service.create(
        db,
        name="订单加工链",
        intent="先同步再加工",
        ontology_id=ontology_id,
        steps=[
            {"kind": "sync", "intent": "同步订单"},
            {"kind": "transform", "intent": "清洗订单"},
        ],
    )

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "pipeline", "pipeline_id": pipeline.id},
        )
        assert is_error is False
        assert result["family"] == "pipeline"
        assert result["subject"] == "订单加工链"
        assert [item["kind"] for item in result["items"]] == ["sync", "transform"]
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert facts["status"] == "drafted"
        assert facts["step_count"] == 2
        assert facts["next_step_index"] == 0

        hidden, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id="another-ontology",
            args={"family": "pipeline", "pipeline_id": pipeline.id},
        )
        assert is_error is False
        assert "不属于当前数据域" in (hidden.get("note") or "")

        invalid, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "pipeline", "scope": "conversation"},
        )
        assert is_error is True
        assert "pipeline" in invalid["error"]
    finally:
        db.delete(pipeline)
        db.commit()


def test_decision_reader_is_current_conversation_only_and_reports_closure(db):
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    conversation = ChatBiConversation(domain_id=domain_id, title="六环审计")
    db.add(conversation)
    db.commit()
    chat_bi_ledger.record_decision(
        db,
        conversation_id=conversation.id,
        node="requirement",
        subject_id="reviewer-1",
        subject_role="admin",
        summary="确认订单分析需求",
    )
    chat_bi_ledger.record_decision(
        db,
        conversation_id=conversation.id,
        node="plan",
        subject_id="reviewer-2",
        ref_kind="artifact",
        ref_id="artifact-pending",
        summary="确认执行方案",
    )

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            domain_id=domain_id,
            conversation_id=conversation.id,
            args={"family": "decision", "limit": 20},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert facts["reached_count"] == 2
        assert facts["total_count"] == 6
        assert facts["dangling_count"] >= 1
        assert {item["subject_id"] for item in result["items"]} == {
            "reviewer-1",
            "reviewer-2",
        }
        assert all(isinstance(item["created_at"], str) for item in result["items"])

        missing, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "decision"},
        )
        assert is_error is True
        assert "对话上下文" in missing["error"]
    finally:
        db.query(ChatBiDecisionRecord).filter(
            ChatBiDecisionRecord.conversation_id == conversation.id
        ).delete(synchronize_session=False)
        db.delete(conversation)
        db.commit()


def test_ontology_version_reader_lists_versions_and_expands_diff(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    version = 9001
    record = VersionRecord(
        entity_type="ontology",
        entity_id=ontology_id,
        version=version,
        diff_summary="新增发票对象",
        diff_json=json.dumps(
            {
                "object_types": {
                    "added": [{"id": "invoice", "display_name": "发票"}],
                    "removed": [],
                    "modified": [],
                }
            },
            ensure_ascii=False,
        ),
        operator="publisher-1",
    )
    db.add(record)
    db.commit()

    try:
        listed, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "ontology_version", "limit": 20},
        )
        assert is_error is False
        assert any(item["version"] == version for item in listed["items"])
        assert all(isinstance(item["created_at"], str) for item in listed["items"])

        diff, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "ontology_version", "version": version},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in diff["facts"]}
        assert facts["version"] == version
        assert facts["change_count"] == 1
        assert diff["items"][0]["section"] == "object_types"
        assert diff["items"][0]["value"]["display_name"] == "发票"

        invalid, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "ontology_version", "scope": "all"},
        )
        assert is_error is True
        assert "ontology_version" in invalid["error"]
    finally:
        db.delete(record)
        db.commit()


def test_standard_reader_returns_active_standard_and_publication_history(db):
    active_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    record = GovernanceStandardRecord(
        version="1.0.0",
        status="published",
        note="Data Agent 只读回归",
        activated_at=active_at,
    )
    db.add(record)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=None,
            args={"family": "standard", "scope": "global", "limit": 20},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert facts["active_version"] == "1.0.0"
        assert facts["rule_count"] > 0
        assert facts["enforced_rule_count"] > 0
        assert any(item["id"] == record.id for item in result["items"])
        assert datetime.fromisoformat(result["as_of"]).replace(
            tzinfo=timezone.utc
        ) == active_at
        assert all(isinstance(item["created_at"], str) for item in result["items"])

        invalid, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id="onto",
            args={"family": "standard", "scope": "ontology"},
        )
        assert is_error is True
        assert "standard" in invalid["error"]
    finally:
        db.delete(record)
        db.commit()


def test_ops_ledger_values_registers_numeric_facts():
    result = {
        "family": "decision",
        "facts": [
            {"key": "reached_count", "label": "已到达环数", "value": 3},
            {"key": "total_count", "label": "六环总数", "value": 6},
        ],
        "items": [{"node": "需求确认", "count": 2}],
    }
    assert {2, 3, 6} <= set(ledger_values(result))

    ledger = FactLedger()
    ChatBiService._ledger_register(ledger, "get_ops_record", result, False)
    assert ledger.has_numeric("2")
    assert ledger.has_numeric("3")
    assert ledger.has_numeric("6")
