"""Flink REST health checks for detached CDC ingestion jobs."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import IngestionContract
from app.services.settings_service import SettingsService

RUNNING_STATES = frozenset({"RUNNING", "RECONCILING", "INITIALIZING", "CREATED"})
FAILED_STATES = frozenset({"FAILED", "CANCELED", "CANCELLING", "SUSPENDED"})


class FlinkHealthError(ValueError):
    pass


def check_ingestion_job(
    db: Session,
    contract_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    contract = db.get(IngestionContract, contract_id)
    if contract is None:
        raise FlinkHealthError("IngestionContract 不存在")
    if contract.mode != "cdc":
        raise FlinkHealthError("只有 CDC 契约需要长期 Flink 健康检查")
    if not contract.flink_job_id:
        raise FlinkHealthError("CDC 契约尚无真实 flink_job_id")
    endpoint = SettingsService().get_airflow_runtime(db).flink_rest_endpoint.rstrip("/")
    if not endpoint:
        raise FlinkHealthError("未在设置页配置 flink_rest_endpoint")

    own_client = client is None
    http = client or httpx.Client(trust_env=False, timeout=10.0)
    try:
        response = http.get(f"{endpoint}/jobs/{contract.flink_job_id}")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        contract.status = "stale"
        db.commit()
        raise FlinkHealthError(f"Flink REST 健康检查失败：{exc}") from exc
    finally:
        if own_client:
            http.close()

    state = str(payload.get("state") or payload.get("status") or "UNKNOWN").upper()
    if state in RUNNING_STATES:
        contract.status = "running"
    elif state in FAILED_STATES:
        contract.status = "failed"
    else:
        contract.status = "stale"
    # REST payload may expose checkpoint counts/timestamps; never invent paths.
    db.commit()
    return {
        "contract_id": contract.id,
        "flink_job_id": contract.flink_job_id,
        "state": state,
        "healthy": state in RUNNING_STATES,
        "status": contract.status,
        "start_time": payload.get("start-time"),
        "duration": payload.get("duration"),
    }
