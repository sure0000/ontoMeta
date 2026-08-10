"""Flink SQL 作业运行器（P1-2）——为单个计算制品生成 DAG、落盘、触发、回读。

与 materialization_runner 同构：ontoMeta 只生成产物（.sql 文件 + DAG），投递给 Airflow
并触发一次运行，**不在本进程里执行 Flink**。复用现有的「write → AirflowClient.trigger →
_wait_for_parse → 回读 DagRun」通道，不新建执行框架（不变量 2）。

职责边界：本模块把「一个或多个 Flink SQL 任务」包装成一次 Airflow 提交。SELECT 体由
executor 生成（它最懂清洗规则 / 聚合口径），本模块调 flink_sql_generator 组装完整脚本、
调 dag_builder 产 DAG、落盘、触发、回执带 dag_run_id / run_url / state。
"""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.connectors.airflow import AirflowClient, AirflowError
from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    FlinkSubmitConfig,
    build_flink_sql_dag,
)
from app.services.settings_service import SettingsService

_settings = SettingsService()


class FlinkJobError(RuntimeError):
    """Flink 作业提交 / 触发失败，面向用户可读。"""


def _wait_for_parse(
    client: AirflowClient, dag_ids: list[str], timeout: float
) -> set[str]:
    """等 Airflow 解析到这批 DAG，返回已解析到的 dag_id 集合（与 materialization 同逻辑）。"""
    deadline = time.monotonic() + timeout
    pending = list(dag_ids)
    seen: set[str] = set()
    while pending:
        for dag_id in list(pending):
            try:
                if client.dag_exists(dag_id):
                    seen.add(dag_id)
                    pending.remove(dag_id)
            except AirflowError:
                pass
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(2)
    return seen


