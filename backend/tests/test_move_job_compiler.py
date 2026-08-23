"""统一执行架构 B1：搬运 JobSpec → FlinkSqlTask 编译器单元测试（无 DB）。

钉住：全量=batch 非 detached、增量/cdc=streaming detached + CDC 源、列改名走 column_map、
两端凭据占位符齐备、缺 target_table 报错。
"""

from __future__ import annotations

import pytest

from app.services.move_job_compiler import compile_move_task
from app.warehouse.jobs.base import ColumnMapping, JobEndpoint, JobSpec
from app.warehouse.logical_schema import LogicalColumn, LogicalTable


def _target_table() -> LogicalTable:
    return LogicalTable(
        name="dim_customer",
        database="dim_erp",
        layer="dim",
        columns=(
            LogicalColumn("customer_id", "bigint", "id", "客户ID"),
            LogicalColumn("customer_name", "varchar(140)", "name", "客户名"),
            LogicalColumn("amount", "decimal", "amount", "金额"),
        ),
    )


def _job(mode: str = "full", *, with_types: bool = True) -> JobSpec:
    return JobSpec(
        name="dim__customer",
        source=JobEndpoint(alias="erp_readonly", platform="postgres",
                           database="erp_ods", table="tab_customer"),
        target=JobEndpoint(alias="ontometa_ds_dw", platform="postgres",
                           database="dim_erp", table="dim_customer"),
        columns=(
            # ColumnMapping(source=物理, target=本体名)
            ColumnMapping(source="cust_id", target="customer_id"),
            ColumnMapping(source="cust_nm", target="customer_name"),
            ColumnMapping(source="amount", target="amount"),
        ),
        mode=mode,
        partition_key="created_at" if mode != "full" else None,
        incremental_column="created_at" if mode == "incremental" else None,
        initial_watermark="2026-01-01 00:00:00" if mode == "incremental" else None,
        layer="dim",
        entity_name="customer",
        target_table=_target_table() if with_types else None,
    )


def test_full_move_is_batch_not_detached():
    task = compile_move_task(_job("full"), engine="postgres")
    assert task.task_id == "dim__customer"
    assert task.detached is False
    assert "SET 'execution.runtime-mode' = 'batch';" in task.sql
    # 源用 JDBC（非 CDC），写目标物理名
    assert "'connector' = 'jdbc'" in task.sql
    assert "INSERT INTO `dim_customer`" in task.sql


def test_move_applies_column_rename_from_mapping():
    """本体名 ≠ 源物理名时，SELECT 用源物理列 AS 本体名。"""
    sql = compile_move_task(_job("full"), engine="postgres").sql
    assert "`cust_id` AS `customer_id`" in sql
    assert "`cust_nm` AS `customer_name`" in sql
    # 源表 CREATE 用物理列名
    assert "`cust_id`" in sql and "`cust_nm`" in sql


def test_move_carries_both_endpoints_credentials():
    """跨库作业两端凭据占位符都要在 env 里。"""
    task = compile_move_task(_job("full"), engine="postgres")
    assert "ERP_READONLY_USER" in task.env
    assert "ERP_READONLY_PASSWORD" in task.env
    assert "ONTOMETA_DS_DW_USER" in task.env
    assert "ONTOMETA_DS_DW_PASSWORD" in task.env
    # CDC host/port 占位符也在（base.py 新增，只增不改）
    assert "ERP_READONLY_HOSTNAME" in task.env


def test_doris_sink_injects_fenodes_from_airflow_extra():
    from dataclasses import replace
    job = _job("full")
    job = replace(
        job,
        target=JobEndpoint(
            alias="ontometa_doris_abc_flink", platform="doris",
            database="ods_erp", table="customer",
        ),
    )
    task = compile_move_task(job, engine="doris")
    assert "ONTOMETA_DORIS_ABC_FLINK_FENODES" in task.env
    assert "extra_dejson.get('fenodes'" in task.env["ONTOMETA_DORIS_ABC_FLINK_FENODES"]
    assert "${ONTOMETA_DORIS_ABC_FLINK_FENODES}" in task.sql


def test_incremental_move_is_bounded_jdbc_batch():
    task = compile_move_task(_job("incremental"), engine="postgres")
    assert task.detached is False
    assert "SET 'execution.runtime-mode' = 'batch';" in task.sql
    assert "'connector' = 'postgres-cdc'" not in task.sql
    assert "WHERE `created_at` >= '2026-01-01 00:00:00'" in task.sql


def test_cdc_move_is_streaming_detached():
    task = compile_move_task(
        _job("cdc"), engine="postgres", checkpoint_dir="file:///tmp/ckpt"
    )
    assert task.detached is True
    assert "'connector' = 'postgres-cdc'" in task.sql


def test_doris_cdc_hard_delete_enables_sink_delete():
    from dataclasses import replace
    job = _job("cdc")
    job = replace(
        job,
        target=JobEndpoint(
            alias="ontometa_doris_abc_flink", platform="doris",
            database="ods_erp", table="customer",
        ),
        delete_policy="hard_delete",
    )
    task = compile_move_task(job, engine="doris", checkpoint_dir="file:///tmp/ckpt")
    assert "'sink.enable-delete' = 'true'" in task.sql


def test_incremental_without_watermark_raises():
    job = _job("incremental")
    from dataclasses import replace
    with pytest.raises(ValueError, match="watermark"):
        compile_move_task(replace(job, initial_watermark=None), engine="postgres")


def test_missing_target_table_raises():
    with pytest.raises(ValueError, match="target_table"):
        compile_move_task(_job("full", with_types=False), engine="postgres")


def test_compile_is_idempotent():
    a = compile_move_task(_job("full"), engine="postgres")
    b = compile_move_task(_job("full"), engine="postgres")
    assert a.sql == b.sql and a.env == b.env and a.detached == b.detached
