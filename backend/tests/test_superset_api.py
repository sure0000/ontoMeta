"""Superset 管理 REST：没配好不等于报错、凭据不出网、解除登记不删外部对象。

三条是这层特有的：

1. ``/superset/status`` 与 ``/superset/assets`` 在**没配 Superset 时也要正常返回**——
   页面要能显示「还没接」，而不是弹一个红色报错让人以为坏了；
2. 任何响应都不得带出账号密码；guest token 只能由后端换取，浏览器不持凭据；
3. ``DELETE`` 只解除登记，不删 Superset 里的图/看板。
"""

from __future__ import annotations

import pytest

from app.models import SupersetAsset
from app.services import superset_service as svc
from app.services.settings_service import SupersetRuntimeConfig

CFG = SupersetRuntimeConfig(
    base_url="http://superset:8088",
    username="admin",
    password="s3cret",
    database_id=3,
    public_base_url="https://bi.example.com",
    enabled=True,
)


@pytest.fixture
def asset(db):
    row = svc.register_asset(
        db,
        asset_type="dashboard",
        superset_id=11,
        title="销售看板",
        url_path="/superset/dashboard/11/",
        embedded_uuid="uuid-11",
    )
    yield row
    db.expire_all()
    for r in db.query(SupersetAsset).all():
        db.delete(r)
    db.commit()


def test_status_says_not_configured_instead_of_failing(client, admin_headers, monkeypatch):
    monkeypatch.setattr(
        svc, "runtime", lambda db: (_ for _ in ()).throw(svc.SupersetNotConfigured("未启用"))
    )
    resp = client.get("/api/superset/status", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert "未启用" in body["reason"]


def test_status_never_leaks_credentials(client, admin_headers, monkeypatch):
    monkeypatch.setattr(svc, "runtime", lambda db: CFG)
    body = client.get("/api/superset/status", headers=admin_headers).json()
    assert body["base_url"] == "https://bi.example.com"
    assert "s3cret" not in str(body)
    assert "password" not in body
    assert "username" not in body


def test_assets_list_works_without_superset_configured(
    client, admin_headers, monkeypatch, asset
):
    """没接 Superset 时列表仍要能开：登记簿是本地的，不依赖那边。"""
    monkeypatch.setattr(
        svc, "runtime", lambda db: (_ for _ in ()).throw(svc.SupersetNotConfigured("未启用"))
    )
    resp = client.get("/api/superset/assets", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["title"] for i in items] == ["销售看板"]
    # 没配对外地址时不要瞎拼一个绝对地址
    assert items[0]["url"] == "/superset/dashboard/11/"


def test_assets_list_uses_the_public_address(client, admin_headers, monkeypatch, asset):
    monkeypatch.setattr(svc, "runtime", lambda db: CFG)
    items = client.get("/api/superset/assets", headers=admin_headers).json()["items"]
    assert items[0]["url"] == "https://bi.example.com/superset/dashboard/11/"


def test_guest_token_is_minted_server_side(client, admin_headers, monkeypatch, asset):
    """浏览器只拿到 token，拿不到换 token 的账号密码。"""
    monkeypatch.setattr(svc, "runtime", lambda db: CFG)

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(svc, "client", lambda cfg: _C())
    monkeypatch.setattr(svc, "guest_token", lambda *a, **k: "guest-xyz")

    resp = client.post(
        f"/api/superset/assets/{asset.id}/guest-token", headers=admin_headers, json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"token": "guest-xyz", "superset_domain": "https://bi.example.com"}
    assert "s3cret" not in str(body)


def test_guest_token_on_a_chart_is_a_404(client, admin_headers, monkeypatch, db):
    monkeypatch.setattr(svc, "runtime", lambda db: CFG)
    chart = svc.register_asset(db, asset_type="chart", superset_id=99, title="图")
    resp = client.post(
        f"/api/superset/assets/{chart.id}/guest-token", headers=admin_headers, json={}
    )
    assert resp.status_code == 404


def test_guest_token_without_superset_is_409_not_500(
    client, admin_headers, monkeypatch, asset
):
    """没配好是冲突不是崩溃：前端据此提示去设置页，而不是显示"服务器错误"。"""
    monkeypatch.setattr(
        svc, "runtime", lambda db: (_ for _ in ()).throw(svc.SupersetNotConfigured("未启用"))
    )
    resp = client.post(
        f"/api/superset/assets/{asset.id}/guest-token", headers=admin_headers, json={}
    )
    assert resp.status_code == 409


def test_unlink_removes_only_the_registration(client, admin_headers, asset, db):
    # 先取出 id：删完之后再碰这个 ORM 实例会去刷新一行已经不存在的记录。
    asset_id = asset.id
    resp = client.delete(f"/api/superset/assets/{asset_id}", headers=admin_headers)
    assert resp.status_code == 200
    db.expire_all()
    assert db.query(SupersetAsset).count() == 0
    # 再删一次是 404，不是静默成功
    assert client.delete(
        f"/api/superset/assets/{asset_id}", headers=admin_headers
    ).status_code == 404
