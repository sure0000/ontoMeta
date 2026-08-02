"""物化 API：端点编排（draft→confirm→execute）、执行记录、前置校验。

物化总是交 Airflow 编排（无直连落库）。首个用例启用 Airflow 并把 REST 客户端换成替身，
验证端点把流水线串通、回执落库、记录可查；其余用例验证前置校验走失败回执而非 5xx。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus
from app.models.data_app import DataSource


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
        ds = DataSource(name=f"tgt-{tag}", kind="doris", dsn_secret_ref=dsn)
        db.add(ds)
        db.commit()
        return {"ontology_id": ontology.id, "datasource_id": ds.id}


def test_materialize_runs_pipeline_and_records_run(client, admin_headers, tmp_path, monkeypatch):
    ids = _seed("run", dsn=f"sqlite:///{tmp_path / 'target.db'}")

    # 启用 Airflow 并把 REST 客户端换成替身（不需真实 Airflow）；投递目录指向 tmp。
    from app.config import settings as env_settings
    from app.services import materialization_runner

    monkeypatch.setattr(env_settings, "airflow_dags_dir", str(tmp_path / "dags"))
    monkeypatch.setattr(env_settings, "airflow_jobs_dir", str(tmp_path / "jobs"))

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def unpause_dag(self, dag_id):
            pass

        def trigger_dag(self, dag_id, *, dag_run_id, conf=None):
            return {"dag_run_id": dag_run_id, "state": "queued"}

        def run_url(self, dag_id, run_id):
            return f"http://airflow/dags/{dag_id}/grid?dag_run_id={run_id}"

        def close(self):
            pass

    monkeypatch.setattr(materialization_runner, "AirflowClient", _FakeClient)
    client.put(
        "/api/settings/airflow",
        headers=admin_headers,
        json={"endpoint": "http://airflow:8080", "enabled": True},
    )
    try:
        resp = client.post(
            f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
            headers=admin_headers,
            json={"target_datasource_id": ids["datasource_id"], "engine": "hive"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["artifact_id"]
        # 编排回执：execute_mode=orchestrated，带 DagRun 信息，记录了目标数据源
        assert body["receipt"]["execute_mode"] == "orchestrated"
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
        json={"target_datasource_id": ids["datasource_id"], "engine": "hive"},
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
        json={"target_datasource_id": "no-such-id", "engine": "hive"},
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
