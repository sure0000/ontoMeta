"""MCP Phase 5：远程 HTTP 传输鉴权 + 匿名拦截 + 前端管理 REST。

要钉住的：
1. 远程身份**逐请求**从 Authorization 头解析（与 stdio 的一进程一身份互不干扰），与
   REST/stdio 共用 resolve_principal_token。
2. 公网默认不匿名：无令牌的 HTTP 请求被 _AnonymousGuardASGI 401（除非显式允许匿名）。
3. /api/mcp/info 只读可达（reader）；/api/mcp/audit、/stats 需 publisher。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.config import settings
from app.database import SessionLocal
from app.mcp.auth import resolve_http_auth
from app.mcp.http_app import _AnonymousGuardASGI
from app.services.principal_service import PrincipalService
from app.services.settings_service import SettingsService

_principals = PrincipalService()


@pytest.fixture(autouse=True)
def _restore_mcp_settings(db):
    service = SettingsService()
    original = service.get_mcp_settings(db)
    yield
    service.update_mcp_settings(db, original)


def _configure_mcp(db, **values):
    SettingsService().update_mcp_settings(db, values)


class _FakeHeaders:
    """最小 Starlette-Headers 替身：大小写不敏感的 get。"""

    def __init__(self, mapping: dict[str, str]):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key, default=None):
        return self._m.get(key.lower(), default)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = _FakeHeaders(headers)


# --------------------------------------------------------------------------
# 逐请求 HTTP 鉴权
# --------------------------------------------------------------------------


def test_http_auth_admin_token_is_publisher(monkeypatch):
    monkeypatch.setattr(settings, "ontometa_admin_token", "admin-xyz")
    auth = resolve_http_auth(_FakeRequest({"Authorization": "Bearer admin-xyz"}))
    assert auth.role == "publisher"
    assert auth.client_type == "mcp_remote"
    assert auth.principal_id is None


def test_http_auth_principal_token(monkeypatch, db):
    _p, token = _principals.create(db, name=f"http-editor-{uuid4().hex[:6]}", role="editor")
    monkeypatch.setattr(settings, "ontometa_admin_token", "unrelated")
    auth = resolve_http_auth(_FakeRequest({"authorization": f"Bearer {token}"}))
    assert auth.role == "editor"
    assert auth.principal_id is not None
    assert auth.client_type == "mcp_remote"


def test_http_auth_no_token_denied_by_default(db):
    _configure_mcp(db, mcp_http_allow_anonymous=False)
    auth = resolve_http_auth(_FakeRequest({}))
    assert auth.role is None  # 无身份 → 授权闸门 fail-closed


def test_http_auth_no_token_remains_denied_even_if_legacy_flag_is_set(db):
    _configure_mcp(db, mcp_http_allow_anonymous=True, mcp_default_role="reader")
    auth = resolve_http_auth(_FakeRequest({}))
    assert auth.role is None


def test_http_auth_bare_token_without_bearer_prefix(monkeypatch):
    monkeypatch.setattr(settings, "ontometa_admin_token", "raw-tok")
    auth = resolve_http_auth(_FakeRequest({"Authorization": "raw-tok"}))
    assert auth.role == "publisher"


# --------------------------------------------------------------------------
# 匿名拦截 ASGI（无令牌 401）
# --------------------------------------------------------------------------


def _run_guard(headers: list[tuple[bytes, bytes]]):
    """驱动 _AnonymousGuardASGI 一次，返回 (inner_called, sent_messages)。"""
    inner_called = {"v": False}

    async def inner(scope, receive, send):
        inner_called["v"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    guard = _AnonymousGuardASGI(inner)
    scope = {"type": "http", "method": "POST", "headers": headers}
    asyncio.run(guard(scope, receive, send))
    return inner_called["v"], sent


def test_guard_401s_anonymous_when_disallowed(db):
    _configure_mcp(db, mcp_http_enabled=True, mcp_http_allow_anonymous=False)
    called, sent = _run_guard(headers=[])
    assert called is False  # 没进到内层
    assert sent[0]["status"] == 401
    assert any(k == b"www-authenticate" for k, _ in sent[0]["headers"])


def test_guard_passes_with_token(db):
    _configure_mcp(db, mcp_http_enabled=True, mcp_http_allow_anonymous=False)
    called, sent = _run_guard(headers=[(b"authorization", b"Bearer whatever")])
    assert called is True  # 有令牌就放行到内层（角色由 handler 精细判定）
    assert sent[0]["status"] == 200


def test_guard_rejects_anonymous_even_if_legacy_flag_is_set(db):
    _configure_mcp(db, mcp_http_enabled=True, mcp_http_allow_anonymous=True)
    called, sent = _run_guard(headers=[])
    assert called is False
    assert sent[0]["status"] == 401


# --------------------------------------------------------------------------
# 前端管理 REST：/api/mcp/*
# --------------------------------------------------------------------------


def test_rest_info_returns_catalog(client, admin_headers):
    r = client.get("/api/mcp/info", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tool_count"] >= 16
    names = {t["name"] for t in data["tools"]}
    assert {"execute_sql", "server_info", "get_mcp_stats"} <= names
    assert "http" in data["transports"]
    assert data["audit"]["reachable"] is True


def test_rest_stats_and_audit_ok_for_admin(client, admin_headers):
    assert client.get("/api/mcp/stats", headers=admin_headers).status_code == 200
    r = client.get("/api/mcp/audit?limit=5", headers=admin_headers)
    assert r.status_code == 200
    assert "logs" in r.json()


@pytest.fixture
def reader_headers(db):
    """一个 reader 主体的令牌头。用完删。"""
    p, token = _principals.create(db, name=f"rest-reader-{uuid4().hex[:6]}", role="reader")
    pid = p.id
    yield {"X-Admin-Token": token}
    with SessionLocal() as s:
        _principals.delete(s, pid)


def test_rest_audit_requires_publisher(client, reader_headers):
    """审计含每次调用的主体与入参 — reader 不得读。"""
    assert client.get("/api/mcp/audit", headers=reader_headers).status_code == 403
    assert client.get("/api/mcp/stats", headers=reader_headers).status_code == 403


def test_rest_info_readable_by_reader(client, reader_headers):
    # 工具清单/连接信息不敏感，reader 可读。
    assert client.get("/api/mcp/info", headers=reader_headers).status_code == 200
