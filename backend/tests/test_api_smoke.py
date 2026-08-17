"""最小 API 冒烟：health / 管理鉴权 / domains。"""

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


def test_admin_auth_ok_and_list_domains(client, admin_headers):
    # 无真实 DataHub 时，sync_domains 内部容错回退本地缓存，端点仍返回 200 + list。
    res = client.get("/api/domains", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)
