"""Phase 2 ingestion contract and Doris ODS boundary tests."""

from __future__ import annotations

import pytest
import uuid

from app.models import DataSource, DomainContext, IngestionContract, ObjectType, Ontology, Property
from app.services.ingestion_contract import IngestionContractError, IngestionContractService

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)"


@pytest.fixture
def ingestion_seed(db):
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:ingestion-{token}",
        name=f"ingestion-{token}",
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=7)
    db.add(ontology)
    db.flush()
    obj = ObjectType(
        ontology_id=ontology.id, name="customer", display_name="客户",
        source_ref=_URN, table_role="business_object",
    )
    db.add(obj)
    db.flush()
    source = DataSource(
        name="ERP", kind="mysql", purpose="business_source", enabled=True,
        dsn_secret_ref="mysql+pymysql://erp@db:3306/erp",
    )
    doris = DataSource(
        name="Doris", kind="doris", purpose="warehouse", enabled=True,
        is_default_warehouse=True, dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add_all([
        Property(object_type_id=obj.id, name="customer_id", display_name="客户ID", data_type="bigint"),
        Property(object_type_id=obj.id, name="modified_at", display_name="修改时间", data_type="timestamp"),
    ])
    db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
        {DataSource.is_default_warehouse: False}, synchronize_session=False
    )
    db.add_all([source, doris])
    db.commit()
    return ontology, obj, source, doris


def _payload(obj, source, doris, **overrides):
    data = {
        "object_type_id": obj.id,
        "source_datasource_id": source.id,
        "source_physical_table": "erp.customer",
        "source_mapping": {"customer_id": "id"},
        "doris_datasource_id": doris.id,
        "target_ods_database": "ods_erp",
        "target_ods_table": "customer",
        "mode": "full",
        "primary_keys": ["customer_id"],
        "delete_policy": "ignore",
        "flink_params": {},
        "status": "active",
    }
    data.update(overrides)
    return data


