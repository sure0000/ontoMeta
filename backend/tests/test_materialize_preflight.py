"""物化提交前自检（Preflight Gate，M13）。

不起真实 Airflow、不建真实库：用 httpx.MockTransport 伪造 Airflow REST（比照
test_airflow_connector.py），并把 preflight 依赖的 settings / 契约服务 / sentinel 超时
这几个 seam monkeypatch 掉，专测「每类失败是否在提交前被如实分类、给出可照做的下一步」。

统一执行架构后（D/F 提交）：sync_runner/docker 通道已废除，preflight 只查 Flink 前置
（flink_jar / flink_checkpoint），movability 独立项已并入 execution_channel 的可搬性预演。
"""

from __future__ import annotations

import os
import time
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
        # 编排旋钮现在全在设置行上（不再有环境变量），基线取与库默认一致的值。
        max_tasks_per_dag=50,
        max_active_tasks_per_dag=16,
        dag_parse_timeout=60.0,
        preflight_sentinel_timeout=20.0,
        staging_swap=True,
        jobs_dir="",
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
    dag_list=(),
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
        if path == "/api/v1/config":  # expose_config=False 的真实响应
            return httpx.Response(403, text="config not exposed")
        if path == "/api/v1/dags":  # ping_api / list_dag_ids（带版本前缀，不含 id）
            return httpx.Response(
                ping,
                json={
                    "dags": [{"dag_id": d} for d in dag_list],
                    "total_entries": len(dag_list),
                },
            )
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
    # 这两个旋钮已从环境变量搬进设置行，故由 runtime 携带（用例仍可用同名参数覆盖）。
    runtime_over.setdefault("preflight_sentinel_timeout", sentinel_timeout)
    runtime_over.setdefault("max_tasks_per_dag", max_tasks)
    runtime = _runtime(runtime_dir, **runtime_over)
    monkeypatch.setattr(pf, "AirflowClient", factory)
    monkeypatch.setattr(pf._settings, "get_airflow_runtime", lambda db: runtime)
    monkeypatch.setattr(
        pf._contract_service, "list_contracts", lambda db, oid, **kw: list(contracts)
    )
    monkeypatch.setattr(
        pf._contract_service, "resolve_target_names", lambda db, cs: names or {}
    )
    return SimpleNamespace(get=lambda model, _id: ds)


