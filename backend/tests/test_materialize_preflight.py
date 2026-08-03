"""物化提交前自检（Preflight Gate，M13）。

不起真实 Airflow、不建真实库：用 httpx.MockTransport 伪造 Airflow REST（比照
test_airflow_connector.py），并把 preflight 依赖的 settings / 契约服务 / sentinel 超时
这几个seam monkeypatch 掉，专测「每类失败是否在提交前被如实分类、给出可照做的下一步」。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest

import app.services.materialize_preflight as pf
from app.connectors.airflow import AirflowClient


def _runtime(dags_dir, **over) -> SimpleNamespace:
    base = dict(
        endpoint="http://airflow:8080",
        username="admin",
        password="admin",
        token=None,
        api_version="v1",
        dags_dir=str(dags_dir),
    )
    available = over.pop("available", None)
    base.update(over)
    if available is None:
        available = bool(base["endpoint"] and base["dags_dir"])
    return SimpleNamespace(available=available, **base)


def _make_handler(
    *,
    health=200,
    ping=200,
    openapi_version="v1",
    connection=200,
    sentinel_found=True,
):
    """按路由分派的 MockTransport handler。各路由的响应可逐个覆盖以造不同失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(health, json={"metadatabase": {"status": "healthy"}})
        if path in ("/openapi.json", "/api/v1/openapi.json", "/api/v2/openapi.json"):
            if openapi_version is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"servers": [{"url": f"/api/{openapi_version}"}]})
        if path == "/api/v1/dags":  # ping_api（带版本前缀，不含 id）
            return httpx.Response(ping, json={"dags": [], "total_entries": 0})
        if path.startswith("/api/v1/connections/"):
            return httpx.Response(connection, json={"connection_id": path.rsplit("/", 1)[-1]})
        if path.startswith("/api/v1/dags/"):  # dag_exists（sentinel 探测）
            return httpx.Response(200 if sentinel_found else 404, json={"dag_id": "x"})
        return httpx.Response(404, text=f"unrouted {path}")

    return handler


def _install(
    monkeypatch,
    tmp_path,
    handler,
    *,
    ds=SimpleNamespace(id="ds1", name="dw"),
    contracts=(),
    names=None,
    runtime_over=None,
    sentinel_timeout=0.3,
    max_tasks=50,
):
    def factory(endpoint, **kwargs):
        kwargs.pop("client", None)
        return AirflowClient(
            endpoint, client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs
        )

    runtime_over = dict(runtime_over or {})
    runtime_dir = runtime_over.pop("dags_dir", str(tmp_path))
    runtime = _runtime(runtime_dir, **runtime_over)
    monkeypatch.setattr(pf, "AirflowClient", factory)
    monkeypatch.setattr(pf._settings, "get_airflow_runtime", lambda db: runtime)
    monkeypatch.setattr(pf._env, "ontometa_preflight_sentinel_timeout", sentinel_timeout)
    monkeypatch.setattr(pf._env, "ontometa_max_tasks_per_dag", max_tasks)
    monkeypatch.setattr(
        pf._contract_service, "list_contracts", lambda db, oid, **kw: list(contracts)
    )
    monkeypatch.setattr(
        pf._contract_service, "resolve_target_names", lambda db, cs: names or {}
    )
    return SimpleNamespace(get=lambda model, _id: ds)


def _by_key(report):
    return {i.key: i for i in report.items}


def test_all_green_is_ok(monkeypatch, tmp_path):
    db = _install(monkeypatch, tmp_path, _make_handler())
    report = pf.run_preflight(
        db, "o1", target_datasource_id="ds1", engine="hive"
    )
    assert report.ok is True
    items = _by_key(report)
    assert items["airflow_reachable"].status == pf.PASS
    assert items["airflow_api_auth"].status == pf.PASS
    assert items["airflow_api_version"].status == pf.PASS
    assert items["warehouse_conn"].status == pf.PASS
    assert items["dag_dir_visible"].status == pf.PASS
    assert items["batch_size"].status == pf.PASS


def test_sentinel_file_is_cleaned_up(monkeypatch, tmp_path):
    db = _install(monkeypatch, tmp_path, _make_handler())
    pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    # sentinel 探测完必须删掉，不能在 dags 目录留一个 .py 污染 Airflow import errors。
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".py")]
    assert leftover == []


def test_api_auth_failure_blocks(monkeypatch, tmp_path):
    """/health 匿名可读会给假绿灯——鉴权真正的检查在带版本前缀的 API 上。"""
    db = _install(monkeypatch, tmp_path, _make_handler(ping=403))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    assert report.ok is False
    auth = _by_key(report)["airflow_api_auth"]
    assert auth.status == pf.FAIL and auth.blocking is True
    assert "AUTH_BACKENDS" in (auth.next_step or "")