def run_flink_sql(
    db: Session,
    *,
    base: str,
    tasks: tuple[FlinkSqlTask, ...],
    warehouse_conn_id: str,
    warehouse_ddl: tuple[str, ...] = (),
    schedule: str | None = None,
    dag_id_suffix: str | None = None,
    artifact_id: str | None = None,
    swaps: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """为一批 Flink SQL 任务生成 DAG、落盘、触发，返回回执。

    Args:
        db: 数据库会话（读 Airflow 设置）
        base: DAG id 的基（制品 id / 本体 id）
        tasks: 一个或多个 Flink SQL 任务（搬运 / transform 清洗 / metric 聚合）
        warehouse_conn_id: 数仓连接（建 sink 表 / 跑 swap 用，Airflow Connection id）
        warehouse_ddl: 建 sink / staging 表的数仓 DDL（空则不建）
        schedule: cron（空 = 只手动触发）
        dag_id_suffix: 批次 / cron 分组后缀
        artifact_id: 制品 id（作 dag_run_id，重复提交幂等）
        swaps: ``{task_id: [swap SQL]}``，全量搬运 staging→正式表原子切换（见 build_flink_sql_dag）

    Returns:
        回执 dict：dag_id / dag_run_id / state / run_url / artifacts（落盘路径）/ error（若有）

    Raises:
        FlinkJobError: 未配 Flink / Airflow、落盘 / 触发失败
    """
    airflow = _settings.get_airflow_runtime(db)
    if not airflow.available:
        raise FlinkJobError(
            "未配置可用的 Airflow（需在设置页填 endpoint 并启用），无法执行 Flink 作业"
        )

    # Flink on YARN 部署参数：从 env 读默认值（部署基础设施）。缺 runner_jar 退回「仅产出」。
    runner_jar = env_settings.flink_sql_runner_jar.strip()
    if not runner_jar:
        return {
            "base": base,
            "execute_mode": "handoff",
            "note": "未配置 Flink SqlRunner JAR（FLINK_SQL_RUNNER_JAR），ontoMeta 只产出 SQL，不执行",
            "sql_files": [t.task_id + ".sql" for t in tasks],
            # **把 SQL 本身带出来**：此前这里只给了几个从未落盘的文件名，"仅产出"
            # 产出的是个空回执，人拿不到任何可执行的东西。数据搬运一律走 Flink，
            # 那这条 Flink SQL 就是这个任务的交付物——配上 JAR 后原样提交的就是它。
            "sql": {t.task_id: t.sql for t in tasks},
        }

    flink = FlinkSubmitConfig(
        sql_runner_jar=runner_jar,
        yarn_queue=env_settings.flink_yarn_queue.strip() or "default",
        checkpoint_dir=env_settings.flink_checkpoint_dir.strip(),
    )

    # 有 artifact_id 时 .sql 落 <dags_dir>/ontometa/<artifact_id>/jobs/；flink run --file
    # 的路径写死进 command，必须与该落盘目录对齐（worker 上可见）。空则退回扁平 jobs_dir。
    if artifact_id:
        _flink_jobs_dir = os.path.join(
            airflow.dags_dir, "ontometa", artifact_id, "jobs"
        )
    else:
        _flink_jobs_dir = airflow.jobs_dir

    # 生成 DAG（.sql 文件 + DAG 源码 + spec.json）
    # build_flink_sql_dag 的新签名：ontology_id + engine + ddl_statements。
    # 本 runner 是通用接口（transform/metric/搬运共用），用 base 当 ontology_id，
    # engine 固定 hive（数仓默认引擎），warehouse_ddl 是 DDL 语句列表（不是 dict）。
    bundle = build_flink_sql_dag(
        ontology_id=base,
        engine="hive",  # 数仓默认引擎（transform/metric sink 表默认建在 hive）
        tasks=list(tasks),
        ddl_statements={f"ddl_{i}": ddl for i, ddl in enumerate(warehouse_ddl)},
        constraints=None,
        swaps=swaps,
        config=flink,
        schedule=schedule,
        dag_id_suffix=dag_id_suffix,
        warehouse_conn_id=warehouse_conn_id,
        max_active_tasks=airflow.max_active_tasks_per_dag,
    )

    # 落盘：DagBundle 现在是纯数据，不含 write() 方法，手动写文件
    delivery = airflow.build_delivery()
    try:
        import json
        from pathlib import Path

        dags_dir = Path(airflow.dags_dir)
        dags_dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在

        # 写 DAG Python 文件
        dag_file = dags_dir / bundle.dag_filename
        dag_file.write_text(bundle.dag_source, encoding="utf-8")

        # 写边车 JSON spec
        spec_file = dags_dir / bundle.spec_filename
        spec_file.write_text(json.dumps(bundle.spec, ensure_ascii=False, indent=2), encoding="utf-8")

        # 写 SQL 文件（每个任务一个 .sql）
        for task_spec in bundle.spec["tasks"]:
            sql_file = dags_dir / task_spec["sql_file"]
            sql_file.write_text(task_spec["sql"], encoding="utf-8")

        written = {
            "dag_file": str(dag_file),
            "spec_file": str(spec_file),
            "sql_files": [str(dags_dir / t["sql_file"]) for t in bundle.spec["tasks"]],
        }
    except OSError as exc:
        raise FlinkJobError(
            f"DAG 投递失败（{airflow.dags_dir}）：{exc}"
        ) from exc

    # 触发
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    run_id = f"ontometa__{artifact_id or 'manual'}"
    error: str | None = None
    triggered: dict[str, Any] = {}
    try:
        # 等 Airflow 解析到 DAG（首次提交常见于解析未完成）
        parsed = _wait_for_parse(client, [bundle.dag_id], airflow.dag_parse_timeout)
        if bundle.dag_id not in parsed:
            error = (
                "Airflow 尚未解析到 DAG，请检查 dags 目录是否双向可见"
                "（ontoMeta 写的与 Airflow 读的是否同一路径）"
            )
        else:
            client.unpause_dag(bundle.dag_id)
            triggered = client.trigger_dag(bundle.dag_id, dag_run_id=run_id)
    except AirflowError as exc:
        error = str(exc)
    finally:
        client.close()

    return {
        "base": base,
        "execute_mode": "flink_on_yarn",
        "dag_id": bundle.dag_id,
        "dag_run_id": run_id,
        "state": triggered.get("state") or ("failed" if error else "queued"),
        "run_url": client.run_url(bundle.dag_id, run_id) if not error else None,
        "schedule": schedule,
        "flink_runner_jar": flink.runner_jar,
        "tasks": [{"task_id": t.task_id, "sql_file": f"{bundle.dag_id}__{t.task_id}.sql"} for t in tasks],
        "artifacts": written,
        "error": error,
    }
