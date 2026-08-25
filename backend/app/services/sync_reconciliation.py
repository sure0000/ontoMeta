"""Verify a completed sync against its Doris ODS target."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import DataSource, IngestionContract
from app.services.data_app_executor import ExecutionError, execute_sql
from app.warehouse import get_adapter


def reconcile_sync_receipt(
    db: Session,
    *,
    receipt: dict[str, Any],
    airflow_state: str | None,
) -> dict[str, Any] | None:
    """Update the ingestion contract and return Doris verification evidence.

    Airflow success only proves that orchestration finished. A sync is successful
    after its declared Doris table can be queried; an empty source legitimately
    produces a queryable target with zero rows.
    """
    contract_id = str(receipt.get("ingestion_contract_id") or "").strip()
    if not contract_id:
        return {
            "status": "failed",
            "verified": False,
            "error": "同步回执缺少接入契约，无法验证 Doris 落数结果",
        }
    contract = db.get(IngestionContract, contract_id)
    if contract is None:
        return {
            "status": "failed",
            "verified": False,
            "error": "同步回执引用的接入契约不存在，无法验证 Doris 落数结果",
        }

    state = (airflow_state or "").lower()
    target = f"{contract.target_ods_database}.{contract.target_ods_table}"
    if state in {"running", "queued", "scheduled"}:
        contract.status = "running"
        db.commit()
        return {
            "status": "running",
            "verified": False,
            "target_table": target,
        }
    if state in {"failed", "upstream_failed"}:
        contract.status = "failed"
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": "Airflow/Flink 同步任务执行失败",
        }
    if state != "success":
        return None

    datasource = db.get(DataSource, contract.doris_datasource_id)
    if datasource is None or not (datasource.dsn_secret_ref or "").strip():
        contract.status = "failed"
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": "目标 Doris 数据源不存在或未配置连接，无法验证落数结果",
        }

    quoted = get_adapter("doris").quote_table_ref(target)
    try:
        _columns, rows = execute_sql(
            dsn=datasource.dsn_secret_ref,
            sql=f"SELECT COUNT(*) AS row_count FROM {quoted}",
            limit=1,
            timeout_seconds=30,
            dialect="doris",
        )
        row_count = int((rows[0] if rows else {}).get("row_count") or 0)
    except (ExecutionError, TypeError, ValueError) as exc:
        contract.status = "failed"
        db.commit()
        return {
            "status": "failed",
            "verified": False,
            "target_table": target,
            "error": f"Doris 目标表验证失败：{exc}",
        }

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    contract.status = "ready"
    contract.last_success_at = now
    db.commit()
    return {
        "status": "verified",
        "verified": True,
        "target_table": target,
        "row_count": row_count,
        "empty": row_count == 0,
        "verified_at": now.isoformat(),
    }


__all__ = ["reconcile_sync_receipt"]
