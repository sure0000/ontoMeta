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
        dags_dir=str(dags_dir),
        # SSH 投递参数：dag_dir_visible 检查改验 SSH 管道，基线给个主机名。
        ssh_host="test-airflow-host",
        ssh_user="deploy",
        ssh_port=22,
        ssh_password=None,
        # 编排旋钮现在全在设置行上（不再有环境变量），基线取与库默认一致的值。
        max_tasks_per_dag=50,
        max_active_tasks_per_dag=16,
        dag_parse_timeout=60.0,
        preflight_sentinel_timeout=20.0,
        staging_swap=True,
        # Flink 执行参数现在也在设置行上（不再有环境变量）。
        flink_sql_runner_jar="/opt/flink/runner.jar",
        flink_sql_runner_class="com.ontometa.flink.SqlRunner",
        flink_bin="flink",
        flink_deploy_target="yarn-per-job",
        flink_parallelism=1,
        flink_yarn_queue="",
        flink_checkpoint_dir="",
    )
    available = over.pop("available", None)
    base.update(over)
    if available is None:
        available = bool(
            base["endpoint"] and base["dags_dir"] and base["ssh_host"]
        )
    return SimpleNamespace(available=available, **base)


def _make_handler(
    *,
    health=200,
    ping=200,
    openapi_version="v1",
    connection=200,
    sentinel_found=True,
    dag_list=(),
    dags_folder=None,
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
        if path == "/api/v1/config":
            if dags_folder is None:  # expose_config=False 的真实响应
                return httpx.Response(403, text="config not exposed")
            return httpx.Response(200, json={"sections": [
                {"name": "core", "options": [{"key": "dags_folder", "value": dags_folder}]}
            ]})
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
    source_ds=None,
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
    # SSH 管道探测默认放行（可注入正是为这个：真实探活不属单测范围）。
    # 专测失败的用例自己覆盖 probe 返回值。
    monkeypatch.setattr(
        pf, "probe_ssh_pipeline", lambda af: (True, "test：管道连通（桩）")
    )
    # 同理：expose_config 关着时会回退去 SSH 上读 dags_folder，默认桩成「读不到」，
    # 免得单测真去 ssh 一台不存在的主机。要测这条回退的用例自己覆盖。
    monkeypatch.setattr(pf, "dags_folder_via_ssh", lambda af: None)
    monkeypatch.setattr(pf._settings, "get_airflow_runtime", lambda db: runtime)
    monkeypatch.setattr(
        pf._contract_service, "list_contracts", lambda db, oid, **kw: list(contracts)
    )
    monkeypatch.setattr(
        pf._contract_service, "resolve_target_names", lambda db, cs: names or {}
    )
    return SimpleNamespace(
        get=lambda model, _id: source_ds
        if source_ds is not None and _id == source_ds.id
        else ds
    )


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
    assert "ontometa_ds_ds1" in (conn.next_step or "")


def test_missing_source_connection_blocks_sync(monkeypatch, tmp_path):
    """同步除目标端外还必须在 Airflow 中配置源库 Connection。"""
    normal = _make_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ontometa_source_source1"):
            return httpx.Response(404, text="not found")
        return normal(request)

    source = SimpleNamespace(id="source-1", name="ERP")
    db = _install(monkeypatch, tmp_path, handler, source_ds=source)
    report = pf.run_preflight(
        db,
        "o1",
        target_datasource_id="ds1",
        source_datasource_id="source-1",
        engine="hive",
    )

    assert report.ok is False
    conn = _by_key(report)["source_conn"]
    assert conn.status == pf.FAIL and conn.blocking is True
    assert "ontometa_source_source1" in (conn.next_step or "")


def test_managed_connections_do_not_require_preexisting_airflow_rows(monkeypatch, tmp_path):
    """自包含 DAG 会在首任务注册连接，Airflow 当前 404 不应阻断提交。"""
    source = SimpleNamespace(id="source-1", name="ERP")
    db = _install(
        monkeypatch,
        tmp_path,
        _make_handler(connection=404),
        source_ds=source,
    )
    monkeypatch.setattr(
        pf,
        "build_embedded_airflow_connections",
        lambda *args, **kwargs: [
            {"conn_id": "warehouse"},
            {"conn_id": "warehouse_flink"},
            {"conn_id": "source"},
        ],
    )
    report = pf.run_preflight(
        db,
        "o1",
        target_datasource_id="ds1",
        source_datasource_id="source-1",
        source_database="erp",
        managed_connections=True,
        engine="hive",
    )

    assert report.ok is True
    items = _by_key(report)
    assert items["warehouse_conn"].status == pf.PASS
    assert items["doris_flink_conn"].status == pf.PASS
    assert items["source_conn"].status == pf.PASS
    assert "自动创建或更新" in items["source_conn"].detail


def test_readonly_connection_403_is_non_blocking_warn(monkeypatch, tmp_path):
    """⚠ §8.2：只读账号可能对 /connections 无权，降级为「无法确认」而非判死。"""
    db = _install(monkeypatch, tmp_path, _make_handler(connection=403))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    conn = _by_key(report)["warehouse_conn"]
    assert conn.status == pf.WARN and conn.blocking is False
    assert report.ok is True  # 非阻断项不拦提交


def test_api_version_reports_what_the_instance_exposes(monkeypatch, tmp_path):
    """版本已不是配置项（客户端自协商），这一项只如实报出实测版本，不再判"不匹配"。"""
    db = _install(monkeypatch, tmp_path, _make_handler(openapi_version="v2"))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    ver = _by_key(report)["airflow_api_version"]
    assert ver.status == pf.PASS and ver.blocking is False
    assert "v2" in ver.detail
    assert report.ok is True


def test_dag_dir_not_visible_blocks(monkeypatch, tmp_path):
    """SSH 管道不通：产物根本到不了 Airflow 主机，是阻断项（不再是本地 sentinel 的 WARN）。"""
    db = _install(monkeypatch, tmp_path, _make_handler())
    monkeypatch.setattr(
        pf, "probe_ssh_pipeline", lambda af: (False, "SSH 投递失败：Permission denied")
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    dag = _by_key(report)["dag_dir_visible"]
    assert dag.status == pf.FAIL and dag.blocking is True
    assert "Permission denied" in (dag.detail or "")
    assert report.ok is False


def test_missing_ssh_host_blocks(monkeypatch, tmp_path):
    """未配 SSH 主机：dag_dir_visible 直接阻断，提示去设置页填。"""
    db = _install(
        monkeypatch, tmp_path, _make_handler(),
        runtime_over={"ssh_host": "", "available": True},
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    dag = _by_key(report)["dag_dir_visible"]
    assert dag.status == pf.FAIL and dag.blocking is True
    assert "设置页" in (dag.next_step or "")


def test_real_ssh_probe_uses_delivery(monkeypatch):
    """probe_ssh_pipeline 本体：真调投递器，把 mkdir+test -w 发到远端。

    它不依赖 preflight 的桩——构造一个记录型投递器塞进 get_delivery，验证命令
    形态与成功/失败两种返回。这正是旧 git 检查缺的覆盖：那时 subprocess 直接
    内联，这个函数根本没法测。
    """
    from types import SimpleNamespace

    import app.services.dag_delivery as dd

    sent = {}

    class Recording(dd.SshDelivery):
        def _exec(self, cmd):
            sent["cmd"] = cmd
            if "boom" in (cmd[-1] if cmd else ""):
                raise dd.DagDeliveryError("SSH 投递失败：boom")
            return None

    monkeypatch.setattr(dd, "get_delivery", lambda *a, **k: Recording(host=a[0], **k))
    af = SimpleNamespace(
        ssh_host="h", ssh_user="u", ssh_port=22,
        ssh_password=None, dags_dir="/opt/airflow/dags",
    )
    ok, detail = pf.probe_ssh_pipeline(af)
    assert ok and "可写" in detail
    assert "mkdir -p" in sent["cmd"][-1] and "test -w" in sent["cmd"][-1]
    assert "/opt/airflow/dags" in sent["cmd"][-1]

    ok, detail = pf.probe_ssh_pipeline(
        SimpleNamespace(ssh_host="h", ssh_user="u", ssh_port=22,
                        ssh_password=None, dags_dir="boom")
    )
    assert not ok and "boom" in detail


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


# Flink 部署设置已从环境变量搬进 Airflow 设置行（DB）；用例通过 runtime_over 覆盖。
_NO_FLINK = {"flink_sql_runner_jar": "", "flink_checkpoint_dir": ""}


def test_missing_flink_jar_warns_but_not_blocks(monkeypatch, tmp_path):
    """缺 SqlRunner JAR → 搬运只产出不执行（handoff），WARN 不阻断提交。"""
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive", emit="dml")
    item = _by_key(report)["flink_jar"]
    assert item.status == pf.WARN and item.blocking is False
    assert "Flink SqlRunner JAR" in (item.next_step or "")
    assert report.ok is True


def _plan_of(*jobs) -> SimpleNamespace:
    return SimpleNamespace(jobs=jobs)


def _move_job(name: str, mode: str, **over) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        mode=mode,
        incremental_column=over.get("incremental_column"),
        initial_watermark=over.get("initial_watermark"),
        source=SimpleNamespace(database="erp", table="tabA", platform="mysql"),
    )


def test_cdc_without_checkpoint_blocks(monkeypatch, tmp_path):
    """本次有 CDC 表但未配 checkpoint：流式作业编译期必挂，提交前必须红。"""
    from app.services.job_planner import JobPlanner

    monkeypatch.setattr(JobPlanner, "build", lambda *a, **k: _plan_of(_move_job("sync_ods_a", "cdc")))
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive", emit="dml")
    item = _by_key(report)["flink_checkpoint"]
    assert item.status == pf.FAIL and item.blocking is True
    assert "Checkpoint" in (item.next_step or "")
    assert report.ok is False


def test_cdc_with_checkpoint_passes(monkeypatch, tmp_path):
    """CDC 表配了 checkpoint → PASS。"""
    from app.services.job_planner import JobPlanner

    monkeypatch.setattr(JobPlanner, "build", lambda *a, **k: _plan_of(_move_job("sync_ods_a", "cdc")))
    db = _install(
        monkeypatch, tmp_path, _make_handler(),
        runtime_over={"flink_sql_runner_jar": "", "flink_checkpoint_dir": "file:///tmp/ckpt"},
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive", emit="dml")
    item = _by_key(report)["flink_checkpoint"]
    assert item.status == pf.PASS and item.blocking is False
    assert report.ok is True


def test_task_level_checkpoint_dir_satisfies_cdc(monkeypatch, tmp_path):
    """checkpoint 目录填在**这条任务**上也算数——只看设置页会拦下已经配好的任务。"""
    from app.services.job_planner import JobPlanner

    monkeypatch.setattr(JobPlanner, "build", lambda *a, **k: _plan_of(_move_job("sync_ods_a", "cdc")))
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(
        db, "o1", target_datasource_id="ds1", engine="hive", emit="dml",
        flink_task_params={"flink_checkpoint_dir": "file:///tmp/task-ckpt"},
    )
    assert _by_key(report)["flink_checkpoint"].status == pf.PASS
    assert report.ok is True


def test_incremental_does_not_need_checkpoint(monkeypatch, tmp_path):
    """增量是有界 batch（水位谓词 + 跑完即退），不该被当成流式作业索要 checkpoint。

    缺增量字段/水位是另一件事，由 ``_check_sync_spec`` 按本任务 Spec 直接报——拿
    「配 checkpoint」去治它，人照做也编不出 SQL。
    """
    from app.services.job_planner import JobPlanner

    monkeypatch.setattr(
        JobPlanner, "build", lambda *a, **k: _plan_of(_move_job("sync_ods_a", "incremental"))
    )
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive", emit="dml")
    assert "flink_checkpoint" not in _by_key(report)
    assert report.ok is True


def test_sync_preflight_previews_this_task_not_the_contract(monkeypatch, tmp_path):
    """回归：一条「全量」同步不能因为**契约**写着 incremental/cdc 而被判成流式作业。

    自检预演的必须是这次要提交的作业，故装载方式/增量字段/水位按 Spec 传给 planner，
    落点固定 ODS（同步从不写 dim/dwd，作业名也不该出现分层）。
    """
    from app.services.job_planner import JobPlanner

    seen: dict = {}

    def _capture(self, db, ontology_id, **kwargs):
        seen.update(kwargs)
        # planner 收到 load_strategy 覆盖后，本次作业就是全量——与契约无关。
        return _plan_of(_move_job("sync_ods_customer_group", kwargs.get("load_strategy") or "cdc"))

    monkeypatch.setattr(JobPlanner, "build", _capture)
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(
        db, "o1", target_datasource_id="ds1", engine="hive", emit="dml",
        selected_targets=["customer_group"],
        load_strategy="full",
        incremental_column="modified",
    )
    assert seen["load_strategy"] == "full"
    assert seen["target_ods_database"] == "ods"
    assert seen["incremental_columns"] == {"customer_group": "modified"}
    # 全量既不是 CDC 也不需要 checkpoint：这一项根本不该出现。
    assert "flink_checkpoint" not in _by_key(report)
    assert report.ok is True


def test_materialize_skips_flink_checks(monkeypatch, tmp_path):
    """物化只建表（SQLExecuteQueryOperator 直连目标仓），不产 Flink 作业。

    此前它照样预演搬运，于是别人契约上的装载方式能把一次建表拦在提交之前。
    """
    from app.services.job_planner import JobPlanner

    def _boom(*a, **k):  # 物化路径压根不该调 planner
        raise AssertionError("materialize 不应预演搬运作业")

    monkeypatch.setattr(JobPlanner, "build", _boom)
    db = _install(monkeypatch, tmp_path, _make_handler(), runtime_over=dict(_NO_FLINK))
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    keys = _by_key(report)
    assert "flink_jar" not in keys and "flink_checkpoint" not in keys
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


def test_dag_dir_mismatch_with_instance_warns_with_both_readings(monkeypatch, tmp_path):
    """投递目录与实例自报的 dags_folder 不一致 → 提醒并把两种可能都说清。

    不阻断：容器部署下 dags_folder 是容器内路径、投递落在宿主机上，两者本来就不同，
    ontoMeta 看不见那层挂载映射，判死会冤枉正确配置。
    """
    db = _install(
        monkeypatch, tmp_path,
        _make_handler(dags_folder="/opt/airflow/dags"),
        runtime_over={"dags_dir": "/home/xuyc/airflow-docker/dags"},
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["dag_dir_matches_instance"]
    assert item.status == pf.WARN and item.blocking is False
    assert "/opt/airflow/dags" in item.detail  # 实例那边是什么，如实说
    assert "容器" in (item.next_step or "")  # 两种读法都给
    assert report.ok is True  # 提醒项不拦提交


def test_dag_dir_under_the_instance_folder_passes(monkeypatch, tmp_path):
    """填成 dags_folder 的子目录也算数——Airflow 是递归扫的。"""
    db = _install(
        monkeypatch, tmp_path,
        _make_handler(dags_folder="/home/xuyc/airflow/dags"),
        runtime_over={"dags_dir": "/home/xuyc/airflow/dags/ontometa"},
    )
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    assert _by_key(report)["dag_dir_matches_instance"].status == pf.PASS


def test_expose_config_off_only_warns(monkeypatch, tmp_path):
    """两条路都读不到 core.dags_folder 是「没法对账」，不是「配错了」——别拿它拦提交。"""
    db = _install(monkeypatch, tmp_path, _make_handler())  # dags_folder=None → 403
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["dag_dir_matches_instance"]
    assert item.status == pf.WARN and item.blocking is False
    assert "expose_config" in item.detail
    assert "SSH" in item.detail  # 说清 SSH 那条也试过了，省得人以为还有招没用


def test_expose_config_off_falls_back_to_ssh_reading(monkeypatch, tmp_path):
    """expose_config 关着是默认配置，不该让「投递目录对不对」永久留在盲区。

    投递管道刚验通，就用同一条 SSH 去问那台机器本人；读到了就照常对账。
    """
    db = _install(monkeypatch, tmp_path, _make_handler())  # REST /config → 403
    monkeypatch.setattr(pf, "dags_folder_via_ssh", lambda af: "/srv/airflow/dags")
    report = pf.run_preflight(db, "o1", target_datasource_id="ds1", engine="hive")
    item = _by_key(report)["dag_dir_matches_instance"]
    assert item.status == pf.WARN and item.blocking is False
    assert "/srv/airflow/dags" in item.detail and "SSH" in item.detail
    assert report.ok is True  # 只提醒，不阻断


def test_ssh_reading_ignores_cli_noise(monkeypatch, tmp_path):
    """airflow CLI 会在 stdout 里夹带警告；只认最后一行的绝对路径，读不出就当没读到。"""
    calls: list[str] = []

    class _FakeDelivery:
        def _ssh(self, cmd):
            calls.append(cmd)
            return SimpleNamespace(
                stdout="DeprecationWarning: something\n/opt/airflow/dags\n"
            )

    monkeypatch.setattr("app.services.dag_delivery.get_delivery", lambda *a, **k: _FakeDelivery())
    airflow = _runtime("/x", ssh_host="h1", ssh_user="u", ssh_port=22, ssh_password=None)
    assert pf.dags_folder_via_ssh(airflow) == "/opt/airflow/dags"
    assert "dags_folder" in calls[0]


def test_ssh_reading_failure_is_silent(monkeypatch, tmp_path):
    """远端没有 airflow 命令（或连不上）只是读不到，不该把整份自检带崩。"""
    def _boom(*a, **k):
        raise RuntimeError("bash: airflow: command not found")

    monkeypatch.setattr("app.services.dag_delivery.get_delivery", _boom)
    airflow = _runtime("/x", ssh_host="h1", ssh_user="u", ssh_port=22, ssh_password=None)
    assert pf.dags_folder_via_ssh(airflow) is None
