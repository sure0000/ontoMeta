"""P3-1：sync 进链测试——sync executor 经 runner 通道产 dag_id。"""

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
    # 统一执行架构：退回「仅产出」走 Flink 路径，不再是 SeaTunnel
    assert receipt["handoff"] == "flink_sql"
    assert "未配置" in receipt["note"]


def test_sync_with_target_datasource_calls_materialization_runner(monkeypatch):
    """配了 target_datasource，sync 复用 materialization_runner 对单对象搬运。"""
    ontology_id = "sync-onto"
    object_name = "Customer"
    ds_id = "target-warehouse"

    # Mock materialization_runner.run
    mock_receipt = {
        "dag_id": "ontometa_materialize_synconto__single",
        "dag_run_id": "sync_run_123",
        "batches": [{"dag_id": "ontometa_materialize_synconto__single", "table": "dim_customer"}],
        "note": "已投递单对象搬运 DAG",
    }
    mock_run = MagicMock(return_value=mock_receipt)

    from app.services import materialization_runner
    monkeypatch.setattr(materialization_runner, "run", mock_run)

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

    # 验证复用了 materialization_runner.run
    assert mock_run.called
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


def test_sync_handles_materialization_error_gracefully(monkeypatch):
    """materialization_runner 失败时 sync 退回「仅产出」，不静默假装执行了。"""
    from app.services.materialization_runner import MaterializationError

    def mock_run_fail(*args, **kwargs):
        raise MaterializationError("未配 Airflow")

    from app.services import materialization_runner
    monkeypatch.setattr(materialization_runner, "run", mock_run_fail)

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
