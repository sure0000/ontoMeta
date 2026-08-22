"""建模工单 API：需求到交付的版本化、可确认工作流。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.modeling import ModelingCase, ModelingCaseLink, ModelingCaseSpec
from app.schemas.modeling import (
    ModelingCaseCreate,
    ModelingCaseSpecConfirm,
    ModelingCaseSpecReject,
    ModelingCaseSpecSave,
    ModelingCaseUpdate,
)
from app.services.modeling_case import ModelingCaseService

router = APIRouter(prefix="/modeling-cases", tags=["modeling-cases"])


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _case_out(case: ModelingCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "conversation_id": case.conversation_id,
        "primary_domain_id": case.primary_domain_id,
        "domain_ids": _loads(case.domain_ids_json, []),
        "stage": case.stage,
        "current_revision": case.current_revision,
        "owner_subject_id": case.owner_subject_id,
        "blocked_reason": case.blocked_reason,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


def _spec_out(spec: ModelingCaseSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "case_id": spec.case_id,
        "kind": spec.kind,
        "revision": spec.revision,
        "status": spec.status,
        "payload": _loads(spec.payload_json, {}),
        "content_hash": spec.content_hash,
        "based_on": _loads(spec.based_on_json, []),
        "validation_report": _loads(spec.validation_report_json, None),
        "proposed_by": spec.proposed_by,
        "confirmed_by": spec.confirmed_by,
        "confirmed_at": spec.confirmed_at,
        "created_at": spec.created_at,
        "updated_at": spec.updated_at,
    }


def _link_out(link: ModelingCaseLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "case_id": link.case_id,
        "ref_kind": link.ref_kind,
        "ref_id": link.ref_id,
        "role": link.role,
        "spec_revision": link.spec_revision,
        "metadata": _loads(link.metadata_json, None),
        "created_at": link.created_at,
    }


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("")
def create_case(data: ModelingCaseCreate, db: Session = Depends(get_db)):
    try:
        return _case_out(ModelingCaseService.create(db, data))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("")
def list_cases(
    stage: str | None = None,
    owner_subject_id: str | None = None,
    conversation_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    cases = ModelingCaseService.list_cases(
        db,
        stage=stage,
        owner_subject_id=owner_subject_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )
    return [_case_out(case) for case in cases]


@router.get("/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)):
    case = ModelingCaseService.get(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return _case_out(case)


@router.patch("/{case_id}")
def update_case(
    case_id: str,
    data: ModelingCaseUpdate,
    db: Session = Depends(get_db),
):
    try:
        return _case_out(ModelingCaseService.update(db, case_id, data))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/{case_id}/specs")
def list_specs(
    case_id: str,
    kind: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    return [
        _spec_out(spec)
        for spec in ModelingCaseService.list_specs(
            db, case_id, kind=kind, status=status
        )
    ]


@router.post("/{case_id}/specs/{kind}")
def save_spec(
    case_id: str,
    kind: str,
    data: ModelingCaseSpecSave,
    db: Session = Depends(get_db),
):
    try:
        return _spec_out(ModelingCaseService.save_spec(db, case_id, kind, data))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{case_id}/specs/{kind}/{revision}/confirm")
def confirm_spec(
    case_id: str,
    kind: str,
    revision: int,
    data: ModelingCaseSpecConfirm,
    db: Session = Depends(get_db),
):
    try:
        return _spec_out(
            ModelingCaseService.confirm_spec(db, case_id, kind, revision, data)
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{case_id}/specs/{kind}/{revision}/reject")
def reject_spec(
    case_id: str,
    kind: str,
    revision: int,
    data: ModelingCaseSpecReject,
    db: Session = Depends(get_db),
):
    try:
        return _spec_out(
            ModelingCaseService.reject_spec(db, case_id, kind, revision, data)
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/{case_id}/specs/{kind}/stale")
def check_stale(case_id: str, kind: str, db: Session = Depends(get_db)):
    return ModelingCaseService.check_stale(db, case_id, kind)


@router.post("/{case_id}/specs/{kind}/rebase")
def rebase_spec(case_id: str, kind: str, db: Session = Depends(get_db)):
    try:
        return _spec_out(ModelingCaseService.rebase(db, case_id, kind))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/{case_id}/links")
def list_links(
    case_id: str,
    ref_kind: str | None = None,
    role: str | None = None,
    db: Session = Depends(get_db),
):
    return [
        _link_out(link)
        for link in ModelingCaseService.list_links(
            db, case_id, ref_kind=ref_kind, role=role
        )
    ]
