"""MCP Phase 4：限流 + 运维自省/监控。

要钉住的：
1. 限流是**滑动窗口**、只对放行的调用计数，execute_sql 单独更严；超限拒绝且审计去重。
2. 限流在服务器层生效，命中回 ``rate_limited`` 而非 ``denied``（两种拦截语义不同）。
3. ``server_info`` 如实报当前身份与限流配置；``get_mcp_stats`` 聚合审计且 publisher 门控。
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
from app.mcp.rate_limit import check_rate_limit, reset_rate_limit
from app.mcp.tools import AuthContext
from app.models.mcp_audit import McpAuditLog
from app.services.settings_service import SettingsService


@pytest.fixture(autouse=True)
def _clean_rate_and_identity(db):
    service = SettingsService()
    original = service.get_mcp_settings(db)
    reset_rate_limit()
    yield
    service.update_mcp_settings(db, original)
    reset_rate_limit()
    mcp_server._reset_session_auth()


def _configure_mcp(db, **values):
    SettingsService().update_mcp_settings(db, values)


def _ctx(role: str | None) -> AuthContext:
    return AuthContext(client_type="mcp_local", role=role)


@pytest.fixture
def call_via_server(monkeypatch):
    def _run(tool_name: str, arguments: dict, *, auth: AuthContext):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(mcp_server, "resolve_auth_context", lambda: auth)
        params = types.CallToolRequestParams(name=tool_name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _run


# --------------------------------------------------------------------------
# 限流器单元
# --------------------------------------------------------------------------


def test_sliding_window_admits_up_to_limit(db):
    _configure_mcp(db, mcp_rate_limit_per_minute=3)
    reset_rate_limit()
    assert check_rate_limit("t", now=0.0)["allowed"] is True
    assert check_rate_limit("t", now=0.1)["allowed"] is True
    assert check_rate_limit("t", now=0.2)["allowed"] is True
    # 第 4 次在同一分钟内 → 拒绝
    v = check_rate_limit("t", now=0.3)
    assert v["allowed"] is False
    assert v["limit"] == 3
    assert v["retry_after"] > 0


def test_window_slides_after_60s(db):
    _configure_mcp(db, mcp_rate_limit_per_minute=1)
    reset_rate_limit()
    assert check_rate_limit("t", now=0.0)["allowed"] is True
    assert check_rate_limit("t", now=10.0)["allowed"] is False
    # 第一次调用（now=0）在 now=61 时已滑出 60s 窗口 → 重新放行
    assert check_rate_limit("t", now=61.0)["allowed"] is True


def test_denied_calls_do_not_consume_window(db):
    """被限流拒绝的调用不计入窗口，否则窗口永远满、永久封锁。"""
    _configure_mcp(db, mcp_rate_limit_per_minute=1)
    reset_rate_limit()
    assert check_rate_limit("t", now=0.0)["allowed"] is True
    # 连撞若干次拒绝
    for i in range(5):
        assert check_rate_limit("t", now=1.0 + i)["allowed"] is False
    # 放行那次（now=0）在 now=61 滑出 → 恢复，而不是被那 5 次拒绝续命
    assert check_rate_limit("t", now=61.0)["allowed"] is True


def test_execute_sql_has_its_own_stricter_limit(db):
    _configure_mcp(db, mcp_rate_limit_per_minute=100, mcp_execute_sql_rate_limit_per_minute=1)
    reset_rate_limit()
    assert check_rate_limit("execute_sql", now=0.0)["allowed"] is True
    assert check_rate_limit("execute_sql", now=0.1)["allowed"] is False
    # 别的工具仍走 100 的上限，不受 execute_sql 影响
    assert check_rate_limit("query_ontology", now=0.2)["allowed"] is True


def test_rate_limit_audit_dedup(db):
    """疯狂撞限流时，同一工具每分钟至多写一条审计。"""
    _configure_mcp(db, mcp_rate_limit_per_minute=1)
    reset_rate_limit()
    check_rate_limit("t", now=0.0)  # 放行
    first = check_rate_limit("t", now=1.0)
    second = check_rate_limit("t", now=2.0)
    assert first["allowed"] is False and first["should_audit"] is True
    assert second["allowed"] is False and second["should_audit"] is False


def test_zero_disables_rate_limit(db):
    _configure_mcp(db, mcp_rate_limit_per_minute=0, mcp_execute_sql_rate_limit_per_minute=0)
    reset_rate_limit()
    for i in range(50):
        assert check_rate_limit("t", now=float(i) * 0.001)["allowed"] is True


# --------------------------------------------------------------------------
# 服务器层限流
# --------------------------------------------------------------------------


def test_server_rate_limits_and_marks_it(call_via_server, db):
    _configure_mcp(db, mcp_rate_limit_per_minute=2)
    reset_rate_limit()
    ok1 = call_via_server("validate_sql", {"sql": "SELECT 1"}, auth=_ctx("reader"))
    ok2 = call_via_server("validate_sql", {"sql": "SELECT 2"}, auth=_ctx("reader"))
    limited = call_via_server("validate_sql", {"sql": "SELECT 3"}, auth=_ctx("reader"))

    assert json.loads(ok1.content[0].text)["metadata"].get("rate_limited") is not True
    assert json.loads(ok2.content[0].text)["metadata"].get("rate_limited") is not True
    body = json.loads(limited.content[0].text)
    assert limited.is_error is True
    assert body["metadata"]["rate_limited"] is True
    assert body["metadata"]["limit_per_minute"] == 2
    assert "retry_after_seconds" in body["metadata"]


def test_rate_limit_precedes_authorization(call_via_server, db):
    """限流在授权之前：一个连 reader 都没有的身份猛刷，也先被限流挡住（不逐次刷审计/DB）。"""
    _configure_mcp(db, mcp_rate_limit_per_minute=1, mcp_execute_sql_rate_limit_per_minute=1)
    reset_rate_limit()
    first = call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx(None))
    second = call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx(None))
    # 第一次放行进窗口、到授权闸门被 denied；第二次先被限流挡下（没走到授权）
    assert json.loads(first.content[0].text)["metadata"].get("denied") is True
    assert json.loads(second.content[0].text)["metadata"].get("rate_limited") is True


def test_rate_limited_call_is_audited_once(call_via_server, db):
    _configure_mcp(db, mcp_rate_limit_per_minute=1)
    reset_rate_limit()
    marker = f"SELECT '{uuid4().hex}'"
    call_via_server("validate_sql", {"sql": marker}, auth=_ctx("reader"))  # 放行
    call_via_server("validate_sql", {"sql": marker}, auth=_ctx("reader"))  # 限流
    call_via_server("validate_sql", {"sql": marker}, auth=_ctx("reader"))  # 限流（去重，不再写）
    with SessionLocal() as db:
        limited_rows = (
            db.query(McpAuditLog)
            .filter(
                McpAuditLog.tool_name == "validate_sql",
                McpAuditLog.error.like("RATE_LIMITED:%"),
                McpAuditLog.arguments_json.like(f"%{marker}%"),
            )
            .all()
        )
    assert len(limited_rows) == 1  # 三次调用只落一条限流审计


# --------------------------------------------------------------------------
# server_info
# --------------------------------------------------------------------------


def test_server_info_reports_identity_and_limits(call_via_server, db):
    _configure_mcp(db, mcp_rate_limit_per_minute=77)
    result = call_via_server("server_info", {}, auth=_ctx("editor"))
    assert result.is_error is False
    data = json.loads(result.content[0].text)["data"]
    assert data["identity"]["role"] == "editor"
    assert data["tool_count"] >= 16
    assert data["rate_limit"]["default_per_minute"] == 77
    assert data["audit"]["reachable"] is True
    # 默认只回「工具名 → 最低角色」：完整描述在调用方自己的工具清单里已经有一份，
    # 这里再抄一遍等于同一段文本付两次上下文（实测 8.1KB）。自查「为什么被拒」
    # 要看的就是角色这一列。
    roles = data["tool_roles"]
    assert {"execute_sql", "server_info", "get_mcp_stats"} <= set(roles)
    assert roles["execute_sql"] == settings.agent_run_sql_min_role
    assert "tools" not in data
    assert "verbose=true" in data["tools_note"]


def test_server_info_verbose_returns_full_descriptions(call_via_server):
    """全文没被删掉，只是改成显式索取。"""
    result = call_via_server("server_info", {"verbose": True}, auth=_ctx("reader"))
    data = json.loads(result.content[0].text)["data"]
    tools = {t["name"]: t for t in data["tools"]}
    assert "execute_sql" in tools
    assert tools["execute_sql"]["description"]
    assert "tool_roles" not in data


def test_server_info_is_reader_accessible(call_via_server):
    result = call_via_server("server_info", {}, auth=_ctx("reader"))
    assert result.is_error is False


# --------------------------------------------------------------------------
# get_mcp_stats
# --------------------------------------------------------------------------


def test_get_mcp_stats_requires_publisher(call_via_server):
    denied = call_via_server("get_mcp_stats", {}, auth=_ctx("editor"))
    assert json.loads(denied.content[0].text)["metadata"]["denied"] is True


def test_get_mcp_stats_aggregates_audit(call_via_server):
    # 制造已知的审计事件：一次成功、一次被拒。
    call_via_server("validate_sql", {"sql": "SELECT 1"}, auth=_ctx("reader"))
    call_via_server("execute_sql", {"sql": "SELECT 1"}, auth=_ctx("reader"))  # denied

    result = call_via_server("get_mcp_stats", {}, auth=_ctx("publisher"))
    assert result.is_error is False
    data = json.loads(result.content[0].text)["data"]
    assert data["totals"]["calls"] >= 2
    assert data["totals"]["denied"] >= 1
    tool_names = {row["tool_name"] for row in data["by_tool"]}
    assert "validate_sql" in tool_names
    roles = {row["role"] for row in data["by_role"]}
    assert "reader" in roles


def test_get_mcp_stats_includes_diagnostics(client, admin_headers):
    body = client.get("/api/mcp/stats?window_minutes=1440", headers=admin_headers).json()
    assert {"error_rate", "average_duration_ms", "p95_duration_ms"} <= set(body["totals"])
    assert {"timeline", "error_groups", "unique_principals", "last_call_at"} <= set(body)
