"""专用 Data Agent 运行记录工具回归。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models import (
    ChatBiConversation,
    ChatBiConversationTask,
    DataApp,
    DataAppVersion,
    DataSource,
    DependencyComponent,
    DraftGenerationTask,
    ObjectType,
    WarehouseMigrationBatch,
    WarehouseMigrationEvidence,
)
from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.models.chat_bi_ledger import ChatBiDecisionRecord
from app.models.governance import GovernanceStandardRecord
from app.models.ontology import VersionRecord
from app.services import chat_bi_ledger
from app.services.agent_grounding import FactLedger
from app.services.chat_bi import ChatBiService
from app.services.chat_bi_tool_schemas import _GET_OPS_RECORD_TOOL
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


def test_landing_missing_subject_is_authoritative_empty_envelope(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    result, _summary, is_error = ChatBiService()._dispatch_get_landing(
        db,
        ontology_id=ontology_id,
        args={"target_kind": "object", "keyword": "不存在的对象"},
    )
    assert is_error is False
    assert result["family"] == "landing"
    assert result["subject"] == "不存在的对象"
    assert result["candidates"] == []
    assert result["as_of"] is None
    assert result["observed_at"]
    assert result["source"] == "OntologyQueryService（当前已发布本体目录）"


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


def test_ops_tool_schema_exposes_all_registered_read_families():
    family_schema = _GET_OPS_RECORD_TOOL["function"]["parameters"]["properties"]["family"]
    assert set(family_schema["enum"]) == {
        "task_run",
        "pipeline",
        "decision",
        "ontology_version",
        "standard",
        "draft_run",
        "merge_report",
        "conflict",
        "datasource",
        "data_app",
        "component",
        "migration",
    }


def test_draft_run_reader_returns_latest_generation_state(db):
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    task = DraftGenerationTask(
        domain_context_id=domain_id,
        ontology_id=ontology_id,
        scope="objects",
        status="failed",
        progress=65,
        message="对象生成中断",
        error_summary="模型返回格式不合法",
    )
    db.add(task)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            domain_id=domain_id,
            args={"family": "draft_run"},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert result["subject"] == task.id
        assert facts["status"] == "failed"
        assert facts["progress"] == 65
        assert facts["error_summary"] == "模型返回格式不合法"
        assert isinstance(result["items"][0]["created_at"], str)

        invalid, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "draft_run", "scope": "all"},
        )
        assert is_error is True
        assert "draft_run" in invalid["error"]
    finally:
        db.delete(task)
        db.commit()


def test_merge_report_reader_flattens_and_bounds_authoritative_report(db):
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    changes = [
        {"id": f"obj-{index}", "name": f"object_{index}", "display_name": f"对象{index}"}
        for index in range(5)
    ]
    task = DraftGenerationTask(
        domain_context_id=domain_id,
        ontology_id=ontology_id,
        scope="full",
        status="succeeded",
        progress=100,
        merge_report_json=json.dumps(
            {
                "summary": {"added": 5, "updated": 0, "kept": 0, "conflict": 0, "removed": 0},
                "object_types": {
                    "added": changes,
                    "updated": [],
                    "kept": [],
                    "conflict": [],
                    "removed": [],
                },
                "properties": {},
                "relation_types": {},
                "business_logics": {},
            },
            ensure_ascii=False,
        ),
    )
    db.add(task)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            domain_id=domain_id,
            args={"family": "merge_report", "task_id": task.id, "limit": 3},
        )
        assert is_error is False
        assert result["subject"] == task.id
        assert len(result["items"]) == 3
        assert result["truncated"] is True
        assert all(item["outcome"] == "added" for item in result["items"])
        summary = next(
            fact["value"] for fact in result["facts"] if fact["key"] == "summary"
        )
        assert summary["added"] == 5
    finally:
        db.delete(task)
        db.commit()


def test_conflict_reader_returns_only_current_ontology_conflicts(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    obj = ObjectType(
        ontology_id=ontology_id,
        name=f"ops_conflict_{uuid4().hex[:8]}",
        display_name="待复核订单",
        status="edited",
        conflict_json=json.dumps(
            {"display_name": {"base": "订单表", "ours": "订单", "theirs": "订单信息"}},
            ensure_ascii=False,
        ),
    )
    db.add(obj)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "conflict", "limit": 20},
        )
        assert is_error is False
        item = next(item for item in result["items"] if item["entity_id"] == obj.id)
        assert item["field"] == "display_name"
        assert item["ours"] == "订单"
        assert item["theirs"] == "订单信息"
    finally:
        db.delete(obj)
        db.commit()


def test_datasource_reader_is_global_read_only_and_redacts_dsn(db):
    tag = uuid4().hex[:8]
    tested_at = datetime.now(timezone.utc)
    datasource = DataSource(
        name=f"ERP-{tag}",
        kind="mysql",
        purpose="business_source",
        dsn_secret_ref="mysql://reader:top-secret@erp.internal:3306/erp_prod",
        catalog_name=f"erp_{tag}",
        status="ok",
        tested_at=tested_at,
    )
    db.add(datasource)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=None,
            args={"family": "datasource", "keyword": tag},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert result["subject"] == f"ERP-{tag}"
        assert facts["status"] == "ok"
        assert facts["database"] == "erp_prod"
        assert "top-secret" not in json.dumps(result, ensure_ascii=False)

        invalid, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id="onto",
            args={"family": "datasource", "scope": "ontology"},
        )
        assert is_error is True
        assert "datasource" in invalid["error"]
    finally:
        db.delete(datasource)
        db.commit()


def test_data_app_reader_honors_ontology_scope_and_lists_versions(db):
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    app = DataApp(
        domain_id=domain_id,
        ontology_id=ontology_id,
        app_type="screen",
        name=f"经营看板-{uuid4().hex[:8]}",
        status="published",
        current_version=3,
        published_version=2,
        published_at=datetime.now(timezone.utc),
    )
    db.add(app)
    db.flush()
    versions = [
        DataAppVersion(
            app_id=app.id,
            version=number,
            diff_summary=f"发布 v{number}",
            operator="publisher-1",
        )
        for number in (1, 2)
    ]
    db.add_all(versions)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            domain_id=domain_id,
            args={
                "family": "data_app",
                "app_id": app.id,
                "ontology_id": "forged-ontology",
                "domain_id": "forged-domain",
            },
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert result["subject"] == app.name
        assert facts["current_version"] == 3
        assert facts["published_version"] == 2
        assert facts["version_count"] == 2
        assert [item["version"] for item in result["items"]] == [2, 1]

        hidden, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id="another-ontology",
            domain_id="another-domain",
            args={"family": "data_app", "app_id": app.id},
        )
        assert is_error is False
        assert "没有匹配" in (hidden.get("note") or "")
    finally:
        db.query(DataAppVersion).filter(DataAppVersion.app_id == app.id).delete(
            synchronize_session=False
        )
        db.delete(app)
        db.commit()


def test_component_reader_uses_redacted_projection_and_global_scope(db):
    tag = uuid4().hex[:8]
    component = DependencyComponent(
        key="llm",
        name=f"模型服务-{tag}",
        deploy_mode="external",
        deploy_status="failed",
        deploy_error="健康检查超时",
        deploy_spec_json=json.dumps({"ssh_password": "deploy-secret"}),
        connection_json=json.dumps({"api_key": "connection-secret"}),
        enabled=True,
    )
    db.add(component)
    db.commit()

    try:
        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=None,
            args={"family": "component", "keyword": tag},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert result["subject"] == component.name
        assert facts["deploy_status"] == "failed"
        assert facts["deploy_error"] == "健康检查超时"
        serialized = json.dumps(result, ensure_ascii=False)
        assert "deploy-secret" not in serialized
        assert "connection-secret" not in serialized
    finally:
        db.delete(component)
        db.commit()


def test_migration_reader_enforces_batch_ontology_boundary(db):
    _domain_id, ontology_id, _aliases = _seed_golden_domain()
    other_ontology = f"other-{uuid4()}"
    batch = WarehouseMigrationBatch(
        ontology_id=other_ontology,
        ontology_version=7,
        status="blocked",
        current_step=4,
        approver="publisher-1",
        approved_by="publisher-2",
        rollback_owner="publisher-3",
        observation_window_minutes=60,
        blocked_reason="影子校验未通过",
    )
    db.add(batch)
    db.flush()
    evidence = WarehouseMigrationEvidence(
        batch_id=batch.id,
        step=4,
        attempt=1,
        status="fail",
        report_json="{}",
        checksum="checksum-4",
        recorded_by="publisher-2",
    )
    db.add(evidence)
    db.commit()

    try:
        hidden, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "migration", "batch_id": batch.id},
        )
        assert is_error is False
        assert "当前范围内没有" in (hidden.get("note") or "")

        result, _summary, is_error = ChatBiService._dispatch_get_ops_record(
            db,
            ontology_id=ontology_id,
            args={"family": "migration", "batch_id": batch.id, "scope": "all"},
        )
        assert is_error is False
        facts = {fact["key"]: fact["value"] for fact in result["facts"]}
        assert facts["status"] == "blocked"
        assert facts["current_step"] == 4
        assert facts["blocked_reason"] == "影子校验未通过"
        assert result["items"][0]["recorded_by"] == "publisher-2"
    finally:
        db.delete(evidence)
        db.delete(batch)
        db.commit()