def _fake_job(name: str, *, database: str = "erp_ods", table: str = "tab_a"):
    """伪搬运作业：带 source 端点——execution_channel 的可搬性预演要读它。"""
    return SimpleNamespace(
        name=name,
        source=SimpleNamespace(database=database, table=table, platform="mysql"),
    )


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
    # 全绿 = 无 FAIL 项（PASS 项 blocking=True 是「必需检查已通过」，属正常）。
    assert all(i.status != pf.FAIL for i in report.items)


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
    contracts = [
        SimpleNamespace(target_id=f"t{i}", target_kind="object_type", load_strategy="full")
        for i in range(3)
    ]
    db = _install(
        monkeypatch, tmp_path, _make_handler(), contracts=contracts, max_tasks=2
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    batch = _by_key(report)["batch_size"]
    assert batch.status == pf.WARN and batch.blocking is False
    assert report.ok is True


def test_selected_targets_narrow_batch_count(monkeypatch, tmp_path):
    contracts = [
        SimpleNamespace(target_id="t1", target_kind="object_type", load_strategy="full"),
        SimpleNamespace(target_id="t2", target_kind="object_type", load_strategy="full"),
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


# ---------- 统一执行架构：Flink 前置条件（替代已废除的 runner/docker 检查）----------


def _no_flink_settings(monkeypatch):
    """钉死 Flink 部署设置：JAR 与 checkpoint 都未配置（与用例无关的环境变量不参与）。"""
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "flink_sql_runner_jar", "")
    monkeypatch.setattr(cfg.settings, "flink_checkpoint_dir", "")


def test_missing_flink_jar_warns_but_not_blocks(monkeypatch, tmp_path):
    """缺 SqlRunner JAR → 搬运只产出不执行（handoff），WARN 不阻断提交。"""
    _no_flink_settings(monkeypatch)
    db = _install(monkeypatch, tmp_path, _make_handler())
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["flink_jar"]
    assert item.status == pf.WARN and item.blocking is False
    assert "FLINK_SQL_RUNNER_JAR" in (item.next_step or "")
    assert report.ok is True


def test_incremental_without_checkpoint_blocks(monkeypatch, tmp_path):
    """本次有增量/CDC 表但未配 checkpoint：流式作业编译期必挂，提交前必须红。"""
    from app.services.job_planner import JobPlanner

    _no_flink_settings(monkeypatch)
    plan = SimpleNamespace(
        jobs=(
            SimpleNamespace(name="inc_a", mode="incremental",
                            source=SimpleNamespace(database="erp", table="tabA", platform="mysql")),
        )
    )
    monkeypatch.setattr(JobPlanner, "build", lambda *a, **k: plan)
    db = _install(monkeypatch, tmp_path, _make_handler())
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["flink_checkpoint"]
    assert item.status == pf.FAIL and item.blocking is True
    assert "FLINK_CHECKPOINT_DIR" in (item.next_step or "")
    assert report.ok is False


def test_incremental_with_checkpoint_passes(monkeypatch, tmp_path):
    """增量/CDC 表配了 checkpoint → PASS。"""
    from app.services.job_planner import JobPlanner

    import app.config as cfg

    _no_flink_settings(monkeypatch)
    monkeypatch.setattr(cfg.settings, "flink_checkpoint_dir", "file:///tmp/ckpt")
    plan = SimpleNamespace(
        jobs=(
            SimpleNamespace(name="inc_a", mode="incremental",
                            source=SimpleNamespace(database="erp", table="tabA", platform="mysql")),
        )
    )
    monkeypatch.setattr(JobPlanner, "build", lambda *a, **k: plan)
    db = _install(monkeypatch, tmp_path, _make_handler())
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["flink_checkpoint"]
    assert item.status == pf.PASS and item.blocking is False
    assert report.ok is True


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


# ---------- 用历史投递判「两侧目录不是同一个」 ----------


def _deliver(tmp_path, dag_id: str, *, age_seconds: float) -> None:
    """伪造一个 ontoMeta 已投出的 DAG 文件，并把 mtime 拨老。"""
    path = tmp_path / f"{dag_id}.py"
    path.write_text("# fake delivered dag\n", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def test_old_delivered_dags_unseen_by_airflow_is_blocking(monkeypatch, tmp_path):
    """投了很久的 DAG 一个都没被认领 → 断定路径不一致并阻断。

    sentinel 只等 20s，分不清「路径错」和「扫得慢」（dag_dir_list_interval 默认 300s），
    于是永远只能给提醒；而 15 分钟前就落盘的文件仍不在册，与扫描快慢无关，是确凿证据。
    """
    _deliver(tmp_path, "ontometa_materialize_abc123", age_seconds=3600)
    db = _install(monkeypatch, tmp_path, _make_handler(dag_list=()))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")

    item = _by_key(report)["dag_dir_visible"]
    assert item.status == pf.FAIL
    assert item.blocking is True
    assert "不是同一个目录" in item.detail
    assert report.ok is False, "确证路径不一致却仍允许提交"


def test_delivered_dag_seen_by_airflow_passes_without_sentinel(monkeypatch, tmp_path):
    """历史投递已被认领 → 直接判通过，不必再写 sentinel 赌时序。"""
    _deliver(tmp_path, "ontometa_materialize_abc123", age_seconds=3600)
    db = _install(
        monkeypatch, tmp_path,
        _make_handler(dag_list=("ontometa_materialize_abc123",)),
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")

    item = _by_key(report)["dag_dir_visible"]
    assert item.status == pf.PASS
    assert "两侧目录一致" in item.detail
    # 走了快路径就不该再落 sentinel 文件
    assert not [f for f in os.listdir(tmp_path) if f.startswith("ontometa_preflight_")]


def test_recent_delivery_falls_back_to_sentinel(monkeypatch, tmp_path):
    """刚投的 DAG 还没被认领**不算**证据——那可能只是没扫到，不能误报阻断。"""
    _deliver(tmp_path, "ontometa_materialize_abc123", age_seconds=5)
    db = _install(
        monkeypatch, tmp_path,
        _make_handler(dag_list=(), sentinel_found=False),
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")

    item = _by_key(report)["dag_dir_visible"]
    assert item.status == pf.WARN, "投递太新时应退回 sentinel 的提醒级结论"
    assert item.blocking is False


def test_sentinel_files_are_not_counted_as_delivered(monkeypatch, tmp_path):
    """遗留的 sentinel 不能被当成「我们投的 DAG」——否则它自己会把自己判成路径不一致。"""
    _deliver(tmp_path, "ontometa_preflight_deadbeef", age_seconds=3600)
    db = _install(
        monkeypatch, tmp_path,
        _make_handler(dag_list=(), sentinel_found=True),
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    assert _by_key(report)["dag_dir_visible"].status == pf.PASS
