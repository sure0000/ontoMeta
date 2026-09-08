"""Superset 作为只连不部署组件：连接形态、拨测不给假绿灯、运行期投影。

Superset 接进来只做三件事——填连接、拨测、被上层按 ``get_superset_runtime`` 读走。
这里钉住的是三条容易退化的东西：

1. **拨测必须真打运行期用到的接口，并校验响应形状**。地址填成 Superset 前面的反代或
   静态页时，任意路径都可能回 200 + HTML；只看状态码的探测会给出"连接成功"的假绿灯，
   而真去建数据集时才发现根本不是 Superset（与 DataHub 那次假绿灯同款）。
2. **``database_id`` 填错要当场暴露**。它是 Superset 侧那条指向数仓的 database 连接，
   ontoMeta 不推导也不代建；填错的话，错误会推迟到 Agent 建数据集时才在另一个进程里炸。
   没填则只提示、不算失败——先配通连接、回头再补 id 是合理次序。
3. **密码是机密字段**，走 ``CONNECTION_SCHEMAS`` 才有掩码回显与「留空 = 保持原值」兜着。
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors.superset import SupersetClient, SupersetError
from app.database import SessionLocal
from app.services import dependency_service as ds
from app.services.dependency_service import DependencyComponentService, ProbeResult


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def svc():
    return DependencyComponentService()


def _client(handler) -> SupersetClient:
    """装了 MockTransport 的客户端：不出网，但走的是真实的请求/校验代码路径。"""
    return SupersetClient(
        "http://superset:8088",
        username="admin",
        password="pw",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------------------ 连接形态


def test_superset_is_in_the_catalog_with_one_default_group(svc):
    schema = svc.schema()
    assert "superset" in schema["components"] or any(
        c["key"] == "superset" for c in schema["components"]
    )
    groups = schema["connection_groups"]["superset"]
    assert [g["id"] for g in groups] == ["default"]
    # 分组必须覆盖全部连接字段，否则漏掉的字段在分节渲染下没人显示。
    covered = [f for g in groups for f in g["fields"]]
    assert sorted(covered) == sorted(f[0] for f in ds.CONNECTION_SCHEMAS["superset"])


def test_password_is_a_secret_field(svc):
    """密码必须声明 secret：掩码回显与「留空 = 保持原值」都挂在这个标记上。"""
    fields = {f["name"]: f for f in svc.schema()["connection_schemas"]["superset"]}
    assert fields["password"]["secret"] is True
    assert fields["base_url"]["secret"] is False
    # 地址与账号是必填；database_id / public_base_url 允许后补。
    assert fields["base_url"]["required"] is True
    assert fields["database_id"]["required"] is False
    assert fields["public_base_url"]["required"] is False


def test_seeded_disabled(svc, db):
    """占位地址的新行不该是启用态：启用着只会让建图工具去连一个不存在的地址。"""
    assert "superset" in ds.DEFAULT_DISABLED_KEYS
    svc.ensure_components(db)
    assert svc._get_singleton(db, "superset") is not None


# ---------------------------------------------------------- 登录：不给假绿灯


def test_login_rejects_html_from_a_reverse_proxy():
    """200 + HTML（反代/静态页）必须判失败——这正是假绿灯的来源。"""
    client = _client(lambda req: httpx.Response(200, text="<!doctype html><html>…"))
    with pytest.raises(SupersetError) as exc:
        client.login()
    assert "不是 JSON" in str(exc.value)


def test_login_rejects_json_without_access_token():
    client = _client(lambda req: httpx.Response(200, json={"message": "ok"}))
    with pytest.raises(SupersetError) as exc:
        client.login()
    assert "access_token" in str(exc.value)


def test_login_reports_bad_credentials_distinctly():
    client = _client(lambda req: httpx.Response(401, json={"message": "bad"}))
    with pytest.raises(SupersetError) as exc:
        client.login()
    assert "账号密码" in str(exc.value)


def test_ping_also_calls_a_real_rest_endpoint():
    """只登录会给假绿灯：token 在带版本前缀的 API 上用不了也算「配了等于没配」。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": "jwt"})
        if request.url.path.endswith("/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf"})
        return httpx.Response(200, json={"result": {"username": "admin"}})

    _client(handler).ping()
    assert "/api/v1/security/login" in seen
    assert "/api/v1/me/" in seen


