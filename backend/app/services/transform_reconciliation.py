"""Advance Doris transform projection state from Airflow final states."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import OntologyWarehouseDeployment, WarehouseObjectProjection


def reconcile_transform_receipt(
    db: Session,
    *,
    receipt: dict,
    airflow_state: str | None,
) -> WarehouseObjectProjection | None:
    if receipt.get("compute_engine") != "doris":
        return None
    deployment = (
        db.query(OntologyWarehouseDeployment)
        .filter(
            OntologyWarehouseDeployment.ontology_id == receipt.get("ontology_id"),
            OntologyWarehouseDeployment.ontology_version == receipt.get("ontology_version"),
            OntologyWarehouseDeployment.doris_datasource_id == receipt.get("datasource_id"),
        )
        .first()
    )
    if deployment is None or not receipt.get("object_type_id"):
        return None
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(
            WarehouseObjectProjection.deployment_id == deployment.id,
            WarehouseObjectProjection.object_type_id == receipt["object_type_id"],
        )
        .first()
    )
    if projection is None:
        return None
    state = (airflow_state or "").lower()
    if state == "success":
        projection.transform_status = "ready"
        projection.queryable = True
        deployment.status = "ready"
    elif state in {"failed", "upstream_failed"}:
        projection.transform_status = "failed"
        # Atomic staging/swap means the previous serving table remains intact.
        # Do not claim the failed candidate became queryable.
        projection.queryable = False
    elif state in {"running", "queued", "scheduled"}:
        projection.transform_status = "running"
        projection.queryable = False
    db.commit()
    return projection
