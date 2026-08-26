"""P1-2：Flink 作业运行器单元测试。

钉住：未配 runner_jar 退回「仅产出」、配了则落盘+触发、回执带 dag_run_id/run_url、
触发失败如实记 error、幂等 run_id。
"""

from __future__ import annotations

import re

import os
from unittest.mock import MagicMock, patch

import pytest

from app.services.flink_job_runner import FlinkJobError, run_flink_sql
from app.services.airflow_dag_builder import FlinkSqlTask
from tests.support.delivery import LocalTransportDelivery, make_runner_jar


@pytest.fixture
def db_mock():
    return MagicMock()


def test_without_runner_jar_returns_handoff_mode(db_mock):
    """未配 runner_jar 时退回「仅产出」，不报错、不触发。"""
    with patch("app.services.flink_job_runner._settings") as settings:
        settings.get_airflow_runtime.return_value = MagicMock(
            available=True, flink_sql_runner_jar=""
        )
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
    with patch("app.services.flink_job_runner._settings") as settings:
            airflow = MagicMock(
                available=True,
                dags_dir=str(tmp_path / "dags"),
                endpoint="http://airflow",
                max_active_tasks_per_dag=16,
                dag_parse_timeout=10.0,
                # Flink 参数现来自 Airflow 运行期配置（DB）
                flink_sql_runner_jar=make_runner_jar(tmp_path),
                flink_sql_runner_class="com.ontometa.flink.SqlRunner",
                flink_bin="flink",
                flink_deploy_target="yarn-per-job",
                flink_parallelism=1,
                flink_yarn_queue="",
                flink_checkpoint_dir="",
            )
            # 投递器要给**真的**：MagicMock 的 deliver() 什么都不写，落盘断言会变成空转。
            # LocalTransportDelivery 走真实 SshDelivery 逻辑，只把传输落到本地目录。
            airflow.build_delivery.return_value = LocalTransportDelivery()
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
    # run_id = 制品 id + 本次提交时刻：重跑一个失败的制品要拿到**新的**一次运行，
    # 定死成 ontometa__<制品> 时 Airflow 回 409 already exists，永远重试不了。
    assert receipt["dag_run_id"].startswith("ontometa__制品-456__")
    assert re.fullmatch(r"ontometa__制品-456__\d{8}T\d{6}Z", receipt["dag_run_id"])
    assert receipt["state"] == "queued"
    assert receipt["run_url"] == "http://airflow/dags/x/grid?dag_run_id=y"
    assert "artifacts" in receipt
    # 投递回执非空且带真实路径：此前调用方读的是不存在的 result.written，
    # 恒为 None/{}，物化的「产物路径」面板因此永远空白。
    assert receipt["artifacts"]  # 非空
    assert os.path.isabs(receipt["artifacts"]["sql_dir"])
    # SqlRunner jar 随包投到共享 _lib/（内容寻址文件名，与各制品目录平级）
    lib_dir = receipt["artifacts"]["lib_dir"]
    assert any(f.startswith("sql-runner-") for f in os.listdir(lib_dir))
    # 落盘了 .sql 文件（按 <dags>/ontometa/<artifact_id>/jobs/ 子目录聚合，名为 <task_id>.sql）
    sql_file = next(
        (tmp_path / "dags").rglob("clean_customer.sql")
    )
    assert sql_file.exists()
    assert "jobs" in sql_file.parts  # 落在 jobs/ 子目录
    assert "INSERT INTO t SELECT 1;" in sql_file.read_text()


def test_trigger_error_is_recorded_in_receipt(db_mock, tmp_path):
    """触发失败时 error 字段如实记录、state=failed，不抛异常（与 materialize 同逻辑）。"""
    with patch("app.services.flink_job_runner._settings") as settings:
            airflow = MagicMock(
                available=True,
                dags_dir=str(tmp_path / "dags"),
                endpoint="http://airflow",
                max_active_tasks_per_dag=16,
                dag_parse_timeout=0.1,
                flink_sql_runner_jar=make_runner_jar(tmp_path),
                flink_sql_runner_class="com.ontometa.flink.SqlRunner",
                flink_bin="flink",
                flink_deploy_target="yarn-per-job",
                flink_parallelism=1,
                flink_yarn_queue="",
                flink_checkpoint_dir="",
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