def test_missing_warehouse_connection_blocks(monkeypatch, tmp_path):
    """Connection 不存在 = 提交后渲染期整个 DAG 一起红（失败模式 #6），必须提交前拦。"""
    db = _install(
        monkeypatch,
        tmp_path,
        _make_handler(connection=404),
        ds=SimpleNamespace(id="ds1", name="dw"),
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    assert report.ok is False
    conn = _by_key(report)["warehouse_conn"]
    assert conn.status == pf.FAIL and conn.blocking is True
    # 下一步要能直接照做：给出该建的 conn_id。
    assert "ontometa_ds_dw" in (conn.next_step or "")


def test_readonly_connection_403_is_non_blocking_warn(monkeypatch, tmp_path):
    """⚠ §8.2：只读账号可能对 /connections 无权，降级为「无法确认」而非判死。"""
    db = _install(monkeypatch, tmp_path, _make_handler(connection=403))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    conn = _by_key(report)["warehouse_conn"]
    assert conn.status == pf.WARN and conn.blocking is False
    assert report.ok is True  # 非阻断项不拦提交


def test_api_version_mismatch_warns_with_correction(monkeypatch, tmp_path):
    db = _install(monkeypatch, tmp_path, _make_handler(openapi_version="v2"))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    ver = _by_key(report)["airflow_api_version"]
    assert ver.status == pf.WARN and ver.blocking is False
    assert "v2" in (ver.next_step or "")
    assert report.ok is True


def test_dag_dir_not_visible_warns(monkeypatch, tmp_path):
    """sentinel 超时未被解析：不硬失败（可能只是解析间隔长），但要现形并给两种可能。"""
    db = _install(
        monkeypatch, tmp_path, _make_handler(sentinel_found=False), sentinel_timeout=0.2
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    dag = _by_key(report)["dag_dir_visible"]
    assert dag.status == pf.WARN and dag.blocking is False
    assert "dag_dir_list_interval" in (dag.next_step or "")
    assert report.ok is True


def test_unwritable_dags_dir_blocks(monkeypatch, tmp_path):
    """ontoMeta 根本写不进投递目录：这一步单独就能抓到，且是阻断项。"""
    bad = tmp_path / "nope"
    bad.write_text("i am a file, not a dir")  # makedirs 会失败
    db = _install(
        monkeypatch, tmp_path, _make_handler(), runtime_over={"dags_dir": str(bad)}
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    dag = _by_key(report)["dag_dir_visible"]
    assert dag.status == pf.FAIL and dag.blocking is True


def test_airflow_unavailable_short_circuits(monkeypatch, tmp_path):
    db = _install(
        monkeypatch, tmp_path, _make_handler(), runtime_over={"available": False}
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    items = _by_key(report)
    assert items["airflow_reachable"].status == pf.FAIL
    assert items["airflow_api_auth"].status == pf.FAIL
    # 不可达时仍能本地算批次规模。
    assert "batch_size" in items
    assert report.ok is False


def test_batch_over_limit_warns(monkeypatch, tmp_path):
    contracts = [SimpleNamespace(target_id=f"t{i}", target_kind="object_type") for i in range(3)]
    db = _install(
        monkeypatch, tmp_path, _make_handler(), contracts=contracts, max_tasks=2
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    batch = _by_key(report)["batch_size"]
    assert batch.status == pf.WARN and batch.blocking is False
    assert report.ok is True


def test_selected_targets_narrow_batch_count(monkeypatch, tmp_path):
    contracts = [
        SimpleNamespace(target_id="t1", target_kind="object_type"),
        SimpleNamespace(target_id="t2", target_kind="object_type"),
    ]
    names = {"t1": ("alpha", "Alpha"), "t2": ("beta", "Beta")}
    db = _install(
        monkeypatch, tmp_path, _make_handler(), contracts=contracts, names=names, max_tasks=1
    )
    report = pf.run_preflight(
        db, "o1", target_datasource_id="ds1", engine="hive", selected_targets=["alpha"]
    )
    # 只勾了 1 个实体，未超上限 1 → pass（而非按全量 2 判超限）。
    assert _by_key(report)["batch_size"].status == pf.PASS


def test_preflight_endpoint_wires_and_serializes(client, admin_headers, monkeypatch):
    """端点层：路由 + _require_ontology/_require_engine + response_model 序列化打通。"""
    from app.database import SessionLocal
    from app.models import DomainContext, Ontology, OntologyStatus
    from app.models.data_app import DataSource

    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id="urn:li:domain:pf", name="pf")
        db.add(domain)
        db.flush()
        ont = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(ont)
        db.flush()
        ds = DataSource(name="pf-tgt", kind="doris", dsn_secret_ref="sqlite:///x")
        db.add(ds)
        db.commit()
        oid, dsid = ont.id, ds.id

    # 端点在函数体内 import run_preflight，故 monkeypatch 模块属性即可拦到。
    report = pf.PreflightReport()
    report.add(
        pf.PreflightItem(
            key="airflow_reachable",
            label="Airflow 可达",
            status=pf.PASS,
            blocking=True,
            detail="ok",
        )
    )
    monkeypatch.setattr(pf, "run_preflight", lambda *a, **k: report)

    resp = client.post(
        f"/api/ontologies/{oid}/warehouse/materialize/preflight",
        headers=admin_headers,
        json={"target_datasource_id": dsid, "engine": "doris"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["items"][0]["key"] == "airflow_reachable"
    assert body["items"][0]["next_step"] is None


def test_preflight_endpoint_unknown_ontology_404(client, admin_headers):
    resp = client.post(
        "/api/ontologies/does-not-exist/warehouse/materialize/preflight",
        headers=admin_headers,
        json={"target_datasource_id": "x", "engine": "hive"},
    )
    assert resp.status_code == 404
