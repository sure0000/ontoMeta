"""Phase 6 production migration, evidence, approval and cut-over APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WarehouseMigrationBatch, WarehouseObjectProjection
from app.schemas import (
    DorisDeploymentPrepareInput,
    ShadowDifferenceInput,
    WarehouseMigrationApprovalInput,
    WarehouseMigrationBatchCreate,
    WarehouseMigrationOperatorInput,
    WarehouseMigrationRollbackInput,
    WarehouseMigrationStepInput,
    WarehouseRollbackDrillInput,
)
from app.services.warehouse_migration import (
    MigrationGateError,
    WarehouseMigrationService,
    runtime_compatibility_inventory,
    shadow_difference_report,
)

router = APIRouter(prefix="/warehouse/migrations", tags=["warehouse-migrations"])
service = WarehouseMigrationService()


def _actor(request: Request, db: Session) -> str:
    """Bind migration evidence/approval to the authenticated principal."""
    principal_id = getattr(request.state, "principal_id", None)
    if principal_id:
        from app.models import Principal

        principal = db.get(Principal, principal_id)
        if principal is None or not principal.active:
            raise HTTPException(status_code=403, detail="认证主体已失效")
        return principal.id
    # The bootstrap admin token is authenticated by middleware but has no row.
    return "bootstrap-admin"


def _guard(call):
    try:
        return call()
    except MigrationGateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("")
def create_batch(payload: WarehouseMigrationBatchCreate, request: Request, db: Session = Depends(get_db)):
    batch = _guard(lambda: service.create_batch(
        db,
        ontology_id=payload.ontology_id,
        approver=payload.approver,
        rollback_owner=payload.rollback_owner,
        observation_window_minutes=payload.observation_window_minutes,
        legacy_dag_ids=payload.legacy_dag_ids,
        new_dag_ids=payload.new_dag_ids,
        created_by=_actor(request, db),
    ))
    return service.serialize(db, batch)


@router.get("")
def list_batches(db: Session = Depends(get_db)):
    rows = db.query(WarehouseMigrationBatch).order_by(WarehouseMigrationBatch.created_at.desc()).all()
    return [service.serialize(db, row) for row in rows]


@router.post("/ontologies/{ontology_id}/prepare-deployment")
def prepare_deployment(
    ontology_id: str,
    payload: DorisDeploymentPrepareInput,
    db: Session = Depends(get_db),
):
    from app.services.doris_deployment import DorisDeploymentError, prepare_current_deployment

    try:
        deployment = prepare_current_deployment(
            db,
            ontology_id=ontology_id,
            datasource_id=payload.datasource_id,
            database_prefix=payload.database_prefix,
            database_overrides=payload.database_overrides,
            table_overrides=payload.table_overrides,
        )
    except DorisDeploymentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    count = db.query(WarehouseObjectProjection).filter_by(
        deployment_id=deployment.id
    ).count()
    return {
        "deployment_id": deployment.id,
        "ontology_id": deployment.ontology_id,
        "ontology_version": deployment.ontology_version,
        "datasource_id": deployment.doris_datasource_id,
        "status": deployment.status,
        "projection_count": count,
    }


@router.get("/compatibility-inventory")
def compatibility_inventory():
    return runtime_compatibility_inventory()


@router.post("/shadow-compare")
def compare_shadow(payload: ShadowDifferenceInput):
    """Return hashes/counts only. Shadow result rows are never returned or persisted."""
    return shadow_difference_report(payload.cases)


@router.get("/{batch_id}")
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(WarehouseMigrationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="迁移批次不存在")
    return service.serialize(db, batch)


@router.get("/{batch_id}/report")
def get_report(batch_id: str, db: Session = Depends(get_db)):
    return _guard(lambda: service.final_report(db, batch_id))


@router.post("/{batch_id}/steps")
def record_step(
    batch_id: str,
    payload: WarehouseMigrationStepInput,
    request: Request,
    db: Session = Depends(get_db),
):
    evidence = _guard(lambda: service.record_step(
        db,
        batch_id=batch_id,
        step=payload.step,
        passed=payload.passed,
        report=payload.report,
        artifact_ids=payload.artifact_ids,
        operator=_actor(request, db),
    ))
    return {
        "evidence_id": evidence.id,
        "step": evidence.step,
        "attempt": evidence.attempt,
        "status": evidence.status,
        "checksum": evidence.checksum,
    }


@router.post("/{batch_id}/rollback-drill")
def rollback_drill(
    batch_id: str,
    payload: WarehouseRollbackDrillInput,
    request: Request,
    db: Session = Depends(get_db),
):
    batch = _guard(lambda: service.record_rollback_drill(
        db, batch_id=batch_id, report=payload.report, operator=_actor(request, db)
    ))
    return service.serialize(db, batch)


@router.post("/{batch_id}/approve-cutover")
def approve_cutover(
    batch_id: str,
    payload: WarehouseMigrationApprovalInput,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = _actor(request, db)
    if payload.approver != actor:
        raise HTTPException(status_code=403, detail="审批人必须是当前认证主体")
    evidence = _guard(lambda: service.approve_cutover(
        db, batch_id=batch_id, approver=actor, note=payload.note
    ))
    return {"evidence_id": evidence.id, "status": evidence.status, "step": evidence.step}


@router.post("/{batch_id}/stop-legacy-dags")
def stop_legacy_dags(
    batch_id: str,
    payload: WarehouseMigrationOperatorInput,
    request: Request,
    db: Session = Depends(get_db),
):
    evidence = _guard(lambda: service.stop_legacy_dags(
        db, batch_id=batch_id, operator=_actor(request, db)
    ))
    return {"evidence_id": evidence.id, "status": evidence.status, "step": evidence.step}


@router.post("/{batch_id}/rollback")
def rollback(
    batch_id: str,
    payload: WarehouseMigrationRollbackInput,
    request: Request,
    db: Session = Depends(get_db),
):
    batch = _guard(lambda: service.rollback(
        db, batch_id=batch_id, operator=_actor(request, db), reason=payload.reason
    ))
    return service.serialize(db, batch)
