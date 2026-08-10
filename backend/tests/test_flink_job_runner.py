"""P1-2：Flink 作业运行器单元测试。

钉住：未配 runner_jar 退回「仅产出」、配了则落盘+触发、回执带 dag_run_id/run_url、
触发失败如实记 error、幂等 run_id。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.flink_job_runner import FlinkJobError, run_flink_sql
from app.services.airflow_dag_builder import FlinkSqlTask
from app.services.dag_delivery import LocalFsDelivery


@pytest.fixture
def db_mock():
    return MagicMock()


def test_without_runner_jar_returns_handoff_mode(db_mock):
    """未配 runner_jar 时退回「仅产出」，不报错、不触发。"""
    with patch("app.services.flink_job_runner.env_settings") as env:
        env.flink_sql_runner_jar = ""
        with patch("app.services.flink_job_runner._settings") as settings:
            settings.get_airflow_runtime.return_value = MagicMock(available=True)
            receipt = run_flink_sql(
                db_mock,
                base="artifact-123",
                tasks=(FlinkSqlTask(task_id="clean", sql="INSERT INTO t SELECT 1;"),),
                warehouse_conn_id="warehouse",
            )
    assert receipt["execute_mode"] == "handoff"
    assert "未配置 Flink SqlRunner JAR" in receipt["note"]
    assert "dag_run_id" not in receipt
    # 「仅产出」必须真的产出东西：此前只给了几个从未落盘的文件名，人拿不到可执行的 SQL。
    # 数据搬运一律走 Flink，那这条 Flink SQL 就是这个任务的交付物。
    assert receipt["sql"] == {"clean": "INSERT INTO t SELECT 1;"}


def test_without_airflow_raises(db_mock):
    """未配 Airflow 直接报错（与 materialize 同逻辑）。"""
    with patch("app.services.flink_job_runner._settings") as settings:
        settings.get_airflow_runtime.return_value = MagicMock(available=False)
        with pytest.raises(FlinkJobError) as exc:
            run_flink_sql(
                db_mock,
                base="artifact-123",
                tasks=(FlinkSqlTask(task_id="t", sql="SELECT 1;"),),
                warehouse_conn_id="warehouse",
            )
    assert "未配置可用的 Airflow" in str(exc.value)


def test_with_runner_jar_writes_and_triggers(db_mock, tmp_path):
    """配了 runner_jar 则生成 DAG、落盘、触发，回执带 dag_run_id / run_url / artifacts。"""
    with patch("app.services.flink_job_runner.env_settings") as env:
        env.flink_sql_runner_jar = "/opt/flink/runner.jar"
        env.flink_sql_runner_class = "com.ontometa.flink.SqlRunner"
        env.flink_bin = "flink"
        env.flink_deploy_target = "yarn-per-job"
        env.flink_parallelism = 1
        env.flink_yarn_queue = ""
        with patch("app.services.flink_job_runner._settings") as settings:
            airflow = MagicMock(
                available=True,
                dags_dir=str(tmp_path / "dags"),
                jobs_dir=str(tmp_path / "jobs"),
                endpoint="http://airflow",
                max_active_tasks_per_dag=16,
                dag_parse_timeout=10.0,
            )
            # 投递器要给**真的**：MagicMock 的 deliver() 什么都不写，落盘断言会变成空转
            # （投递器是后加的 seam，这个用例当时没跟着更新）。
            airflow.build_delivery.return_value = LocalFsDelivery()
            settings.get_airflow_runtime.return_value = airflow
            with patch("app.services.flink_job_runner.AirflowClient") as client_cls:
                client = MagicMock()
                client_cls.return_value = client
                client.dag_exists.return_value = True
                client.trigger_dag.return_value = {"state": "queued"}
                client.run_url.return_value = "http://airflow/dags/x/grid?dag_run_id=y"

                receipt = run_flink_sql(
                    db_mock,
                    base="artifact-abc-123",
                    tasks=(FlinkSqlTask(task_id="clean_customer", sql="INSERT INTO t SELECT 1;"),),
                    warehouse_conn_id="warehouse_hive",
                    artifact_id="制品-456",
                )

    assert receipt["execute_mode"] == "flink_on_yarn"
    assert receipt["dag_id"].startswith("ontometa_flink_")
    assert receipt["dag_run_id"] == "ontometa__制品-456"
    assert receipt["state"] == "queued"
    assert receipt["run_url"] == "http://airflow/dags/x/grid?dag_run_id=y"
    assert "artifacts" in receipt
    # 落盘了 .sql 文件（按 <dags>/ontometa/<artifact_id>/jobs/ 子目录聚合）
    sql_file = next(
        (tmp_path / "dags").rglob(f"{receipt['dag_id']}__clean_customer.sql")
    )
    assert sql_file.exists()
    assert "INSERT INTO t SELECT 1;" in sql_file.read_text()


def test_trigger_error_is_recorded_in_receipt(db_mock, tmp_path):
    """触发失败时 error 字段如实记录、state=failed，不抛异常（与 materialize 同逻辑）。"""
    with patch("app.services.flink_job_runner.env_settings") as env:
        env.flink_sql_runner_jar = "/opt/flink/runner.jar"
        env.flink_sql_runner_class = "com.ontometa.flink.SqlRunner"
        env.flink_bin = "flink"
        env.flink_deploy_target = "yarn-per-job"
        env.flink_parallelism = 1
        env.flink_yarn_queue = ""
        with patch("app.services.flink_job_runner._settings") as settings:
            airflow = MagicMock(
                available=True,
                dags_dir=str(tmp_path / "dags"),
                jobs_dir=str(tmp_path / "jobs"),
                endpoint="http://airflow",
                max_active_tasks_per_dag=16,
                dag_parse_timeout=0.1,
            )
            settings.get_airflow_runtime.return_value = airflow
            with patch("app.services.flink_job_runner.AirflowClient") as client_cls:
                client = MagicMock()
                client_cls.return_value = client
                client.dag_exists.return_value = False  # 未解析到

                receipt = run_flink_sql(
                    db_mock,
                    base="artifact-123",
                    tasks=(FlinkSqlTask(task_id="t", sql="SELECT 1;"),),
                    warehouse_conn_id="warehouse",
                )

    assert receipt["state"] == "failed"
    assert "尚未解析到 DAG" in receipt["error"]
    assert receipt["run_url"] is None
