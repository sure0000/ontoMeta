"""MCP 服务的管理 REST（供前端设置页的「MCP 服务」Tab 用）。

前端不能直接说 MCP 协议，故把「服务状态 / 审计 / 统计」用普通 REST 暴露。这些走
主后端的 AdminAuthMiddleware：
- ``/api/mcp/info``：服务形态 + 工具清单（不敏感）→ reader 起（GET 默认）。
- ``/api/mcp/audit`` / ``/api/mcp/stats``：含每次调用的主体与入参 → 显式要求 publisher。

数据逻辑一律走 ``app.mcp.introspection``（与 MCP 工具 server_info/get_mcp_stats/
list_audit_logs 共用同一份），REST 只是薄壳。
"""

from __future__ import annotations

from typing import Literal
from fastapi.responses import Response

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_role
from app.database import get_db
from app.mcp import introspection
from app.mcp.audit import record_call
from app.mcp.skills import (
    get_skill,
    list_skills,
    reset_override,
    save_override,
    set_enabled,
    skill_coverage_gaps,
    skill_view_dict,
    export_zip,
    list_versions,
    restore_version,
)
from app.mcp.tools import AuthContext

router = APIRouter()


class SkillOverridePayload(BaseModel):
    body: str = Field(min_length=1)


class SkillEnabledPayload(BaseModel):
    enabled: bool


class McpSettingsPayload(BaseModel):
    mcp_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=100000)
    mcp_execute_sql_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=100000)


def _audit_management(
    request: Request, action: str, arguments: dict, *, success: bool, error: str | None = None
) -> None:
    record_call(
        auth=AuthContext(
            client_type="api",
            role=getattr(request.state, "principal_role", None),
            principal_id=getattr(request.state, "principal_id", None),
        ),
        tool_name=action,
        arguments=arguments,
        success=success,
        error=error,
    )


@router.get("/mcp/info")
def mcp_info(db: Session = Depends(get_db)):
    """MCP 服务形态：传输、鉴权策略、限流、工具清单、审计可达性。"""
    status = introspection.service_status(db)
    status["audit"] = introspection.audit_health(db)
    return status


@router.get("/mcp/stats", dependencies=[Depends(require_role("publisher"))])
def mcp_stats(
    window_minutes: int | None = Query(None, ge=1, le=43200),
    top_tools: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """审计聚合统计。仅 publisher。"""
    return introspection.compute_stats(
        db, window_minutes=window_minutes, top_tools=top_tools
    )


@router.get("/mcp/audit", dependencies=[Depends(require_role("publisher"))])
def mcp_audit(
    tool_name: str | None = Query(None),
    success: bool | None = Query(None),
    denied_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    principal_id: str | None = Query(None),
    principal_role: Literal["reader", "editor", "reviewer", "publisher", "anonymous"] | None = Query(None),
    result: Literal["success", "failed", "denied", "rate_limited"] | None = Query(None),
    window_minutes: int | None = Query(None, ge=1, le=43200),
    db: Session = Depends(get_db),
):
    """审计日志分页。入参在写入时已脱敏。仅 publisher。"""
    logs, total = introspection.query_audit(
        db,
        tool_name=tool_name,
        success=success,
        denied_only=denied_only,
        limit=limit,
        offset=offset,
        principal_id=principal_id,
        principal_role=principal_role,
        result=result,
        window_minutes=window_minutes,
    )
    return {"logs": logs, "total": total, "limit": limit, "offset": offset}


@router.get("/mcp/settings")
def mcp_settings(db: Session = Depends(get_db)):
    from app.api.deps import settings_service

    values = settings_service.get_mcp_settings(db)
    values = {
        key: values[key]
        for key in ("mcp_rate_limit_per_minute", "mcp_execute_sql_rate_limit_per_minute")
        if key in values
    }
    values.pop("updated_at", None)
    return values


@router.put("/mcp/settings", dependencies=[Depends(require_role("publisher"))])
def update_mcp_settings(
    payload: McpSettingsPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.api.deps import settings_service

    data = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
    try:
        values = settings_service.update_mcp_settings(db, data)
    except ValueError as exc:
        _audit_management(request, "settings:mcp", data, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_management(request, "settings:mcp", data, success=True)
    values.pop("updated_at", None)
    return values


@router.get("/mcp/skills")
def mcp_skills(db: Session = Depends(get_db)):
    """Skill 清单与工具覆盖度（reader）。"""
    return {
        "skills": [skill_view_dict(item, include_body=False) for item in list_skills(db)],
        "coverage_gaps": skill_coverage_gaps(db),
    }


@router.get("/mcp/skills/export")
def export_mcp_skills(db: Session = Depends(get_db)):
    try:
        content, filename = export_zip(db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/mcp/skills/{name}/versions")
def mcp_skill_versions(name: str, db: Session = Depends(get_db)):
    try:
        return {"versions": list_versions(db, name)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/mcp/skills/{name}")
def mcp_skill_detail(name: str, db: Session = Depends(get_db)):
    skill = get_skill(db, name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return skill_view_dict(skill)


@router.get("/mcp/skills/{name}/export")
def export_mcp_skill(name: str, db: Session = Depends(get_db)):
    try:
        content, filename = export_zip(db, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/mcp/skills/{name}/versions/{version}/restore",
    dependencies=[Depends(require_role("publisher"))],
)
def restore_mcp_skill_version(
    name: str,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        skill = restore_version(
            db,
            name,
            version,
            updated_by=getattr(request.state, "principal_id", None) or "admin",
        )
    except ValueError as exc:
        _audit_management(
            request,
            "skill:restore-version",
            {"name": name, "version": version},
            success=False,
            error=str(exc),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_management(
        request,
        "skill:restore-version",
        {"name": name, "version": version},
        success=True,
    )
    return skill_view_dict(skill)


@router.put("/mcp/skills/{name}", dependencies=[Depends(require_role("publisher"))])
def update_mcp_skill(
    name: str,
    payload: SkillOverridePayload,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        skill = save_override(
            db,
            name,
            payload.body,
            updated_by=getattr(request.state, "principal_id", None) or "admin",
        )
    except ValueError as exc:
        _audit_management(
            request, "skill:update", {"name": name}, success=False, error=str(exc)
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_management(request, "skill:update", {"name": name}, success=True)
    return skill_view_dict(skill)


@router.delete(
    "/mcp/skills/{name}/override", dependencies=[Depends(require_role("publisher"))]
)
def delete_mcp_skill_override(
    name: str, request: Request, db: Session = Depends(get_db)
):
    try:
        skill = reset_override(
            db, name, updated_by=getattr(request.state, "principal_id", None) or "admin"
        )
    except ValueError as exc:
        _audit_management(
            request, "skill:reset", {"name": name}, success=False, error=str(exc)
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_management(request, "skill:reset", {"name": name}, success=True)
    return skill_view_dict(skill)


@router.patch("/mcp/skills/{name}", dependencies=[Depends(require_role("publisher"))])
def patch_mcp_skill(
    name: str,
    payload: SkillEnabledPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        skill = set_enabled(
            db,
            name,
            payload.enabled,
            updated_by=getattr(request.state, "principal_id", None) or "admin",
        )
    except ValueError as exc:
        _audit_management(
            request, "skill:toggle", {"name": name, "enabled": payload.enabled}, success=False, error=str(exc)
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_management(
        request, "skill:toggle", {"name": name, "enabled": payload.enabled}, success=True
    )
    return skill_view_dict(skill)
