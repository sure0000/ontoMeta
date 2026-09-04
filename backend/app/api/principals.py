"""主体与角色管理（M0 · RBAC）。

本路由整体要求 publisher 角色（见 ``auth._ROLE_OVERRIDES``）——
否则低权角色可自我提权。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import principal_service
from app.auth import _METHOD_DEFAULTS, _ROLE_OVERRIDES
from app.database import get_db
from app.models.principal import Role
from app.models.principal import Principal, role_satisfies
from app.schemas import (
    PrincipalCreate,
    PrincipalCreated,
    PrincipalOut,
    PrincipalUpdate,
    RolePolicyOut,
)

router = APIRouter()


@router.get("/principals", response_model=list[PrincipalOut])
def list_principals(db: Session = Depends(get_db)):
    return principal_service.list_principals(db)


@router.post("/principals", response_model=PrincipalCreated)
def create_principal(data: PrincipalCreate, db: Session = Depends(get_db)):
    try:
        principal, token = principal_service.create(db, name=data.name, role=data.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PrincipalCreated(
        **PrincipalOut.model_validate(principal).model_dump(), token=token
    )


@router.patch("/principals/{principal_id}", response_model=PrincipalOut)
def update_principal(
    principal_id: str, data: PrincipalUpdate, db: Session = Depends(get_db)
):
    try:
        principal = principal_service.update(
            db, principal_id, name=data.name, role=data.role, active=data.active
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if principal is None:
        raise HTTPException(status_code=404, detail="主体不存在")
    return principal


@router.post("/principals/{principal_id}/rotate-token", response_model=PrincipalCreated)
def rotate_principal_token(principal_id: str, db: Session = Depends(get_db)):
    result = principal_service.rotate_token(db, principal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="主体不存在")
    principal, token = result
    return PrincipalCreated(
        **PrincipalOut.model_validate(principal).model_dump(), token=token
    )


@router.delete("/principals/{principal_id}")
def delete_principal(principal_id: str, db: Session = Depends(get_db)):
    if not principal_service.delete(db, principal_id):
        raise HTTPException(status_code=404, detail="主体不存在")
    return {"deleted": principal_id}


@router.get("/principals-policy", response_model=RolePolicyOut)
def get_role_policy():
    """当前生效的权限矩阵，供前端展示与审计。"""
    return RolePolicyOut(
        roles=[r.value for r in Role],
        method_defaults=_METHOD_DEFAULTS,
        overrides=[
            {"method": m, "path_pattern": p, "minimum_role": r}
            for m, p, r in _ROLE_OVERRIDES
        ],
    )


@router.get("/principals/{principal_id}/mcp-access")
def principal_mcp_access(
    principal_id: str,
    audit_limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """MCP tool permissions, recent calls and client templates for one Principal."""
    from app.mcp import introspection

    principal = db.get(Principal, principal_id)
    if principal is None:
        raise HTTPException(status_code=404, detail="主体不存在")
    tools = introspection.tool_catalog()
    allowed = [tool for tool in tools if role_satisfies(principal.role, tool["required_role"])]
    recent, total_calls = introspection.query_audit(
        db, principal_id=principal.id, limit=audit_limit
    )
    placeholder = f"<{principal.token_prefix}…完整令牌>"
    return {
        "principal_id": principal.id,
        "role": principal.role,
        "allowed_count": len(allowed),
        "tool_count": len(tools),
        "tools": [
            {**tool, "allowed": role_satisfies(principal.role, tool["required_role"])}
            for tool in tools
        ],
        "recent_calls": recent,
        "total_calls": total_calls,
        "http_config": {
            "type": "http",
            "url": "/mcp/",
            "headers": {"Authorization": f"Bearer {placeholder}"},
        },
    }
