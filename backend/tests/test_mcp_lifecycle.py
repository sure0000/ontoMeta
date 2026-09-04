"""MCP 写侧生命周期与对象聚合回归。"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import mcp.types as types
import pytest

from app.agents import registry
from app.agents.drafters.base import Drafter
from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import AuthContext
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus
from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.services.agent_pipeline import AgentPipelineService


class _Drafter(Drafter):
    kind = "metric"
    required_context = ("ontology_id",)

    def draft(self, intent, context):
        return {
            "metric_name": "mcp_lifecycle_metric",
            "engine": "hive",
            "subject_objects": ["order"],
        }


class _Executor(Executor):
    kind = "metric"

    def __init__(self):
        self.executions = 0

    def dry_run(self, spec, context):
        return {"will_create": spec["metric_name"], "rows_affected": 0}

    def execute(self, spec, context):
        self.executions += 1
        return {"created": spec["metric_name"]}


@pytest.fixture
def metric_agent():
    old_drafter = registry.get_drafter("metric")
    old_executor = registry.get_executor("metric")
    executor = _Executor()
    registry.register("metric", _Drafter(), executor)
    yield executor
    registry.register("metric", old_drafter, old_executor)


@pytest.fixture
def seeded_ontology():
    suffix = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"mcp-lifecycle-{suffix}", name=f"MCP 生命周期 {suffix}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            version=1,
            status=OntologyStatus.PUBLISHED.value,
        )
        db.add(ontology)
        db.flush()
        db.add_all(
            [
                ObjectType(
                    ontology_id=ontology.id,
                    name="order",
                    display_name="订单",
                    table_role="business_object",
                    status="published",
                ),
                ObjectType(
                    ontology_id=ontology.id,
                    name=f"audit_{suffix}",
                    display_name="审计日志",
                    table_role="technical",
                    status="published",
                ),
            ]
        )
        db.commit()
        return ontology.id


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


def _ctx(role: str | None) -> AuthContext:
    return AuthContext(
        client_type="mcp_local", role=role, principal_name=f"mcp-{role}"
    )


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(name: str, arguments: dict, role: str | None):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(mcp_server, "resolve_auth_context", lambda: _ctx(role))
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _call


def _body(result):
    return json.loads(result.content[0].text)


def _draft_args(ontology_id: str) -> dict:
    return {
        "kind": "metric",
        "intent": "统计订单指标",
        "ontology_id": ontology_id,
        "context": {
            "ontology_id": ontology_id,
            "target_datasource_id": "warehouse-test",
            "business_logic_id": "logic-test",
        },
    }


def test_draft_task_editor_creates_and_validates(
    call_via_server, seeded_ontology, metric_agent
):
    result = call_via_server("draft_task", _draft_args(seeded_ontology), "editor")
    body = _body(result)
    assert result.is_error is False
    assert body["success"] is True
    data = body["data"]
    assert data["status"] == ArtifactStatus.VALIDATED.value
    assert data["validation"]["blocking_count"] == 0
    assert data["validation"]["dry_run"]["will_create"] == "mcp_lifecycle_metric"


def test_draft_task_reports_missing_context(
    call_via_server, seeded_ontology, metric_agent
):
    args = _draft_args(seeded_ontology)
    args["context"] = {"ontology_id": seeded_ontology}
    result = call_via_server("draft_task", args, "editor")
    body = _body(result)
    assert result.is_error is True
    assert body["success"] is False
    assert set(body["data"]["missing"]) >= {
        "target_datasource_id",
        "business_logic_id",
    }


def test_draft_task_rejects_mismatched_ontology(
    call_via_server, seeded_ontology, metric_agent
):
    args = _draft_args(seeded_ontology)
    args["context"]["ontology_id"] = "other-ontology"
    result = call_via_server("draft_task", args, "editor")
    assert result.is_error is True
    assert "不一致" in _body(result)["error"]


def test_validate_task_reruns_validation(
    call_via_server, seeded_ontology, metric_agent
):
    created = _body(
        call_via_server("draft_task", _draft_args(seeded_ontology), "editor")
    )
    task_id = created["data"]["task_id"]
    result = call_via_server("validate_task", {"task_id": task_id}, "editor")
    body = _body(result)
    assert result.is_error is False
    assert body["data"]["task_id"] == task_id
    assert body["data"]["status"] == ArtifactStatus.VALIDATED.value


def test_confirm_and_execute_are_publisher_only(call_via_server, monkeypatch):
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"mcp-auth-{uuid4().hex[:8]}",
            status=ArtifactStatus.VALIDATED.value,
            validation_report_json='{"blocking_count": 0}',
            spec_json="{}",
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id

    for role in ("reader", "editor"):
        result = call_via_server("confirm_task", {"task_id": task_id}, role)
        assert result.is_error is True
        assert _body(result)["metadata"]["denied"] is True

    confirmed = _body(
        call_via_server("confirm_task", {"task_id": task_id}, "publisher")
    )
    assert confirmed["data"]["status"] == ArtifactStatus.CONFIRMED.value
    monkeypatch.setattr(
        "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
        lambda _task_id: None,
    )
    for role in ("reader", "editor"):
        result = call_via_server("execute_task", {"task_id": task_id}, role)
        assert result.is_error is True
        assert _body(result)["metadata"]["denied"] is True
    result = call_via_server("execute_task", {"task_id": task_id}, "publisher")
    assert result.is_error is False
    assert _body(result)["data"]["status"] == ArtifactStatus.EXECUTING.value


def test_lifecycle_state_gates_are_enforced(call_via_server, monkeypatch):
    with SessionLocal() as db:
        drafted = GovernanceArtifact(
            kind="metric",
            name=f"mcp-gate-draft-{uuid4().hex[:8]}",
            status=ArtifactStatus.DRAFTED.value,
            spec_json="{}",
        )
        confirmed = GovernanceArtifact(
            kind="metric",
            name=f"mcp-gate-confirmed-{uuid4().hex[:8]}",
            status=ArtifactStatus.CONFIRMED.value,
            spec_json="{}",
        )
        db.add_all([drafted, confirmed])
        db.commit()
        drafted_id, confirmed_id = drafted.id, confirmed.id

    confirm = call_via_server("confirm_task", {"task_id": drafted_id}, "publisher")
    assert confirm.is_error is True
    assert "validated" in _body(confirm)["error"]

    execute = call_via_server("execute_task", {"task_id": drafted_id}, "publisher")
    assert execute.is_error is True
    assert "confirmed" in _body(execute)["error"]

    # A confirmed task is accepted by the execution gate; the worker dispatch is
    # replaced so this test does not contact Airflow.
    monkeypatch.setattr(
        "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
        lambda _task_id: None,
    )
    result = call_via_server("execute_task", {"task_id": confirmed_id}, "publisher")
    assert result.is_error is False
    assert _body(result)["data"]["status"] == ArtifactStatus.EXECUTING.value


def test_execute_task_returns_immediately_after_dispatch(call_via_server, monkeypatch):
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"mcp-async-{uuid4().hex[:8]}",
            status=ArtifactStatus.CONFIRMED.value,
            spec_json="{}",
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id

    seen = []
    monkeypatch.setattr(
        "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
        lambda artifact_id: seen.append(artifact_id),
    )
    started = time.monotonic()
    result = call_via_server("execute_task", {"task_id": task_id}, "publisher")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert result.is_error is False
    assert seen == [task_id]
    assert _body(result)["data"]["status"] == ArtifactStatus.EXECUTING.value


def test_claim_execution_is_atomic_and_idempotent():
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"mcp-claim-{uuid4().hex[:8]}",
            status=ArtifactStatus.CONFIRMED.value,
            spec_json="{}",
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id

        service = AgentPipelineService()
        first, claimed = service.claim_execution(db, task_id)
        second, claimed_again = service.claim_execution(db, task_id)
        assert claimed is True
        assert claimed_again is False
        assert first.status == second.status == ArtifactStatus.EXECUTING.value


def test_succeeded_execute_is_idempotent(call_via_server, monkeypatch):
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"mcp-done-{uuid4().hex[:8]}",
            status=ArtifactStatus.SUCCEEDED.value,
            spec_json="{}",
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id
    monkeypatch.setattr(
        "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
        lambda _task_id: pytest.fail("succeeded task must not spawn worker"),
    )
    result = call_via_server("execute_task", {"task_id": task_id}, "publisher")
    assert result.is_error is False
    assert _body(result)["data"]["already_succeeded"] is True


def test_query_objects_group_by_role_returns_distribution(
    call_via_server, seeded_ontology
):
    result = call_via_server(
        "query_objects",
        {"ontology_id": seeded_ontology, "group_by": "role"},
        "reader",
    )
    body = _body(result)
    assert result.is_error is False
    assert "objects" not in body["data"]
    assert body["data"]["by_role"]["business_object"] == 1
    assert body["data"]["by_role"]["technical"] == 1
