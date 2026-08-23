"""Phase 6 ordered migration, shadow privacy, approval and cut-over gates."""
from __future__ import annotations

from datetime import datetime
import uuid

import pytest

from app.models import (
    DataSource,
    DomainContext,
    DorisWarehouseConfig,
    ObjectType,
    Ontology,
    OntologyStatus,
    OntologyWarehouseDeployment,
    WarehouseMigrationBatch,
    WarehouseObjectProjection,
)
from app.services.doris_deployment import prepare_current_deployment
from app.services.query_routing import projection_mapping, readiness_error
from app.services.warehouse_migration import (
    MigrationGateError,
    WarehouseMigrationService,
    cutover_error,
    shadow_difference_report,
)


@pytest.fixture(autouse=True)
def _cleanup_phase6(db):
    yield
    # Foreign-key enforcement is off in the shared SQLite test database, so
    # delete in dependency order for deterministic isolation.
    from app.models import WarehouseMigrationEvidence

    db.rollback()
    db.query(WarehouseMigrationEvidence).delete()
    db.query(WarehouseMigrationBatch).delete()
    db.query(WarehouseObjectProjection).delete()
    db.query(OntologyWarehouseDeployment).delete()
    db.query(DorisWarehouseConfig).delete()
    db.query(DataSource).filter(DataSource.name.like("prod-doris-%")).delete()
    db.commit()


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:phase6-{tag}", name=f"phase6-{tag}")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id,
        status=OntologyStatus.PUBLISHED.value,
        version=7,
    )
    db.add(ontology)
    db.flush()
    ready = ObjectType(
        ontology_id=ontology.id, name="customer", display_name="客户", status="published"
    )
    pending = ObjectType(
        ontology_id=ontology.id, name="sales_order", display_name="订单", status="published"
    )
    db.add_all([ready, pending])
    db.flush()
    for existing in db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)):
        existing.is_default_warehouse = False
    doris = DataSource(
        name=f"prod-doris-{tag}",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        enabled=True,
        status="ok",
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.flush()
    config = DorisWarehouseConfig(
        id=f"config-{ontology.id}",
        warehouse_datasource_id=doris.id,
        enabled=True,
        query_host="fe",
        query_port=9030,
        fenodes_json='["fe:8030"]',
        airflow_ddl_conn_id="doris_ddl",
        airflow_etl_conn_id="doris_etl",
        airflow_flink_conn_id="doris_flink",
        reader_dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(config)
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=7,
        doris_datasource_id=doris.id,
        status="ready",
    )
    db.add(deployment)
    db.flush()
    db.add_all([
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=ready.id,
            serving_layer="dim",
            serving_database="dim_erp",
            serving_table="customer",
            schema_status="ready",
            sync_status="ready",
            transform_status="ready",
            queryable=True,
            sync_watermark="2026-08-22T07:00:00Z",
        ),
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=pending.id,
            serving_layer="dwd",
            serving_database="dwd_erp",
            serving_table="sales_order",
            schema_status="ready",
            sync_status="ready",
            transform_status="pending",
            queryable=False,
        ),
    ])
    db.commit()
    return ontology, doris, deployment


def test_prepare_deployment_is_pending_until_airflow_success(db):
    ontology, doris, deployment = _seed(db)
    from app.services.materialization_contract import MaterializationContractService

    MaterializationContractService().sync(db, ontology.id)
    # Replace the seed's ready deployment to exercise the real step-2 entry.
    db.query(WarehouseObjectProjection).filter_by(deployment_id=deployment.id).delete()
    db.delete(deployment)
    db.commit()
    prepared = prepare_current_deployment(
        db, ontology_id=ontology.id, datasource_id=doris.id, database_prefix="erp"
    )
    assert prepared.status == "pending"
    projections = db.query(WarehouseObjectProjection).filter_by(deployment_id=prepared.id).all()
    assert projections
    assert all(p.schema_status == "pending" and not p.queryable for p in projections)


def test_phase6_failure_blocks_and_order_cannot_be_skipped(db):
    ontology, _doris, _deployment = _seed(db)
    service = WarehouseMigrationService()
    batch = service.create_batch(
        db,
        ontology_id=ontology.id,
        approver="bootstrap-admin",
        rollback_owner="bootstrap-admin",
        observation_window_minutes=60,
        created_by="operator",
    )
    with pytest.raises(MigrationGateError, match="严格执行步骤 1"):
        service.record_step(
            db,
            batch_id=batch.id,
            step=2,
            passed=True,
            report={},
            artifact_ids=[],
            operator="operator",
        )
    evidence = service.record_step(
        db,
        batch_id=batch.id,
        step=1,
        passed=False,
        report={"reason": "reader grant too broad"},
        artifact_ids=[],
        operator="operator",
    )
    assert evidence.status == "fail"
    db.refresh(batch)
    assert batch.status == "blocked"
    assert batch.current_step == 0
    with pytest.raises(MigrationGateError, match="步骤 1"):
        service.approve_cutover(db, batch_id=batch.id, approver="bootstrap-admin", note="go")


