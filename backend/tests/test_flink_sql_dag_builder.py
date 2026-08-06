"""P1-3：Flink SQL 计算 DAG 构建（Flink on YARN，BashOperator）单元测试。

钉住：flink run 命令形态、.sql 落 job_files、依赖串联、batch/streaming（-d）、
建表 DDL 去分号、DAG 源码是合法 Python、幂等。
"""

from __future__ import annotations

import ast

import pytest

from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    FlinkSubmitConfig,
    build_flink_sql_dag,
    flink_dag_id_for,
)


def _cfg(**over) -> FlinkSubmitConfig:
    base = dict(runner_jar="/opt/flink/flink-sql-runner.jar")
    base.update(over)
    return FlinkSubmitConfig(**base)


def _build(**over):
    kwargs = dict(
        base="artifact-abc-123",
        tasks=(FlinkSqlTask(task_id="clean_customer", sql="INSERT INTO t SELECT 1;"),),
        warehouse_conn_id="warehouse_hive",
        flink=_cfg(),
        jobs_dir="/opt/airflow/jobs",
    )
    kwargs.update(over)
    return build_flink_sql_dag(**kwargs)


def test_dag_id_is_stable_and_flink_scoped():
    assert flink_dag_id_for("artifact-abc-123").startswith("ontometa_flink_")
    assert flink_dag_id_for("x", "b0").endswith("__b0")


def test_sql_lands_as_text_job_file():
    b = _build()
    fname = f"{b.dag_id}__clean_customer.sql"
    assert isinstance(b.job_files[fname], str)
    assert "INSERT INTO t" in b.job_files[fname]


def test_command_submits_to_yarn_via_flink_run():
    b = _build()
    cmd = b.spec["tasks"][0]["command"]
    assert cmd.startswith("flink run -t yarn-per-job -p 1")
    assert "-c com.ontometa.flink.SqlRunner /opt/flink/flink-sql-runner.jar" in cmd
    assert "--file /opt/airflow/jobs/" in cmd and cmd.endswith(".sql")


def test_streaming_task_submits_detached():
    b = _build(
        tasks=(FlinkSqlTask(task_id="rt", sql="INSERT INTO t SELECT 1;", detached=True),)
    )
    assert "-d" in b.spec["tasks"][0]["command"].split()


def test_batch_task_is_not_detached():
    b = _build()
    assert "-d" not in b.spec["tasks"][0]["command"].split()


def test_yarn_queue_and_extra_args_are_passed():
    b = _build(flink=_cfg(yarn_queue="etl", extra_args=("-Dkey=val",)))
    cmd = b.spec["tasks"][0]["command"]
    assert "-Dyarn.application.queue=etl" in cmd
    assert "-Dkey=val" in cmd


def test_warehouse_ddl_is_split_into_single_statements():
    b = _build(warehouse_ddl=("CREATE TABLE ads_gmv (gmv decimal);",))
    assert b.spec["warehouse_ddl"] == ["CREATE TABLE ads_gmv (gmv decimal)"]


def test_credentials_only_via_env_jinja_not_in_sql_file():
    b = _build(
        tasks=(
            FlinkSqlTask(
                task_id="t1",
                sql="CREATE TABLE s (...) WITH ('url'='${ERP_URL}');",
                env={"ERP_URL": "{{ conn.erp.host }}"},
            ),
        )
    )
    # 凭据表达式在 env，不在 sql 产物里
    assert b.spec["tasks"][0]["env"] == {"ERP_URL": "{{ conn.erp.host }}"}
    assert "conn.erp.host" not in b.job_files[f"{b.dag_id}__t1.sql"]


def test_dag_source_is_valid_python():
    b = _build(
        tasks=(
            FlinkSqlTask(task_id="a", sql="INSERT INTO t SELECT 1;"),
            FlinkSqlTask(task_id="b", sql="INSERT INTO u SELECT 2;", depends_on=("a",)),
        ),
        warehouse_ddl=("CREATE TABLE t (x int);",),
    )
    ast.parse(b.dag_source)  # 不抛即合法


def test_empty_tasks_rejected():
    with pytest.raises(ValueError):
        _build(tasks=())


def test_build_is_idempotent():
    a = _build()
    b = _build()
    assert a.dag_source == b.dag_source
    assert a.spec == b.spec
    assert a.job_files == b.job_files
