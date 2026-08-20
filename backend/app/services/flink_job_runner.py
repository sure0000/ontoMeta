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

from app.connectors.airflow import AirflowClient, AirflowError
from app.services.airflow_dag_builder import (
    FlinkSqlTask,
    build_flink_sql_dag,
)
from app.services import flink_params
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
    flink_task_params: dict[str, Any] | None = None,
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
        flink_task_params: **这个任务自己的** Flink 提交参数（并行度/队列/提交目标/
            checkpoint/额外 -D），来自制品 Spec；留空的项跟随设置页默认值。

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

    # Flink on YARN 提交参数：设置页（DB）给默认值，**制品 Spec 逐任务覆盖**——
    # 大搬运与小聚合对并行度/队列的要求不同，共用一套参数总有一边是错的。
    try:
        task_params = flink_params.normalize(flink_task_params)
    except flink_params.FlinkParamError as exc:
        raise FlinkJobError(f"任务的 Flink 执行参数非法：{exc}") from exc
    runner_jar = (airflow.flink_sql_runner_jar or "").strip()
    if not runner_jar:
        return {
            "base": base,
            "execute_mode": "handoff",
            "note": "未配置 Flink SqlRunner JAR（设置页 → Airflow/Flink），ontoMeta 只产出 SQL，不执行",
            "sql_files": [t.task_id + ".sql" for t in tasks],
            # **把 SQL 本身带出来**：此前这里只给了几个从未落盘的文件名，"仅产出"
            # 产出的是个空回执，人拿不到任何可执行的东西。数据搬运一律走 Flink，
            # 那这条 Flink SQL 就是这个任务的交付物——配上 JAR 后原样提交的就是它。
            "sql": {t.task_id: t.sql for t in tasks},
        }

    flink = flink_params.resolve_config(
        airflow, task_params, runner_jar=runner_jar, queue_fallback="default"
    )

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
        # 无 artifact_id 时产物落扁平 dags_dir，DAG 里算 lib_dir 不能再往上一级
        nested_layout=bool(artifact_id),
    )

    # 落盘：DagBundle 是纯数据（F/G 后不含 write 方法），交给统一的 DagDelivery 投递器。
    # 产物按 <dags>/ontometa/<artifact_id>/ 子目录聚合，.sql 落其 jobs/（与 read_spec 的
    # sql_dir 对齐）；无 artifact_id 时退回扁平 dags_dir。
    if artifact_id:
        _out_dir = os.path.join(airflow.dags_dir, "ontometa", artifact_id)
    else:
        _out_dir = airflow.dags_dir
    _jobs_dir = os.path.join(_out_dir, "jobs")
    delivery = airflow.build_delivery()
    try:
        job_files = {t["sql_file"]: t["sql"] for t in bundle.spec["tasks"]}
        # SqlRunner jar 随包投递：内容寻址（文件名含 sha12），落远端 ontometa/_lib/，
        # 多个制品共享一份。bundle 无 jar = handoff 模式（只产 SQL 不执行）。
        lib_files = {}
        if bundle.runner_jar_filename:
            with open(bundle.runner_jar_path, "rb") as fh:
                lib_files[bundle.runner_jar_filename] = fh.read()
        result = delivery.deliver(
            dags_dir=_out_dir,
            jobs_dir=_jobs_dir,
            dag_filename=bundle.dag_filename,
            dag_source=bundle.dag_source,
            spec_filename=bundle.spec_filename,
            spec=bundle.spec,
            job_files=job_files,
            lib_files=lib_files,
        )
        # 投递器给的是**远端**路径——ontoMeta 与 Airflow 不同机时，本地视角的路径是错的。
        written = result.files_written
    except Exception as exc:  # noqa: BLE001 —— 投递失败（含 OSError / DagDeliveryError）
        raise FlinkJobError(f"DAG 投递失败：{exc}") from exc

    # 触发
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
    )
    run_id = f"ontometa__{artifact_id or 'manual'}"
    error: str | None = None
    triggered: dict[str, Any] = {}
    try:
        # 等 Airflow 解析到 DAG（首次提交常见于解析未完成）
        parsed = _wait_for_parse(client, [bundle.dag_id], airflow.dag_parse_timeout)
        if bundle.dag_id not in parsed:
            error = (
                "Airflow 尚未解析到 DAG。产物经 SSH 投递到 Airflow 主机的 dags 目录"
                "（设置页 → Airflow），请确认：①投递的 dags_dir 与 Airflow 的 "
                "dags_folder 是同一目录；②DAG 源码无 import 错误（看 Airflow UI 的 "
                "import errors）；③dag_dir_list_interval 较长的首次解析延迟。"
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
        # 这次**真正生效**的提交参数（设置页默认 + 本任务覆盖后的结果）。参数现在逐任务
        # 不同，"去设置页看一眼" 已不再是答案，回执必须自己说清用了什么。
        "flink": flink_params.effective(flink, task_params),
        "tasks": [{"task_id": t.task_id, "sql_file": f"{bundle.dag_id}__{t.task_id}.sql"} for t in tasks],
        # L4 血缘：把每个任务的 inlets/outlets URN 一并带出，供任务状态块展示
        # 「本次启动了哪些任务 + 谁依赖谁」。空（未解析到）则不带。
        "lineage": [
            {
                "task_id": t.task_id,
                "source_urns": list(t.source_urns),
                "target_urn": t.target_urn,
            }
            for t in tasks
            if t.source_urns or t.target_urn
        ],
        "artifacts": written,
        "error": error,
    }
