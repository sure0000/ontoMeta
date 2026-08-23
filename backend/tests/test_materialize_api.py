"""物化 API：端点编排（draft→confirm→execute）、执行记录、前置校验。

物化总是交 Airflow 编排（无直连落库）。首个用例启用 Airflow 并把 REST 客户端换成替身，
验证端点把流水线串通、回执落库、记录可查；其余用例验证前置校验走失败回执而非 5xx。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus
from app.models.data_app import DataSource


@pytest.fixture(autouse=True)
def _cleanup_seeded_sources():
    """测试隔离：本模块 seed 的 DataSource 留在共享会话库里会污染后续用例的
    warehouse-first 选源（如 test_column_profiler 取「最可用源」）。用完即清。"""
    yield
    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.name.like("tgt-%")).delete()
        db.commit()


def _seed(tag: str, dsn: str | None) -> dict:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:matapi-{tag}", name=f"matapi-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(ontology)
        db.flush()
        db.add(
            ObjectType(
                ontology_id=ontology.id,
                name="customer",
                display_name="客户",
                table_role="business_object",
            )
        )
        for existing in db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)):
            existing.is_default_warehouse = False
        ds = DataSource(
            name=f"tgt-{tag}", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref=dsn,
        )
        db.add(ds)
        db.commit()
        return {"ontology_id": ontology.id, "datasource_id": ds.id}


def test_materialize_runs_pipeline_and_records_run(client, admin_headers, tmp_path, monkeypatch):
    ids = _seed("run", dsn=f"sqlite:///{tmp_path / 'target.db'}")

    # 启用 Airflow 并把 REST 客户端换成替身（不需真实 Airflow）；投递目录指向 tmp。
    # 编排配置全在设置行里（不再有环境变量），故直接写设置行。
    from app.services import materialization_runner

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def dag_exists(self, dag_id):
            return True

        def unpause_dag(self, dag_id):
            pass

        def trigger_dag(self, dag_id, *, dag_run_id, conf=None):
            return {"dag_run_id": dag_run_id, "state": "queued"}

        def run_url(self, dag_id, run_id):
            return f"http://airflow/dags/{dag_id}/grid?dag_run_id={run_id}"

        def close(self):
            pass

    monkeypatch.setattr(materialization_runner, "AirflowClient", _FakeClient)

    # 投递器给真的（SSH 逻辑完整跑，传输落到本地 tmp）：runner 经 build_delivery()
    # 拿实例，patch 类方法覆盖全部用例，避免真 ssh 到不存在的 test 主机。
    from app.services.settings_service import AirflowRuntimeConfig

    from tests.support.delivery import LocalTransportDelivery

    monkeypatch.setattr(
        AirflowRuntimeConfig,
        "build_delivery",
        lambda self: LocalTransportDelivery(),
    )

    # 提交前 preflight（P2 强制闸门）会真连 Airflow 核实可达性/连接；本用例只替身了
    # runner 通道的客户端，preflight 自有一套 AirflowClient。这里桩掉 preflight 直接放行——
    # 本用例验证的是「环境就绪时端到端跑通」，preflight 本身另有 test_materialize_preflight.py 覆盖。
    from app.services import materialize_preflight

    monkeypatch.setattr(
        materialize_preflight,
        "run_preflight",
        lambda *a, **kw: materialize_preflight.PreflightReport(items=[]),
    )
    client.put(
        "/api/settings/airflow",
        headers=admin_headers,
        json={
            "endpoint": "http://airflow:8080",
            "enabled": True,
            "dags_dir": str(tmp_path / "dags"),
            # SSH 投递参数：available 判定需要主机。
            "ssh_host": "test-airflow-host",
            # 统一执行：Flink 参数经设置页落库。给 JAR 走真实 DAG 路径（非 handoff）；给
            # checkpoint 目录以支持含 timestamp 分区键的表默认的 incremental→CDC。
            "flink_sql_runner_jar": "/opt/sql-runner.jar",
            "flink_checkpoint_dir": "file:///tmp/ontometa-ckpt",
        },
    )
    try:
        resp = client.post(
            f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
            headers=admin_headers,
            json={"target_datasource_id": ids["datasource_id"], "engine": "doris"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["artifact_id"]
        # 编排回执：execute_mode=orchestrated（读时对账的开关），带 DagRun 信息与目标数据源；
        # 物化只建结构，emit=ddl。
        assert body["receipt"]["execute_mode"] == "orchestrated"
        assert body["receipt"]["emit"] == "ddl"
        assert body["receipt"]["dag_id"]
        assert body["receipt"]["target_datasource"]["id"] == ids["datasource_id"]

        # 执行记录可查
        runs = client.get(
            f"/api/ontologies/{ids['ontology_id']}/warehouse/materialization-runs",
            headers=admin_headers,
        ).json()
        assert any(r["artifact_id"] == body["artifact_id"] for r in runs)
    finally:
        client.put(
            "/api/settings/airflow",
            headers=admin_headers,
            json={"endpoint": "http://localhost:8081", "enabled": False},
        )


def test_materialize_bad_datasource_returns_400(client, admin_headers):
    ids = _seed("bad", dsn=None)
    # 目标源无 dsn → runner 抛 MaterializationError；执行阶段捕获为 failed 回执
    resp = client.post(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
        headers=admin_headers,
        json={"target_datasource_id": ids["datasource_id"], "engine": "doris"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["ok"] is False


def test_materialize_unknown_datasource_id(client, admin_headers):
    ids = _seed("unknown", dsn="sqlite:///x.db")
    resp = client.post(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
        headers=admin_headers,
        json={"target_datasource_id": "no-such-id", "engine": "doris"},
    )
    # 执行期 MaterializationError → failed 回执（非 5xx）
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"


def test_materialize_unknown_engine_400(client, admin_headers):
    ids = _seed("engine", dsn="sqlite:///x.db")
    resp = client.post(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
        headers=admin_headers,
        json={"target_datasource_id": ids["datasource_id"], "engine": "nope"},
    )
    assert resp.status_code == 400
