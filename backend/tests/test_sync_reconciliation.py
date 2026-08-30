from __future__ import annotations

from uuid import uuid4

from app.models import (
    DataSource,
    DomainContext,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.sync_reconciliation import reconcile_sync_receipt


def _contract(db) -> IngestionContract:
    token = uuid4().hex
    domain = DomainContext(
        id=f"domain-{token}",
        datahub_domain_id=f"urn:li:domain:{token}",
        name="sales",
    )
    ontology = Ontology(
        id=f"ontology-{token}",
        domain_context_id=domain.id,
        status="published",
        version=1,
    )
    obj = ObjectType(
        id=f"object-{token}",
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        status="published",
        source_ref="urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)",
    )
    datasource = DataSource(
        id=f"doris-{token}",
        name="默认 Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@doris/ods",
    )
    contract = IngestionContract(
        id=f"contract-{token}",
        ontology_id=f"ontology-{token}",
        ontology_version=1,
        object_type_id=f"object-{token}",
        source_datasource_id=f"source-{token}",
        source_physical_table="erp.customer",
        doris_datasource_id=datasource.id,
        target_ods_database="ods",
        target_ods_table="ods_sales_customer",
        mode="full",
        status="submitted",
    )
    db.add_all([domain, ontology, obj, datasource, contract])
    db.commit()
    return contract


def _cleanup(db, contract: IngestionContract) -> None:
    datasource_id = contract.doris_datasource_id
    ontology_id = contract.ontology_id
    object_id = contract.object_type_id
    ontology = db.get(Ontology, ontology_id)
    domain_id = ontology.domain_context_id if ontology else None
    db.query(WarehouseObjectProjection).filter(
        WarehouseObjectProjection.object_type_id == object_id
    ).delete(synchronize_session=False)
    db.query(OntologyWarehouseDeployment).filter(
        OntologyWarehouseDeployment.ontology_id == ontology_id
    ).delete(synchronize_session=False)
    db.delete(contract)
    db.flush()
    obj = db.get(ObjectType, object_id)
    if obj is not None:
        db.delete(obj)
    if ontology is not None:
        db.delete(ontology)
    if domain_id:
        domain = db.get(DomainContext, domain_id)
        if domain is not None:
            db.delete(domain)
    datasource = db.get(DataSource, datasource_id)
    if datasource is not None:
        db.delete(datasource)
    db.commit()


def test_sync_success_requires_nonempty_doris_target(db, monkeypatch):
    contract = _contract(db)
    monkeypatch.setattr(
        "app.services.sync_reconciliation.execute_sql",
        lambda **_kwargs: ([{"key": "row_count", "title": "row_count"}], [{"row_count": 42}]),
    )

    evidence = reconcile_sync_receipt(
        db,
        receipt={"ingestion_contract_id": contract.id},
        airflow_state="success",
    )

    assert evidence == {
        "status": "verified",
        "verified": True,
        "target_table": "ods.ods_sales_customer",
        "row_count": 42,
        "empty": False,
        "verified_at": evidence["verified_at"],
    }
    db.refresh(contract)
    assert contract.status == "ready"
    assert contract.last_success_at is not None
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(WarehouseObjectProjection.object_type_id == contract.object_type_id)
        .one()
    )
    assert projection.ods_database == "ods"
    assert projection.ods_table == "ods_sales_customer"
    assert projection.serving_database == "ods"
    assert projection.serving_table == "ods_sales_customer"
    assert projection.queryable is True
    from app.services.query_routing import projection_mapping

    mapping = projection_mapping(
        db,
        datasource=db.get(DataSource, contract.doris_datasource_id),
        ontology_ids=[contract.ontology_id],
        object_names=["customer"],
    )
    assert mapping["tables"] == {"customer": "ods.ods_sales_customer"}
    _cleanup(db, contract)


def test_sync_empty_doris_target_is_successful(db, monkeypatch):
    contract = _contract(db)
    monkeypatch.setattr(
        "app.services.sync_reconciliation.execute_sql",
        lambda **_kwargs: ([{"key": "row_count", "title": "row_count"}], [{"row_count": 0}]),
    )

    evidence = reconcile_sync_receipt(
        db,
        receipt={"ingestion_contract_id": contract.id},
        airflow_state="success",
    )

    assert evidence["verified"] is True
    assert evidence["status"] == "verified"
    assert evidence["row_count"] == 0
    assert evidence["empty"] is True
    assert "error" not in evidence
    db.refresh(contract)
    assert contract.status == "ready"
    assert contract.last_success_at is not None
    _cleanup(db, contract)
