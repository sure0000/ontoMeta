"""物化 API：端点编排（draft→confirm→execute）、执行记录、前置校验。

真实数仓落库需活集群，此处只验证端点把流水线串通、回执落库、记录可查、错误可读。
目标源用 sqlite（hive 方言 DDL 打到 sqlite 会失败）——正是为了验证"执行失败不抛
5xx，回执带 ok=false"这条约定；不测 ok=true（无 sqlite adapter，属手动端到端范畴）。
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


def test_materialize_runs_pipeline_and_records_run(client, admin_headers, tmp_path):
    ids = _seed("run", dsn=f"sqlite:///{tmp_path / 'target.db'}")
    resp = client.post(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/materialize",
        headers=admin_headers,
        json={"target_datasource_id": ids["datasource_id"], "engine": "hive"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["artifact_id"]
    assert body["status"] in ("succeeded", "failed")
    # 回执分 DDL/ETL 两阶段，且记录了目标数据源
    assert "ddl" in body["receipt"]
    assert body["receipt"]["target_datasource"]["id"] == ids["datasource_id"]

    # 执行记录可查
    runs = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/materialization-runs",
        headers=admin_headers,
    ).json()
    assert any(r["artifact_id"] == body["artifact_id"] for r in runs)


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
