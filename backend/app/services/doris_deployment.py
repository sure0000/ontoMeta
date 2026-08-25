"""Doris ontology deployment and projection state transitions.

A pending deployment may be prepared before DDL execution. Materialization
submission never marks schema ready; only Airflow final-success reconciliation
calls :func:`publish_schema_ready`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.materialization_contract import MaterializationContractService
from app.services.ods_naming import ODS_DATABASE, target_ods_table_name
from app.services.source_ref import has_physical_source
from app.warehouse.policy import require_doris_datasource


class DorisDeploymentError(ValueError):
    """Deployment metadata cannot be created safely."""


def _table_name(layer: str, name: str) -> str:
    return name if name.startswith(f"{layer}_") else f"{layer}_{name}"


def _upsert_deployment(
    db: Session,
    *,
    ontology_id: str,
    datasource_id: str,
    artifact_id: str | None,
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
    schema_ready: bool,
) -> OntologyWarehouseDeployment:
    ontology = db.get(Ontology, ontology_id)
    if ontology is None or ontology.status != "published":
        raise DorisDeploymentError("只有当前已发布本体可以创建 Doris Deployment")
    ds = db.get(DataSource, datasource_id)
    try:
        require_doris_datasource(ds, operation="Doris Deployment")
    except ValueError as exc:
        raise DorisDeploymentError(str(exc)) from exc
    if not ds.is_default_warehouse:
        raise DorisDeploymentError("Doris Deployment 必须绑定默认 warehouse DataSource")

    deployment = (
        db.query(OntologyWarehouseDeployment)
        .filter(
            OntologyWarehouseDeployment.ontology_id == ontology.id,
            OntologyWarehouseDeployment.ontology_version == ontology.version,
        )
        .first()
    )
    if deployment is None:
        deployment = OntologyWarehouseDeployment(
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            doris_datasource_id=ds.id,
            status="pending",
        )
        db.add(deployment)
        db.flush()
    elif deployment.doris_datasource_id != ds.id:
        raise DorisDeploymentError("当前本体版本已绑定另一 Doris DataSource")

    if schema_ready:
        deployment.status = "schema_ready"
        deployment.materialization_artifact_id = artifact_id

    contracts = MaterializationContractService().list_contracts(
        db, ontology.id, materialized_only=True
    )
    by_object = {c.target_id: c for c in contracts if c.target_kind == "object_type"}
    overrides = database_overrides or {}
    tables = table_overrides or {}
    objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).all()
    for obj in objects:
        contract = by_object.get(obj.id)
        if contract is None:
            continue
        layer = contract.target_layer
        database = overrides.get(layer) or (
            f"{layer}_{database_prefix}" if database_prefix else layer
        )
        physical = tables.get(contract.id) or _table_name(layer, obj.name)
        projection = (
            db.query(WarehouseObjectProjection)
            .filter(
                WarehouseObjectProjection.deployment_id == deployment.id,
                WarehouseObjectProjection.object_type_id == obj.id,
            )
            .first()
        )
        if projection is None:
            projection = WarehouseObjectProjection(
                deployment_id=deployment.id,
                object_type_id=obj.id,
                schema_status="pending",
                sync_status="empty",
                transform_status="not_required",
                queryable=False,
            )
            db.add(projection)
        # ODS 落点恒定（见 ods_naming.ODS_DATABASE）：库名前缀只作用于服务层（dim/dwd/…），
        # 让 Projection 记一个带前缀的 ODS 库，会和同步实际写入的库对不上。
        projection.ods_database = ODS_DATABASE
        projection.ods_table = (
            target_ods_table_name(db, ontology.id, obj)
            if has_physical_source(obj.source_ref)
            else None
        )
        projection.serving_layer = layer
        projection.serving_database = database
        projection.serving_table = physical
        if schema_ready:
            projection.schema_status = "ready"
            # Schema publication never implies data readiness.
            projection.queryable = False
            if projection.sync_status not in {"ready", "stale"}:
                projection.sync_status = "empty"
            if projection.transform_status not in {"ready", "stale"}:
                projection.transform_status = "not_required"

    db.commit()
    db.refresh(deployment)
    return deployment


def prepare_current_deployment(
    db: Session,
    *,
    ontology_id: str,
    datasource_id: str,
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
) -> OntologyWarehouseDeployment:
    """Create pending current-version Deployment/Projections before Phase 6 DDL."""
    # Projection scope and materialize DDL must read the same current contract
    # set. Machine sync preserves pinned fields.
    MaterializationContractService().sync(db, ontology_id)
    return _upsert_deployment(
        db,
        ontology_id=ontology_id,
        datasource_id=datasource_id,
        artifact_id=None,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        schema_ready=False,
    )


def publish_schema_ready(
    db: Session,
    *,
    ontology_id: str,
    datasource_id: str,
    artifact_id: str | None = None,
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
) -> OntologyWarehouseDeployment:
    """Publish schema metadata only after reconciled Airflow final success."""
    return _upsert_deployment(
        db,
        ontology_id=ontology_id,
        datasource_id=datasource_id,
        artifact_id=artifact_id,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        schema_ready=True,
    )


def deployment_query_target(deployment: OntologyWarehouseDeployment) -> dict[str, Any]:
    """Safe, credential-free deployment metadata for query receipts."""
    return {
        "deployment_id": deployment.id,
        "ontology_id": deployment.ontology_id,
        "ontology_version": deployment.ontology_version,
        "datasource_id": deployment.doris_datasource_id,
        "status": deployment.status,
    }
