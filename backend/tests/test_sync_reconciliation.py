from __future__ import annotations

from uuid import uuid4

from app.models import DataSource, IngestionContract
from app.services.sync_reconciliation import reconcile_sync_receipt


def _contract(db) -> IngestionContract:
    token = uuid4().hex
    datasource = DataSource(
        id=f"doris-{token}",
        name="默认 Doris",
        kind="doris",
        purpose="warehouse",
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
    db.add_all([datasource, contract])
    db.commit()
    return contract


def _cleanup(db, contract: IngestionContract) -> None:
    datasource_id = contract.doris_datasource_id
    db.delete(contract)
    db.flush()
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
