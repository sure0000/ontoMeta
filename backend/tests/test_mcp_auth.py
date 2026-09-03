"""MCP Phase 3：认证、授权、审计。

三条要钉住的不变量：
1. **身份来自 Token**，与 REST 同一份判定（``resolve_principal_token``）——admin token
   → publisher，Principal Token → 该主体角色，无 Token → 匿名默认角色。
2. **授权 fail-closed 且集中在服务器层**：工具的 ``execute`` 不各写鉴权，服务器按
   ``required_role`` 统一拦；缺角色一律拒。特别是 ``execute_sql`` 不能比 Data Agent
   的 run_sql 更松。
3. **每次调用都留审计**（成功/失败/被拒），凭据脱敏，append-only。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

import mcp.types as types
from app.config import settings
from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.auth import resolve_auth_context
from app.mcp.tools import AuthContext, TOOL_REGISTRY, tool_required_role
from app.models.mcp_audit import McpAuditLog
from app.models.principal import Principal
from app.services.principal_service import PrincipalService

_principals = PrincipalService()


@pytest.fixture(autouse=True)
def _clean_session_identity():
    """每个用例后清掉缓存的会话身份——它是进程级全局，泄漏会污染后面的用例。"""
    yield
    mcp_server._reset_session_auth()


@pytest.fixture
def call_via_server(monkeypatch):
    """以指定角色跑一次服务器层调用（含授权闸门 + 审计），返回 CallToolResult。"""

    def _run(tool_name: str, arguments: dict, *, auth: AuthContext):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(mcp_server, "resolve_auth_context", lambda: auth)
        params = types.CallToolRequestParams(name=tool_name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _run


def _ctx(role: str | None, principal_id: str | None = None) -> AuthContext:
    return AuthContext(client_type="mcp_local", role=role, principal_id=principal_id)


# --------------------------------------------------------------------------
# 认证：Token → 角色
# --------------------------------------------------------------------------


def test_no_token_falls_back_to_default_role(monkeypatch):
    monkeypatch.delenv("ONTOMETA_MCP_TOKEN", raising=False)
    monkeypatch.setattr(settings, "ontometa_mcp_token", None)
    monkeypatch.setattr(settings, "mcp_default_role", "reader")
    auth = resolve_auth_context()
    assert auth.role == "reader"
    assert auth.principal_id is None
    assert auth.client_type == "mcp_local"


def test_empty_default_role_means_no_identity(monkeypatch):
    monkeypatch.delenv("ONTOMETA_MCP_TOKEN", raising=False)
    monkeypatch.setattr(settings, "ontometa_mcp_token", None)
    monkeypatch.setattr(settings, "mcp_default_role", "")
    auth = resolve_auth_context()
    assert auth.role is None
    assert auth.is_authenticated is False


def test_admin_token_is_publisher(monkeypatch):
    monkeypatch.setattr(settings, "ontometa_admin_token", "the-admin-token")
    monkeypatch.setenv("ONTOMETA_MCP_TOKEN", "the-admin-token")
    auth = resolve_auth_context()
    assert auth.role == "publisher"
    assert auth.principal_id is None  # superuser 不查库


def test_principal_token_resolves_to_its_role(monkeypatch, db):
    _p, token = _principals.create(
        db, name=f"mcp-editor-{uuid4().hex[:6]}", role="editor"
    )
    monkeypatch.setattr(settings, "ontometa_admin_token", "unrelated-admin")
    monkeypatch.setenv("ONTOMETA_MCP_TOKEN", token)
    auth = resolve_auth_context()
    assert auth.role == "editor"
    assert auth.principal_id is not None
    assert auth.principal_name and auth.principal_name.startswith("mcp-editor")


def test_unknown_token_is_anonymous(monkeypatch):
    monkeypatch.setattr(settings, "ontometa_admin_token", "the-admin-token")
    monkeypatch.setattr(settings, "mcp_default_role", "reader")
    monkeypatch.setenv("ONTOMETA_MCP_TOKEN", "om_pr_not-a-real-token")
    auth = resolve_auth_context()
    assert auth.role == "reader"
    assert auth.principal_id is None


# --------------------------------------------------------------------------
# 授权：required_role 集中强制，fail-closed
# --------------------------------------------------------------------------


def test_execute_sql_matches_data_agent_min_role():
    """MCP 代跑 SQL 不能比 Data Agent 的 run_sql 更松。"""
    assert tool_required_role(TOOL_REGISTRY["execute_sql"]) == settings.agent_run_sql_min_role


def test_read_tools_default_to_reader():
    for name in ("query_ontology", "query_objects", "list_tasks", "validate_sql"):
        assert tool_required_role(TOOL_REGISTRY[name]) == "reader"


def test_reader_denied_execute_sql(call_via_server):
    result = call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx("reader"))
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["metadata"]["denied"] is True
    assert payload["metadata"]["required_role"] == "publisher"


def test_reader_denied_proposals(call_via_server):
    result = call_via_server(
        "propose_sync",
        {"intent": "x", "context": {"ontology_id": "y"}},
        auth=_ctx("reader"),
    )
    assert result.is_error is True
    assert json.loads(result.content[0].text)["metadata"]["denied"] is True


def test_no_identity_denied_even_read(call_via_server):
    """匿名（role=None，锁定部署）连只读也拿不到——fail-closed。"""
    result = call_via_server(
        "query_ontology", {"include_unpublished": True}, auth=_ctx(None)
    )
    assert result.is_error is True
    assert json.loads(result.content[0].text)["metadata"]["denied"] is True


def test_reader_allowed_read_tool(call_via_server):
    result = call_via_server(
        "query_ontology", {"include_unpublished": True}, auth=_ctx("reader")
    )
    assert result.is_error is False
    assert json.loads(result.content[0].text)["success"] is True


def test_editor_allowed_proposal_but_not_sql(call_via_server, seeded_ontology_for_auth):
    """editor 能提案，但代跑 SQL 仍被拦（publisher）。"""
    ontology_id = seeded_ontology_for_auth
    denied = call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx("editor"))
    assert denied.is_error is True
    assert json.loads(denied.content[0].text)["metadata"]["denied"] is True

    # 提案缺参会因业务原因失败，但**不是**授权拒绝——授权闸门已放行。
    proposal = call_via_server(
        "propose_sync",
        {"intent": "同步", "context": {"ontology_id": ontology_id}},
        auth=_ctx("editor"),
    )
    body = json.loads(proposal.content[0].text)
    assert body["metadata"].get("denied") is not True


def test_publisher_allowed_execute_sql_reaches_tool(call_via_server, monkeypatch):
    """publisher 过授权闸门；无默认仓时工具自身 fail-closed（业务失败，非授权拒绝）。"""
    with SessionLocal() as db:
        from app.models.data_app import DataSource

        has_wh = (
            db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).first()
            is not None
        )
    result = call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx("publisher"))
    body = json.loads(result.content[0].text)
    assert body["metadata"].get("denied") is not True
    if not has_wh:
        assert body["success"] is False
        assert "默认 Doris" in body["error"]


# --------------------------------------------------------------------------
# 审计：每次调用都留痕，脱敏，append-only
# --------------------------------------------------------------------------


def _audit_rows(tool_name: str) -> list[McpAuditLog]:
    with SessionLocal() as db:
        return (
            db.query(McpAuditLog)
            .filter(McpAuditLog.tool_name == tool_name)
            .order_by(McpAuditLog.created_at.desc())
            .all()
        )


def test_denied_call_is_audited(call_via_server):
    marker = f"SELECT '{uuid4().hex}'"
    call_via_server("execute_sql", {"sql": marker}, auth=_ctx("reader"))
    row = next(
        r for r in _audit_rows("execute_sql") if marker in (r.arguments_json or "")
    )
    assert row.denied is True
    assert row.success is False
    assert row.principal_role == "reader"
    assert row.duration_ms is not None


def test_successful_call_is_audited(call_via_server):
    marker = f"SELECT '{uuid4().hex}'"
    call_via_server("validate_sql", {"sql": marker}, auth=_ctx("reader"))
    row = next(
        r for r in _audit_rows("validate_sql") if marker in (r.arguments_json or "")
    )
    assert row.success is True
    assert row.denied is False


def test_audit_redacts_credential_arguments(call_via_server):
    # 唯一标记：SQLite created_at 只有秒级精度，靠时间排序取不准「本次那条」。
    marker = f"SELECT '{uuid4().hex}'"
    call_via_server(
        "validate_sql",
        {"sql": marker, "password": "hunter2", "token": "om_pr_secret"},
        auth=_ctx("reader"),
    )
    row = next(
        r for r in _audit_rows("validate_sql") if marker in (r.arguments_json or "")
    )
    assert "hunter2" not in (row.arguments_json or "")
    assert "om_pr_secret" not in (row.arguments_json or "")
    args = json.loads(row.arguments_json)
    assert args["password"] == "***"
    assert args["token"] == "***"
    assert args["sql"] == marker  # 非敏感参数照留


def test_list_audit_logs_requires_publisher(call_via_server):
    denied = call_via_server("list_audit_logs", {}, auth=_ctx("editor"))
    assert json.loads(denied.content[0].text)["metadata"]["denied"] is True

    allowed = call_via_server("list_audit_logs", {"limit": 5}, auth=_ctx("publisher"))
    body = json.loads(allowed.content[0].text)
    assert body["success"] is True
    assert "logs" in body["data"]


@pytest.fixture
def seeded_ontology_for_auth():
    """一个最小本体，够 propose_sync 的授权闸门放行后走到业务校验。"""
    from app.models import DomainContext, Ontology, OntologyStatus

    suffix = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"auth-domain-{suffix}", name=f"鉴权测试域 {suffix}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, version=1, status=OntologyStatus.DRAFT.value
        )
        db.add(ontology)
        db.commit()
        return ontology.id