def test_phase6_step1_minimum_identity_gate_and_retry(db):
    ontology, doris, _deployment = _seed(db)
    service = WarehouseMigrationService()
    batch = service.create_batch(
        db,
        ontology_id=ontology.id,
        approver="bootstrap-admin",
        rollback_owner="bootstrap-admin",
        observation_window_minutes=30,
    )
    report = {
        "doris_version": "3.x",
        "fe_nodes": 2,
        "be_nodes": 3,
        "identities": {
            "ontometa_reader": "pass",
            "ontometa_ddl": "pass",
            "ontometa_etl": "pass",
            "ontometa_flink_sink": "fail",
        },
        "preflight": "pass",
    }
    with pytest.raises(MigrationGateError, match="最小权限身份"):
        service.record_step(
            db, batch_id=batch.id, step=1, passed=True, report=report,
            artifact_ids=[], operator="operator",
        )
    # Validation exceptions do not manufacture pass/fail evidence; operator can
    # explicitly record the blocking failure or fix it and retry step 1.
    report["identities"]["ontometa_flink_sink"] = "pass"
    evidence = service.record_step(
        db, batch_id=batch.id, step=1, passed=True, report=report,
        artifact_ids=[], operator="operator",
    )
    assert evidence.status == "pass"
    db.refresh(batch)
    assert batch.current_step == 1
    assert batch.doris_datasource_id == doris.id


def test_shadow_report_never_returns_business_rows():
    report = shadow_difference_report([
        {"name": "GMV", "legacy_result": [{"gmv": 12}], "doris_result": [{"gmv": 12}]},
        {"name": "orders", "legacy_result": [{"n": 3}], "doris_result": [{"n": 4}]},
    ])
    assert report["cases"] == 2
    assert report["matched"] == 1
    assert report["different"] == 1
    assert report["user_visible"] is False
    assert report["raw_results_retained"] is False
    assert "legacy_result" not in str(report)
    assert "doris_result" not in str(report)


def test_projection_coverage_is_per_referenced_object(db):
    ontology, doris, _deployment = _seed(db)
    assert readiness_error(
        db,
        datasource=doris,
        ontology_ids=[ontology.id],
        object_names=["customer"],
    ) is None
    error = readiness_error(
        db,
        datasource=doris,
        ontology_ids=[ontology.id],
        object_names=["customer", "sales_order"],
    )
    assert "sales_order" in error
    assert "不可查询" in error
    mapping = projection_mapping(
        db,
        datasource=doris,
        ontology_ids=[ontology.id],
        object_names=["customer"],
    )
    assert mapping["tables"] == {"customer": "dim_erp.customer"}
    assert mapping["projections"][0]["sync_watermark"] == "2026-08-22T07:00:00Z"


def test_active_batch_hides_shadow_until_step10_approval(db):
    ontology, _doris, _deployment = _seed(db)
    service = WarehouseMigrationService()
    batch = service.create_batch(
        db,
        ontology_id=ontology.id,
        approver="bootstrap-admin",
        rollback_owner="bootstrap-admin",
        observation_window_minutes=30,
    )
    assert "shadow" in cutover_error(db, [ontology.id])
    batch.current_step = 10
    batch.status = "cutover"
    batch.approved_by = "bootstrap-admin"
    batch.approved_at = datetime.utcnow()
    db.commit()
    assert cutover_error(db, [ontology.id]) is None


def test_migration_api_requires_order(client, admin_headers, db):
    ontology, _doris, _deployment = _seed(db)
    response = client.post(
        "/api/warehouse/migrations",
        headers=admin_headers,
        json={
            "ontology_id": ontology.id,
            "approver": "bootstrap-admin",
            "rollback_owner": "bootstrap-admin",
            "observation_window_minutes": 30,
            "operator": "operator",
        },
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["id"]
    skipped = client.post(
        f"/api/warehouse/migrations/{batch_id}/steps",
        headers=admin_headers,
        json={"step": 2, "passed": True, "report": {}, "operator": "operator"},
    )
    assert skipped.status_code == 422
    failed = client.post(
        f"/api/warehouse/migrations/{batch_id}/steps",
        headers=admin_headers,
        json={"step": 1, "passed": False, "report": {"reason": "blocked"}, "operator": "operator"},
    )
    assert failed.status_code == 200
    final = client.get(
        f"/api/warehouse/migrations/{batch_id}/report", headers=admin_headers
    )
    assert final.status_code == 200
    assert final.json()["batch"]["status"] == "blocked"
