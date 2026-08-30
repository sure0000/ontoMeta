"""sync 进链测试——sync executor 走 run_sync 产数据搬运 DAG。"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.agents.executors.sync import SyncExecutor


def test_sync_without_target_datasource_fallback_handoff():
    """未配 target_datasource 时 sync 退回「仅产出」。"""
    executor = SyncExecutor()
    # drafter 产出的 spec 带全这些字段
    spec = {
        "ontology_id": "onto-x",
        "object_type": "Customer",
        "source": "erp.customers",
        "target": "dim_customer",
        "engine": "hive",
        "mode": "full",
        "preserved": False,
    }
    receipt = executor.execute(spec, context={})
    assert "handoff" in receipt
    # 统一执行架构：退回「仅产出」走 Flink 路径
    assert receipt["handoff"] == "flink_sql"
    assert "未配置" in receipt["note"]


def test_sync_with_target_datasource_calls_run_sync(monkeypatch):
    """配了 target_datasource，sync 走 run_sync 对单对象搬运。

    **必须是 run_sync 而不是 run_materialize**：同步任务负责确保并写入自己的统一目标表；
    独立物化入口只服务于没有源表的人工建模对象。
    """
    ontology_id = "sync-onto"
    object_name = "Customer"
    ds_id = "target-warehouse"

    # Mock materialization_runner.run_sync
    mock_receipt = {
        "dag_id": "ontometa_materialize_synconto__single",
        "dag_run_id": "sync_run_123",
        "batches": [{"dag_id": "ontometa_materialize_synconto__single", "table": "dim_customer"}],
        "note": "已投递单对象搬运 DAG",
    }
    mock_run = MagicMock(return_value=mock_receipt)

    from app.services import materialization_runner
    monkeypatch.setattr(materialization_runner, "run_sync", mock_run)

    executor = SyncExecutor()
    spec = {
        "ontology_id": ontology_id,
        "object_type": object_name,
        "source": "erp.customers",
        "target": "dim_customer",
        "target_datasource_id": ds_id,
        "engine": "hive",
        "mode": "full",
        "preserved": False,
    }
    context = {"artifact_id": "art-sync-123"}

    receipt = executor.execute(spec, context)

    # 验证走的是同步入口（run_sync），不是建表入口
    assert mock_run.called
    assert not hasattr(materialization_runner, "run"), "拆分后不该再有含混的 run()"
    args, kwargs = mock_run.call_args
    # db, ontology_id 是位置参数
    assert args[1] == ontology_id
    assert kwargs["target_datasource_id"] == ds_id
    assert kwargs["engine"] == "hive"
    assert kwargs["selected_targets"] == [object_name]  # 只搬单对象
    assert kwargs["artifact_id"] == "art-sync-123"

    # 验证回执带 dag_id（可被 compiler 串进链）
    assert receipt["dag_id"] == "ontometa_materialize_synconto__single"
    assert "dag_run_id" in receipt


def test_new_doris_sync_persists_contract_and_targets_only_ods(monkeypatch):
    from app.database import SessionLocal
    from app.models import (
        DataSource, DomainContext, IngestionContract, ObjectType, Ontology, Property
    )
    import uuid

    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        token = uuid.uuid4().hex[:8]
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:sync-{token}", name=f"sync-{token}")
        db.add(domain); db.flush()
        ontology = Ontology(domain_context_id=domain.id, status="published", version=2)
        db.add(ontology); db.flush()
        obj = ObjectType(
            ontology_id=ontology.id, name="Customer", display_name="客户",
            source_ref="urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)",
        )
        db.add(obj); db.flush()
        db.add_all([
            Property(object_type_id=obj.id, name="customer_id", display_name="ID", data_type="bigint"),
            Property(object_type_id=obj.id, name="modified_at", display_name="修改时间", data_type="timestamp"),
        ])
        source = DataSource(
            name="ERP", kind="mysql", purpose="business_source",
            catalog_name="erp", dsn_secret_ref="mysql://x",
        )
        doris = DataSource(
            name="Doris", kind="doris", purpose="warehouse", is_default_warehouse=True,
            dsn_secret_ref="mysql://doris",
        )
        db.add_all([source, doris]); db.commit()
        oid, source_id, doris_id = ontology.id, source.id, doris.id

    from app.services import materialization_runner
    from app.services import materialize_preflight
    from app.services.materialize_preflight import PreflightReport

    mock_run = MagicMock(return_value={"ok": True, "dag_id": "d1", "dag_run_id": "r1"})
    monkeypatch.setattr(materialization_runner, "run_sync", mock_run)
    monkeypatch.setattr(materialize_preflight, "run_preflight", lambda *a, **k: PreflightReport())
    spec = {
        "ontology_id": oid,
        "object_type": "Customer",
        "source": "erp.customer",
        "source_datasource_id": source_id,
        "target_datasource_id": doris_id,
        "target_ods_database": "ods_erp",
        "target_ods_table": "customer",
        "engine": "doris",
        "mode": "incremental",
        "primary_keys": ["customer_id"],
        "incremental_column": "modified_at",
        "initial_watermark": "2026-01-01 00:00:00",
        "delete_policy": "ignore",
    }
    receipt = SyncExecutor().execute(spec, {"artifact_id": "a1"})
    kwargs = mock_run.call_args.kwargs
    expected_table = f"ods_sync_{token}_customer"
    # 落点不是配置项：存量 Spec 里写着 ods_erp，执行时照样落到唯一的 ODS 库。
    assert kwargs["target_ods_database"] == "ods"
    assert kwargs["database_prefix"] is None
    # Spec 传入的 customer 被忽略：表名只由后端规则生成。
    assert kwargs["target_ods_tables"] == {"Customer": expected_table}
    assert kwargs["source_platforms"] == {"Customer": "mysql"}
    assert kwargs["source_datasource_id"] == source_id
    assert kwargs["initial_watermarks"] == {"Customer": "2026-01-01 00:00:00"}
    assert receipt["target_tables"] == [f"ods.{expected_table}"]
    assert receipt["watermark_after"] is None
    with SessionLocal() as db:
        row = db.query(IngestionContract).filter(IngestionContract.ontology_id == oid).one()
        assert row.status == "submitted"
        assert row.target_ods_database == "ods"
        assert row.target_ods_table == expected_table
        db.query(IngestionContract).filter(IngestionContract.ontology_id == oid).delete(
            synchronize_session=False
        )
        db.query(DataSource).filter(DataSource.id.in_([source_id, doris_id])).delete(
            synchronize_session=False
        )
        db.commit()


def test_sync_handles_materialization_error_gracefully(monkeypatch):
    """run_sync 失败时（未配 Airflow / 无可搬对象…）退回「仅产出」，不静默假装执行了。"""
    from app.services.materialization_runner import MaterializationError

    def mock_run_fail(*args, **kwargs):
        raise MaterializationError("未配 Airflow")

    from app.services import materialization_runner
    monkeypatch.setattr(materialization_runner, "run_sync", mock_run_fail)

    executor = SyncExecutor()
    spec = {
        "ontology_id": "onto-x",
        "object_type": "Customer",
        "source": "erp.customers",
        "target": "dim_customer",
        "target_datasource_id": "ds-1",
        "engine": "hive",
        "mode": "full",
        "preserved": False,
    }

    receipt = executor.execute(spec, context={})

    assert "handoff" in receipt
    assert "未执行" in receipt["note"] or "未配 Airflow" in receipt["note"]


def test_sync_missing_object_type_fallback():
    """spec 缺 object_type 时退回「仅产出」。"""
    executor = SyncExecutor()
    spec = {
        "ontology_id": "onto-x",
        "source": "erp.customers",
        "target": "dim_customer",
        "target_datasource_id": "ds-1",
        "engine": "hive",
        "mode": "full",
        "preserved": False,
        # object_type 缺失
    }
    receipt = executor.execute(spec, context={})
    assert "handoff" in receipt
    assert "缺object_type" in receipt["note"] or "缺 object_type" in receipt["note"]
