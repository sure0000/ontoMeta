"""Superset 管理 REST：没配好不等于报错、凭据不出网、解除登记不删外部对象。

三条是这层特有的：

1. ``/superset/status`` 与 ``/superset/assets`` 在**没配 Superset 时也要正常返回**——
   页面要能显示「还没接」，而不是弹一个红色报错让人以为坏了；
2. 任何响应都不得带出账号密码；
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
    """对外地址 + 全屏参数：点过去直接看内容，不带 Superset 的全局导航。"""
    monkeypatch.setattr(svc, "runtime", lambda db: CFG)
    items = client.get("/api/superset/assets", headers=admin_headers).json()["items"]
    assert items[0]["url"] == "https://bi.example.com/superset/dashboard/11/?standalone=1"


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