def test_full_contract_persists_versioned_ods_binding(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    row = IngestionContractService().upsert(
        db, ontology.id, _payload(obj, source, doris)
    )
    assert row.ontology_version == 7
    assert row.target_ods_database == "ods_erp"
    assert row.target_ods_table == f"ods_ingestion_{ontology.domain_context.name.rsplit('-', 1)[-1]}_customer"
    assert row.doris_datasource_id == doris.id
    assert row.mode == "full"


def test_contract_overrides_caller_supplied_ods_table(db, ingestion_seed):
    """ODS 表名是后端固定规则，API/Agent 传入的自定义值不能覆盖。"""
    ontology, obj, source, doris = ingestion_seed
    row = IngestionContractService().upsert(
        db, ontology.id,
        _payload(obj, source, doris, target_ods_table="caller_defined_name"),
    )
    assert row.target_ods_table.startswith("ods_ingestion_")
    assert row.target_ods_table.endswith("_customer")
    assert row.target_ods_table != "caller_defined_name"


def test_contract_rejects_non_ods_target(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    with pytest.raises(IngestionContractError, match="ODS"):
        IngestionContractService().upsert(
            db, ontology.id,
            _payload(obj, source, doris, target_ods_database="dwd_erp"),
        )


def test_incremental_requires_key_column_and_initial_watermark(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    with pytest.raises(IngestionContractError, match="initial_watermark"):
        IngestionContractService().upsert(
            db, ontology.id,
            _payload(
                obj, source, doris, mode="incremental",
                incremental_column="modified_at", initial_watermark=None,
            ),
        )
    row = IngestionContractService().upsert(
        db, ontology.id,
        _payload(
            obj, source, doris, mode="incremental",
            incremental_column="modified_at",
            initial_watermark="2026-01-01 00:00:00",
        ),
    )
    assert row.incremental_column == "modified_at"
    assert row.initial_watermark == "2026-01-01 00:00:00"


def test_cdc_requires_sequence_and_checkpoint(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    with pytest.raises(IngestionContractError, match="sequence_column"):
        IngestionContractService().upsert(
            db, ontology.id,
            _payload(obj, source, doris, mode="cdc"),
        )
    with pytest.raises(IngestionContractError, match="checkpoint"):
        IngestionContractService().upsert(
            db, ontology.id,
            _payload(obj, source, doris, mode="cdc", sequence_column="modified_at"),
        )
    row = IngestionContractService().upsert(
        db, ontology.id,
        _payload(
            obj, source, doris, mode="cdc", sequence_column="modified_at",
            delete_policy="hard_delete",
            flink_params={"flink_checkpoint_dir": "hdfs://ns/ckpt/customer"},
        ),
    )
    assert row.checkpoint_path == "hdfs://ns/ckpt/customer"
    assert row.delete_policy == "hard_delete"


def test_batch_reconciliation_advances_only_on_success(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    service = IngestionContractService()
    row = service.upsert(db, ontology.id, _payload(obj, source, doris))
    same = service.reconcile_task_result(
        db, row.id, task_state="running", result={"watermark_after": "w2"}
    )
    assert same.status == "active"
    ready = service.reconcile_task_result(
        db, row.id, task_state="success", result={"rows_written": 10}
    )
    assert ready.status == "ready"
    assert ready.last_success_at is not None


def test_incremental_watermark_advances_only_after_success(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    service = IngestionContractService()
    row = service.upsert(
        db, ontology.id,
        _payload(
            obj, source, doris, mode="incremental",
            incremental_column="modified_at", initial_watermark="w1",
        ),
    )
    service.reconcile_task_result(
        db, row.id, task_state="failed", result={"watermark_after": "w2"}
    )
    assert db.get(IngestionContract, row.id).sync_watermark is None
    service.reconcile_task_result(
        db, row.id, task_state="success", result={"watermark_after": "w2"}
    )
    assert db.get(IngestionContract, row.id).sync_watermark == "w2"


def test_cdc_requires_real_flink_job_id_to_run(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    service = IngestionContractService()
    row = service.upsert(
        db, ontology.id,
        _payload(
            obj, source, doris, mode="cdc", sequence_column="modified_at",
            flink_params={"flink_checkpoint_dir": "hdfs://ns/ckpt/customer"},
        ),
    )
    with pytest.raises(IngestionContractError, match="flink_job_id"):
        service.reconcile_task_result(db, row.id, task_state="success", result={})
    running = service.reconcile_task_result(
        db, row.id, task_state="success", result={"flink_job_id": "job-123"}
    )
    assert running.status == "running"
    assert running.flink_job_id == "job-123"


def test_cdc_health_check_uses_configured_flink_rest(db, ingestion_seed, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from app.services.flink_health import check_ingestion_job
    from app.services.settings_service import SettingsService

    ontology, obj, source, doris = ingestion_seed
    service = IngestionContractService()
    row = service.upsert(
        db, ontology.id,
        _payload(
            obj, source, doris, mode="cdc", sequence_column="modified_at",
            flink_params={"flink_checkpoint_dir": "hdfs://ns/ckpt/customer"},
        ),
    )
    row.flink_job_id = "a" * 32
    db.commit()
    monkeypatch.setattr(
        SettingsService,
        "get_airflow_runtime",
        lambda self, _db: SimpleNamespace(flink_rest_endpoint="http://flink:8081"),
    )
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"state": "RUNNING", "start-time": 1})
    ))
    try:
        health = check_ingestion_job(db, row.id, client=client)
    finally:
        client.close()
    assert health["healthy"] is True
    assert health["flink_job_id"] == "a" * 32
    assert db.get(IngestionContract, row.id).status == "running"


def test_non_default_or_non_doris_sink_is_rejected(db, ingestion_seed):
    ontology, obj, source, doris = ingestion_seed
    doris.is_default_warehouse = False
    db.commit()
    with pytest.raises(IngestionContractError, match="默认 Doris"):
        IngestionContractService().upsert(
            db, ontology.id, _payload(obj, source, doris)
        )
