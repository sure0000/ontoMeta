"""Deliver and trigger Doris-native SQL DAGs through Airflow."""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError, build_run_id
from app.services.doris_sql_dag_builder import build_doris_sql_dag
from app.services.settings_service import SettingsService

_settings = SettingsService()


class DorisJobError(RuntimeError):
    pass


def _wait_for_parse(client: AirflowClient, dag_id: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if client.dag_exists(dag_id):
                return True
        except AirflowError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def run_doris_sql(
    db: Session,
    *,
    artifact_id: str,
    kind: str,
    conn_id: str,
    execute_sql: list[str],
    precheck_sql: list[str] | None = None,
    setup_sql: list[str] | None = None,
    quality_sql: list[str] | None = None,
    publish_sql: list[str] | None = None,
    schedule: str | None = None,
    source_tables: list[str] | None = None,
    target_tables: list[str] | None = None,
) -> dict[str, Any]:
    if not execute_sql:
        raise DorisJobError("Doris SQL 任务没有可执行语句")
    airflow = _settings.get_airflow_runtime(db)
    if not airflow.available:
        return {
            "execute_mode": "handoff",
            "compute_engine": "doris",
            "target_engine": "doris",
            "sql": list(execute_sql),
            "note": "未配置可用 Airflow，ontoMeta 只产出 Doris SQL，不执行",
        }
    bundle = build_doris_sql_dag(
        artifact_id=artifact_id,
        kind=kind,
        conn_id=conn_id,
        precheck_sql=precheck_sql,
        setup_sql=setup_sql,
        execute_sql=execute_sql,
        quality_sql=quality_sql,
        publish_sql=publish_sql,
        schedule=schedule,
    )
    out_dir = os.path.join(airflow.dags_dir, "ontometa", artifact_id)
    try:
        delivered = airflow.build_delivery().deliver(
            dags_dir=out_dir,
            jobs_dir=os.path.join(out_dir, "jobs"),
            dag_filename=bundle.dag_filename,
            dag_source=bundle.dag_source,
            spec_filename=bundle.spec_filename,
            spec=bundle.spec,
            job_files={},
            lib_files={},
        )
    except Exception as exc:  # noqa: BLE001
        raise DorisJobError(f"Doris DAG 投递失败：{exc}") from exc

    client = AirflowClient(
        airflow.endpoint, username=airflow.username, password=airflow.password
    )
    run_id = build_run_id(artifact_id)
    error: str | None = None
    triggered: dict[str, Any] = {}
    try:
        if not _wait_for_parse(client, bundle.dag_id, airflow.dag_parse_timeout):
            error = "Airflow 尚未解析到 Doris SQL DAG"
        else:
            client.unpause_dag(bundle.dag_id)
            triggered = client.trigger_dag(bundle.dag_id, dag_run_id=run_id)
    except AirflowError as exc:
        error = str(exc)
    finally:
        client.close()
    return {
        "execute_mode": "orchestrated",
        "compute_engine": "doris",
        "target_engine": "doris",
        "artifact_id": artifact_id,
        "dag_id": bundle.dag_id,
        "dag_run_id": run_id,
        "state": triggered.get("state") or ("failed" if error else "queued"),
        "run_url": client.run_url(bundle.dag_id, run_id),
        "source_tables": list(source_tables or []),
        "target_tables": list(target_tables or []),
        "artifacts": delivered.files_written,
        "error": error,
        "ok": error is None,
    }
