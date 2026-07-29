"""M0 RBAC：角色 × 端点允许/拒绝矩阵、Token 轮换、向后兼容。

最关键的是 `test_backward_compatible_*`：RBAC 上线不得改变既有部署行为——
未创建任何 principal 时，共享 Admin Token 必须照常全权可用。
"""

from __future__ import annotations

import pytest

from app.auth import required_role
from app.database import SessionLocal
from app.models import Principal, role_satisfies


def _make(client, admin_headers, name: str, role: str) -> dict:
    resp = client.post(
        "/api/principals", headers=admin_headers, json={"name": name, "role": role}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"id": body["id"], "headers": {"X-Admin-Token": body["token"]}}


# ---------- 角色序 ----------


@pytest.mark.parametrize(
    "actual,minimum,ok",
    [
        ("reader", "reader", True),
        ("reader", "editor", False),
        ("editor", "reader", True),
        ("editor", "reviewer", False),
        ("reviewer", "editor", True),
        ("reviewer", "publisher", False),
        ("publisher", "publisher", True),
        ("publisher", "reader", True),
        (None, "reader", False),
        ("bogus", "reader", False),
    ],
)
def test_role_ordering(actual, minimum, ok):
    assert role_satisfies(actual, minimum) is ok


# ---------- 策略表 ----------


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/domains", "reader"),
        ("GET", "/api/object-types/x", "reader"),
        ("POST", "/api/domains/x/generate-draft", "editor"),
        ("PATCH", "/api/object-types/x", "editor"),
        # 删除不可逆 → publisher
        ("DELETE", "/api/business-logics/x", "publisher"),
        # 复核类
        ("GET", "/api/confirmations", "reviewer"),
        ("POST", "/api/confirmations/x/confirm", "reviewer"),
        ("PATCH", "/api/fields/pin", "reviewer"),
        ("POST", "/api/ontologies/x/conflicts/resolve-all", "reviewer"),
        # 发布 / 执行 / 设置 / 主体自管理 → publisher
        ("POST", "/api/ontologies/x/publish", "publisher"),
        ("PATCH", "/api/object-types/x/pre-publish", "publisher"),
        ("POST", "/api/chat-bi/messages/x/execute", "publisher"),
        ("POST", "/api/data-apps/x/share", "publisher"),
        ("PATCH", "/api/settings/llm", "publisher"),
        ("GET", "/api/principals", "publisher"),
        ("POST", "/api/principals", "publisher"),
    ],
)
def test_required_role_policy(method, path, expected):
    assert required_role(method, path) == expected


def test_unknown_method_fails_closed():
    """未知方法必须按最高权限处理，不能放行。"""
    assert required_role("TRACE", "/api/domains") == "publisher"


def test_new_endpoint_is_covered_by_default():
    """新增端点无需改策略表即被覆盖——避免漏挂依赖造成静默失守。"""
    assert required_role("POST", "/api/some-brand-new-thing") == "editor"
    assert required_role("DELETE", "/api/some-brand-new-thing") == "publisher"


# ---------- 向后兼容 ----------


def test_backward_compatible_admin_token_is_superuser(client, admin_headers):
    """共享 Admin Token 等价 publisher，且不查库。"""
    assert client.get("/api/domains", headers=admin_headers).status_code == 200
    assert client.get("/api/principals", headers=admin_headers).status_code == 200


def test_backward_compatible_no_principals_unchanged(client, admin_headers):
    with SessionLocal() as db:
        db.query(Principal).delete()
        db.commit()
    assert client.get("/api/domains", headers=admin_headers).status_code == 200


def test_missing_token_still_401(client):
    assert client.get("/api/domains").status_code == 401


def test_invalid_token_still_401(client):
    resp = client.get("/api/domains", headers={"X-Admin-Token": "nope"})
    assert resp.status_code == 401


# ---------- 角色 × 端点矩阵 ----------


def test_reader_can_read_but_not_write(client, admin_headers):
    p = _make(client, admin_headers, "读者", "reader")
    assert client.get("/api/domains", headers=p["headers"]).status_code == 200
    resp = client.patch(
        "/api/object-types/whatever", headers=p["headers"], json={"display_name": "x"}
    )
    assert resp.status_code == 403
    assert "需要 editor 角色" in resp.json()["detail"]


def test_editor_can_write_but_not_review_or_publish(client, admin_headers):
    p = _make(client, admin_headers, "编辑", "editor")
    # 读写放行（404 表示已过鉴权、只是资源不存在）
    assert client.get("/api/domains", headers=p["headers"]).status_code == 200
    assert client.patch(
        "/api/object-types/nope", headers=p["headers"], json={"display_name": "x"}
    ).status_code != 403
    # 复核与发布被拒
    assert client.get("/api/confirmations", headers=p["headers"]).status_code == 403
    assert client.post(
        "/api/ontologies/x/publish", headers=p["headers"], json={}
    ).status_code == 403


