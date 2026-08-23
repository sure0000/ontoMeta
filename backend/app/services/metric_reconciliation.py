"""Advance Doris ADS logic projection from Airflow final states."""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import WarehouseLogicProjection


def reconcile_metric_receipt(db: Session, *, receipt: dict, airflow_state: str | None):
    if receipt.get("compute_engine") != "doris" or not receipt.get("logic_projection_id"):
        return None
    projection = db.get(WarehouseLogicProjection, receipt["logic_projection_id"])
    if projection is None:
        return None
    state = (airflow_state or "").lower()
    if state == "success":
        projection.status = "ready"
        projection.queryable = True
        projection.last_success_at = datetime.now(timezone.utc).replace(tzinfo=None)
    elif state in {"failed", "upstream_failed"}:
        projection.status = "failed"
        projection.queryable = False
    elif state in {"running", "queued", "scheduled"}:
        projection.status = "running"
        projection.queryable = False
    db.commit()
    return projection
