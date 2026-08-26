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


def test_empty_password_renders_empty_not_the_word_none():
    """密码/库名为 NULL 的 Connection 渲染出空串，**不是字面量 "None"**。

    Airflow 的 Jinja 不设 finalize，``{{ conn.x.password }}`` 遇到 None 会渲染成四个
    字母 ``None``，连接器于是拿着一个叫 "None" 的密码去认证。Doris 默认的 root 正是
    空密码，症状是 ``Access denied for user 'root@…' (using password: YES)``——读起来
    像密码配错了，实际是我们凭空发明了一个密码。
    """
    import jinja2

    task = compile_move_task(_job("full"), engine="postgres")

    class _Conn:
        login = "root"
        password = None  # Doris 默认 root 无密码
        host = "10.0.0.1"
        port = 9030
        schema = None

    env = jinja2.Environment()  # 与 Airflow 一样不设 finalize
    rendered = {
        k: env.from_string(v).render(conn={"erp_readonly": _Conn(), "ontometa_ds_dw": _Conn()})
        for k, v in task.env.items()
    }
    assert rendered["ERP_READONLY_PASSWORD"] == ""
    assert rendered["ONTOMETA_DS_DW_PASSWORD"] == ""
    assert "None" not in rendered["ERP_READONLY_URL"]  # 库名 None 会拼成 `/None`
    assert rendered["ERP_READONLY_USER"] == "root"  # 有值的字段照常渲染
    assert rendered["ERP_READONLY_PORT"] == "9030"


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


def _doris_job():
    from dataclasses import replace
    return replace(
        _job("full"),
        target=JobEndpoint(
            alias="ontometa_doris_abc_flink", platform="doris",
            database="ods_erp", table="customer",
        ),
    )


def test_doris_sink_omits_benodes_unless_configured():
    """没配 BE 地址就不发 ``benodes``：连接器照旧问 FE 要 BE 在哪。

    发一个没人赋值的占位符更糟——SqlRunner 对缺失的环境变量是直接报错的
    （见 tools/flink-sql-runner），配置没动过的部署会当场跑不动。
    """
    sql = compile_move_task(_doris_job(), engine="doris").sql
    assert "'benodes'" not in sql


def test_doris_sink_label_prefix_is_per_run():
    """stream load 的 label 前缀取自运行期（DagRun 时刻 + 重试次数），不是固定值。

    连接器默认按表名派生 label 前缀，于是同一张表第二次搬运直接
    ``[LABEL_ALREADY_EXISTS] Label [...] has already been used``：首次成功之后
    这张表就再也搬不动了，而错误只出现在 Flink 作业里。
    """
    task = compile_move_task(_doris_job(), engine="doris")
    assert "'sink.label-prefix' = '${ONTOMETA_DORIS_ABC_FLINK_LOAD_LABEL}'" in task.sql
    label = task.env["ONTOMETA_DORIS_ABC_FLINK_LOAD_LABEL"]
    assert "ts_nodash" in label and "try_number" in label


def test_doris_sink_uses_benodes_when_configured():
    """配了就直接把 BE 地址交给连接器，不再采信 FE 给的那份。

    容器化 Doris 的 BE 在 FE 里登记成 127.0.0.1，集群外的 Flink 照着连是
    ``Connect to 127.0.0.1:8040 failed``——错误发生在 TaskManager 里，提交回执
    只会说一句 "Failed to wait job finish"，不看 Flink 作业日志根本找不到。
    """
    task = compile_move_task(_doris_job(), engine="doris", target_benodes=True)
    assert "'benodes' = '${ONTOMETA_DORIS_ABC_FLINK_BENODES}'" in task.sql
    # 值同样只走 Airflow Connection 的 extra，不进产物（凭据/端点不落 Spec）
    assert "extra_dejson.get('benodes'" in task.env["ONTOMETA_DORIS_ABC_FLINK_BENODES"]


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