# ------------------------------------------------------------------ 拨测


class _FakeClient:
    """替身：让拨测用例只关心「探针怎么判」，不关心 HTTP 细节。"""

    def __init__(self, *, ping_error=None, database_error=None):
        self._ping_error = ping_error
        self._database_error = database_error
        self.databases_read: list[int] = []

    def ping(self):
        if self._ping_error:
            raise self._ping_error
        return {"result": {}}

    def get_database(self, database_id: int):
        self.databases_read.append(database_id)
        if self._database_error:
            raise self._database_error
        return {"result": {"id": database_id}}

    def close(self):
        pass


def _patch_client(monkeypatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(
        "app.connectors.superset.SupersetClient", lambda *a, **k: fake
    )


def test_probe_needs_a_base_url():
    assert ds._probe_superset({}, {}).ok is False


def test_probe_surfaces_login_failure(monkeypatch):
    _patch_client(monkeypatch, _FakeClient(ping_error=SupersetError("login", "鉴权失败")))
    result = ds._probe_superset({"base_url": "http://superset:8088"}, {})
    assert result.ok is False
    assert "login" in result.message


def test_probe_passes_but_nags_when_database_id_is_missing(monkeypatch):
    """没填 database_id 只提示：先配通连接、回头再补 id 是合理次序。"""
    _patch_client(monkeypatch, _FakeClient())
    result = ds._probe_superset({"base_url": "http://superset:8088"}, {})
    assert result.ok is True
    assert "database_id" in result.message


def test_probe_verifies_the_configured_database_exists(monkeypatch):
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    result = ds._probe_superset(
        {"base_url": "http://superset:8088", "database_id": 7}, {}
    )
    assert result.ok is True
    assert fake.databases_read == [7]


def test_probe_fails_when_the_configured_database_is_gone(monkeypatch):
    """填了一个 Superset 里不存在的 database，要当场红，而不是等建数据集时才炸。"""
    _patch_client(
        monkeypatch,
        _FakeClient(database_error=SupersetError("get_database", "HTTP 404")),
    )
    result = ds._probe_superset(
        {"base_url": "http://superset:8088", "database_id": 999}, {}
    )
    assert result.ok is False
    assert "999" in result.message


def test_probe_is_registered_for_the_default_group():
    assert ds._PROBES[("superset", "default")] is ds._probe_superset


def test_probe_writes_status_and_ledger(monkeypatch, svc, db):
    """拨测结果逐条记账，行状态由记账聚合——与其它组件同一套。"""
    svc.ensure_components(db)
    row = svc._get_singleton(db, "superset")
    before = (row.connection_json, row.settings_json, row.connection_status, row.enabled)

    probes = dict(ds._PROBES)
    probes[("superset", "default")] = lambda conn, extra: ProbeResult(True, "连接成功", 12)
    monkeypatch.setattr(ds, "_PROBES", probes)
    try:
        result = svc.probe(db, row.id)
        assert result.ok is True
        db.expire_all()
        row = svc._get_singleton(db, "superset")
        assert row.connection_status == "connected"
        assert ds._loads(row.settings_json)["_probe"]["default"]["ok"] is True
    finally:
        db.expire_all()
        row = svc._get_singleton(db, "superset")
        (row.connection_json, row.settings_json, row.connection_status, row.enabled) = before
        db.commit()


# ------------------------------------------------------------- 运行期投影


def test_runtime_falls_back_to_base_url_for_the_public_address(svc, db):
    """没配对外地址就用 base_url——但两者是两个概念，不能反过来推导。"""
    from app.services.settings_service import SettingsService

    svc.ensure_components(db)
    row = svc._get_singleton(db, "superset")
    before = row.connection_json
    try:
        svc.update_component(
            db,
            row.id,
            {
                "connection": {
                    "base_url": "http://internal:8088/",
                    "username": "u",
                    "password": "p",
                    "database_id": 7,
                }
            },
        )
        cfg = SettingsService().get_superset_runtime(db)
        assert cfg.base_url == "http://internal:8088"
        assert cfg.public_base_url == "http://internal:8088"
        assert cfg.database_id == 7
        assert cfg.configured is True
    finally:
        db.expire_all()
        row = svc._get_singleton(db, "superset")
        row.connection_json = before
        db.commit()
