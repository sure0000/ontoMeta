"""B8 契约测试：MCP initialize / tools/call、Scope 403、限流 429、目录一致性。"""

from __future__ import annotations

import pytest

from app.services.external_rate_limit import external_rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    external_rate_limiter.reset()
    yield
    external_rate_limiter.reset()


def _create_app(client, admin_headers, **extra):
    body = {"name": "b8-app", "description": "contract", **extra}
    res = client.post("/api/external-apps", headers=admin_headers, json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_mcp_initialize_contract(client, admin_headers):
    created = _create_app(client, admin_headers)
    api_key = created["api_key"]
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    res = client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "ontometa"
    assert "tools" in result["capabilities"]

    client.delete(f"/api/external-apps/{created['id']}", headers=admin_headers)


def test_mcp_tools_call_list_domains(client, admin_headers):
    created = _create_app(client, admin_headers)
    api_key = created["api_key"]
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    res = client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_domains", "arguments": {}},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == 2
    result = body["result"]
    assert result.get("isError") is False
    assert isinstance(result.get("structuredContent"), list)

    # 调用日志可查
    logs = client.get(
        f"/api/external-apps/{created['id']}/call-logs",
        headers=admin_headers,
    )
    assert logs.status_code == 200
    assert any(row.get("tool_name") == "list_domains" for row in logs.json())

    client.delete(f"/api/external-apps/{created['id']}", headers=admin_headers)


def test_scope_denies_without_permission(client, admin_headers):
    created = _create_app(
        client,
        admin_headers,
        scopes=["domains:read"],
    )
    api_key = created["api_key"]
    headers = {"X-API-Key": api_key}

    ok = client.get("/api/v1/domains", headers=headers)
    assert ok.status_code == 200, ok.text

    denied = client.get("/api/v1/object-types", headers=headers)
    assert denied.status_code == 403, denied.text
    assert "objects:read" in denied.json()["detail"]

    mcp_denied = client.post(
        "/api/mcp/tools/call",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": "list_object_types", "arguments": {}},
    )
    assert mcp_denied.status_code == 403, mcp_denied.text

    client.delete(f"/api/external-apps/{created['id']}", headers=admin_headers)


def test_rate_limit_returns_429(client, admin_headers):
    created = _create_app(
        client,
        admin_headers,
        scopes=["domains:read"],
        rate_limit_per_minute=2,
    )
    api_key = created["api_key"]
    headers = {"X-API-Key": api_key}

    assert client.get("/api/v1/domains", headers=headers).status_code == 200
    assert client.get("/api/v1/domains", headers=headers).status_code == 200
    limited = client.get("/api/v1/domains", headers=headers)
    assert limited.status_code == 429, limited.text
    assert "频繁" in limited.json()["detail"]

    client.delete(f"/api/external-apps/{created['id']}", headers=admin_headers)


def test_mcp_catalog_matches_rest_directory(client, admin_headers):
    """MCP tools/list 与控制台 catalog 同源字段一致。"""
    created = _create_app(client, admin_headers)
    api_key = created["api_key"]

    catalog = client.get("/api/external-api/catalog", headers=admin_headers)
    assert catalog.status_code == 200
    catalog_items = catalog.json()
    assert len(catalog_items) >= 1

    mcp_list = client.get("/api/mcp/tools", headers={"X-API-Key": api_key})
    assert mcp_list.status_code == 200
    mcp_tools = mcp_list.json()["tools"]

    catalog_by_name = {item["tool_name"]: item for item in catalog_items}
    mcp_by_name = {t["name"]: t for t in mcp_tools}
    assert set(catalog_by_name) == set(mcp_by_name)

    for name, cat in catalog_by_name.items():
        mcp = mcp_by_name[name]
        assert cat["description"] == mcp["description"]
        assert cat["input_schema"] == mcp["inputSchema"]
        assert cat["required_scope"]
        assert cat.get("rest_path")
        assert mcp["annotations"]["requiredScope"] == cat["required_scope"]

    client.delete(f"/api/external-apps/{created['id']}", headers=admin_headers)
