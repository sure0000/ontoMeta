"""治理规约 API（G3）：查看生效规约、发布版本、存量 re-lint。

全局 ``AdminAuthMiddleware`` 已对 /api 下路由做管理员鉴权，故此处不重复守卫。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.governance_standard import GovernanceStandardService

router = APIRouter()
_service = GovernanceStandardService()


class PublishStandardIn(BaseModel):
    version: str
    note: str | None = None


@router.get("/governance/standard")
def get_active_standard(db: Session = Depends(get_db)):
    """当前生效规约 + 可发布版本 + 给 agent 的约束卡（prompt_card）。"""
    standard = _service.get_active(db)
    return {
        "active_version": standard.version,
        "available_versions": _service.available_versions(),
        "standard": standard.to_dict(),
        "prompt_card": standard.compile_prompt_card(),
    }


@router.get("/governance/standard/history")
def standard_history(db: Session = Depends(get_db)):
    return [
        {
            "version": r.version,
            "status": r.status,
            "note": r.note,
            "activated_at": r.activated_at,
            "created_at": r.created_at,
        }
        for r in _service.history(db)
    ]


@router.post("/governance/standard/publish")
def publish_standard(body: PublishStandardIn, db: Session = Depends(get_db)):
    try:
        record = _service.publish(db, body.version, note=body.note)
    except ValueError as exc:  # 未登记版本 → 输入问题，4xx
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "version": record.version,
        "status": record.status,
        "activated_at": record.activated_at,
    }


@router.get("/ontologies/{ontology_id}/governance/relint")
def relint_ontology(ontology_id: str, db: Session = Depends(get_db)):
    """按当前生效规约体检某本体的物理产物（规约升级后找不合规历史表）。"""
    return _service.relint(db, ontology_id)