def test_reviewer_can_review_but_not_publish(client, admin_headers):
    p = _make(client, admin_headers, "复核", "reviewer")
    assert client.get("/api/confirmations", headers=p["headers"]).status_code != 403
    assert client.post(
        "/api/ontologies/x/publish", headers=p["headers"], json={}
    ).status_code == 403
    # 删除也属 publisher
    assert client.delete(
        "/api/business-logics/x", headers=p["headers"]
    ).status_code == 403


def test_publisher_passes_everything(client, admin_headers):
    p = _make(client, admin_headers, "发布", "publisher")
    for method, path in [
        ("GET", "/api/domains"),
        ("GET", "/api/confirmations"),
        ("GET", "/api/principals"),
    ]:
        resp = client.request(method, path, headers=p["headers"])
        assert resp.status_code != 403, f"{method} {path} 不应被拒"


def test_non_publisher_cannot_self_escalate(client, admin_headers):
    """低权角色不得读写主体表，否则可自我提权。"""
    p = _make(client, admin_headers, "想提权的编辑", "editor")
    assert client.get("/api/principals", headers=p["headers"]).status_code == 403
    assert client.post(
        "/api/principals", headers=p["headers"], json={"name": "x", "role": "publisher"}
    ).status_code == 403


# ---------- 主体管理 ----------


def test_token_returned_once_and_stored_hashed(client, admin_headers):
    resp = client.post(
        "/api/principals", headers=admin_headers, json={"name": "一次性", "role": "reader"}
    )
    body = resp.json()
    token = body["token"]
    assert token.startswith("om_pr_")
    assert body["token_prefix"] == token[:12]

    # 列表接口不得再返回明文
    listed = client.get("/api/principals", headers=admin_headers).json()
    row = next(r for r in listed if r["id"] == body["id"])
    assert "token" not in row

    with SessionLocal() as db:
        stored = db.get(Principal, body["id"])
        assert stored.token_hash != token
        assert len(stored.token_hash) == 64  # sha256 hex


def test_rotate_token_invalidates_old(client, admin_headers):
    p = _make(client, admin_headers, "轮换", "reader")
    assert client.get("/api/domains", headers=p["headers"]).status_code == 200

    rotated = client.post(
        f"/api/principals/{p['id']}/rotate-token", headers=admin_headers
    ).json()
    new_headers = {"X-Admin-Token": rotated["token"]}

    assert client.get("/api/domains", headers=p["headers"]).status_code == 401
    assert client.get("/api/domains", headers=new_headers).status_code == 200


def test_deactivated_principal_rejected(client, admin_headers):
    p = _make(client, admin_headers, "停用", "reader")
    client.patch(f"/api/principals/{p['id']}", headers=admin_headers, json={"active": False})
    assert client.get("/api/domains", headers=p["headers"]).status_code == 401


def test_role_change_takes_effect(client, admin_headers):
    p = _make(client, admin_headers, "提升", "reader")
    assert client.patch(
        "/api/object-types/nope", headers=p["headers"], json={"display_name": "x"}
    ).status_code == 403
    client.patch(f"/api/principals/{p['id']}", headers=admin_headers, json={"role": "editor"})
    assert client.patch(
        "/api/object-types/nope", headers=p["headers"], json={"display_name": "x"}
    ).status_code != 403


def test_invalid_role_rejected(client, admin_headers):
    resp = client.post(
        "/api/principals", headers=admin_headers, json={"name": "x", "role": "root"}
    )
    assert resp.status_code == 400


def test_delete_principal(client, admin_headers):
    p = _make(client, admin_headers, "待删", "reader")
    assert client.delete(f"/api/principals/{p['id']}", headers=admin_headers).status_code == 200
    assert client.get("/api/domains", headers=p["headers"]).status_code == 401


def test_policy_endpoint_exposes_matrix(client, admin_headers):
    body = client.get("/api/principals-policy", headers=admin_headers).json()
    assert body["roles"] == ["reader", "editor", "reviewer", "publisher"]
    assert body["method_defaults"]["DELETE"] == "publisher"
    assert any(o["minimum_role"] == "reviewer" for o in body["overrides"])


# ---------- 与 M4 的衔接 ----------


def test_chat_bi_execute_requires_publisher(client, admin_headers):
    """M4 留下的权限缺口在此闭合：执行 SQL 直接打物理源，必须 publisher。"""
    editor = _make(client, admin_headers, "编辑不许执行", "editor")
    resp = client.post(
        "/api/chat-bi/messages/x/execute",
        headers=editor["headers"],
        json={"data_source_id": "y"},
    )
    assert resp.status_code == 403
