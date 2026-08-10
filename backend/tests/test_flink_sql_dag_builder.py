"""P1-3：Flink SQL 计算 DAG 构建（Flink on YARN，BashOperator）单元测试。

钉住：flink run 命令形态（--file + -Dyarn.application.queue + extra_args）、
.sql 在 spec.tasks[].sql、依赖串联、batch/streaming（-d）、建表 DDL 去分号、
DAG 源码是合法 Python、幂等、staging swap。

统一执行架构（F/G 后）接口：build_flink_sql_dag(ontology_id, engine, tasks,
ddl_statements, config, ...)；DagBundle 是纯数据（dag_id/dag_source/spec）；
SQL 从 spec.tasks[].sql 读，运行期 sql_dir 走 xcom；swap 在 spec.swaps。
"""

from __future__ import annotations

import ast

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
        ontology_id="artifact-abc-123",
        engine="hive",
        tasks=[FlinkSqlTask(task_id="clean_customer", sql="INSERT INTO t SELECT 1;")],
        ddl_statements={"dim.customer": "CREATE TABLE t (x int)"},
        warehouse_conn_id="warehouse_hive",
        config=_cfg(),
    )
    kwargs.update(over)
    return build_flink_sql_dag(**kwargs)


def test_dag_id_is_stable_and_flink_scoped():
    assert flink_dag_id_for("artifact-abc-123").startswith("ontometa_flink_")
    assert flink_dag_id_for("x", "b0").endswith("__b0")


def test_sql_lands_in_task_spec():
    b = _build()
    task = b.spec["tasks"][0]
    assert task["task_id"] == "clean_customer"
    assert isinstance(task["sql"], str)
    assert "INSERT INTO t" in task["sql"]
    # .sql 文件名稳定，运行期由 xcom 的 sql_dir 拼绝对路径
    assert task["sql_file"] == "clean_customer.sql"


def test_command_submits_to_yarn_via_flink_run():
    b = _build()
    cmd = b.spec["tasks"][0]["command"]
    assert cmd.startswith("flink run -t yarn-per-job -p 1")
    assert "-c com.ontometa.flink.SqlRunner /opt/flink/flink-sql-runner.jar" in cmd
    # SqlRunner 只认 --file；路径是运行期 xcom 解析的 sql_dir + 文件名
    assert "--file " in cmd
    assert "xcom_pull" in cmd and cmd.endswith(".sql")


def test_streaming_task_submits_detached():
    b = _build(
        tasks=[FlinkSqlTask(task_id="rt", sql="INSERT INTO t SELECT 1;", detached=True)]
    )
    assert "-d" in b.spec["tasks"][0]["command"].split()


def test_batch_task_is_not_detached():
    b = _build()
    assert "-d" not in b.spec["tasks"][0]["command"].split()


def test_yarn_queue_and_extra_args_are_passed():
    b = _build(config=_cfg(yarn_queue="etl", extra_args=("-Dkey=val",)))
    cmd = b.spec["tasks"][0]["command"]
    assert "-Dyarn.application.queue=etl" in cmd
    assert "-Dkey=val" in cmd


def test_warehouse_ddl_is_split_into_single_statements():
    b = _build(ddl_statements={"ads_gmv": "CREATE TABLE ads_gmv (gmv decimal);"})
    assert b.spec["warehouse_ddl"] == ["CREATE TABLE ads_gmv (gmv decimal)"]


def test_credentials_only_via_env_jinja_not_in_sql_file():
    b = _build(
        tasks=[
            FlinkSqlTask(
                task_id="t1",
                sql="CREATE TABLE s (...) WITH ('url'='${ERP_URL}');",
                env={"ERP_URL": "{{ conn.erp.host }}"},
            )
        ]
    )
    # 凭据表达式在 endpoint_env，不在 sql 产物里
    assert b.spec["tasks"][0]["endpoint_env"] == {"ERP_URL": "{{ conn.erp.host }}"}
    assert "conn.erp.host" not in b.spec["tasks"][0]["sql"]


def test_dag_source_is_valid_python():
    b = _build(
        tasks=[
            FlinkSqlTask(task_id="a", sql="INSERT INTO t SELECT 1;"),
            FlinkSqlTask(task_id="b", sql="INSERT INTO u SELECT 2;"),
        ],
        ddl_statements={"t": "CREATE TABLE t (x int);"},
    )
    ast.parse(b.dag_source)  # 不抛即合法


def test_empty_tasks_and_ddl_yield_empty_dag():
    """空输入产出空 DAG（不抛）——空批的防御在上层 materialization_runner 跳过，
    build 层只忠实编排，职责分离。"""
    b = _build(tasks=[], ddl_statements={})
    assert b.spec["tasks"] == []
    assert b.spec["warehouse_ddl"] == []
    ast.parse(b.dag_source)  # 空 DAG 仍是合法 Python


def test_build_is_idempotent():
    a = _build()
    b = _build()
    assert a.dag_source == b.dag_source
    assert a.spec == b.spec


# ---------- staging / swap（统一执行 B2）----------


def test_swap_statements_land_in_spec_swaps():
    """全量搬运的 staging→正式表切换语句进 spec.swaps（逐条去分号）。"""
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv_customer", sql="INSERT INTO stg SELECT 1;")],
        swaps={"mv_customer": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    assert b.spec["swaps"]["mv_customer"] == ["ALTER TABLE dim REPLACE WITH TABLE stg"]


def test_no_swap_yields_no_swap_entry():
    b = _build(tasks=[FlinkSqlTask(task_id="mv", sql="INSERT INTO t SELECT 1;")])
    assert b.spec["swaps"].get("mv") in (None, [])


def test_swap_operator_wired_downstream_in_dag_source():
    """有 swap 的任务，DAG 源码里挂一个下游 swap_<task> SQLExecuteQueryOperator。"""
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv_customer", sql="INSERT INTO stg SELECT 1;")],
        swaps={"mv_customer": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    ast.parse(b.dag_source)  # 合法
    assert "swap_" in b.dag_source
    assert "SQLExecuteQueryOperator" in b.dag_source


def test_staging_dag_source_is_valid_python():
    b = _build(
        tasks=[FlinkSqlTask(task_id="mv", sql="INSERT INTO stg SELECT 1;")],
        ddl_statements={"dim": "CREATE TABLE dim (x int);", "stg": "CREATE TABLE stg LIKE dim;"},
        swaps={"mv": ["ALTER TABLE dim REPLACE WITH TABLE stg;"]},
    )
    ast.parse(b.dag_source)
