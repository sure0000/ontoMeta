"""最小 API 冒烟：health / 管理鉴权 / domains(mock) / external v1。"""

from __future__ import annotations


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["app"] == "ontoMeta"


def test_admin_auth_missing_token(client):
    res = client.get("/api/domains")
    assert res.status_code == 401
    assert "管理鉴权" in res.json()["detail"]


def test_admin_auth_wrong_token(client):
    res = client.get("/api/domains", headers={"X-Admin-Token": "wrong-token"})
    assert res.status_code == 401
    assert "无效" in res.json()["detail"]


def test_admin_auth_ok_and_list_domains_mock(client, admin_headers):
    res = client.get("/api/domains", headers=admin_headers)
    assert res.status_code == 200, res.text
    domains = res.json()
    assert isinstance(domains, list)
    assert len(domains) >= 1
    names = {d["name"] for d in domains}
    # MOCK_DOMAINS: 客户域 / 订单域 / 商品域
    assert "客户域" in names or any("域" in n for n in names)


def test_mcp_discovery_exempt_from_admin(client):
    res = client.get("/api/mcp")
    assert res.status_code == 200
    body = res.json()
    assert body.get("endpoint") == "/api/mcp"
    assert "auth" in body


def test_external_v1_requires_api_key(client):
    res = client.get("/api/v1/domains")
    assert res.status_code == 401
    assert "API Key" in res.json()["detail"]


def test_external_app_create_and_v1_domains(client, admin_headers):
    create = client.post(
        "/api/external-apps",
        headers=admin_headers,
        json={"name": "pytest-app", "description": "b3"},
    )
    assert create.status_code == 200, create.text
    created = create.json()
    assert created.get("api_key", "").startswith("om_sk_")
    api_key = created["api_key"]
    app_id = created["id"]

    # 再次 GET 不应回显明文
    detail = client.get(f"/api/external-apps/{app_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json().get("api_key") in (None, "")

    res = client.get("/api/v1/domains", headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)

    # 清理
    deleted = client.delete(f"/api/external-apps/{app_id}", headers=admin_headers)
    assert deleted.status_code == 200
