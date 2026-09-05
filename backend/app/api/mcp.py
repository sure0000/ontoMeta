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
    install_to_dir,
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


class SkillInstallPayload(BaseModel):
    """把生效 Skill 直接写进 Agent 读取的目录（后端主机上的绝对路径）。"""

    target_dir: str = Field(min_length=1)
    #: 留空 = 全部已启用；给了就只装这几份（仍是生效正文）。
    names: list[str] | None = None
    #: 只回计划、不写盘。界面先拿它给人看"会新建/覆盖哪几份"。
    dry_run: bool = False
    #: 记住这个目录作为下次的默认值（真正写盘时才记）。
    remember: bool = True


class McpSettingsPayload(BaseModel):
    mcp_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=100000)
    mcp_execute_sql_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=100000)
    # 代执行授权闸的开关（与角色正交，只作用于 MCP 的 confirm/execute）。
    mcp_require_execution_approval: bool | None = None
    # 本机 stdio 宿主可用 ask_user_question 等 UI 取得人类确认，并回传任务 digest。
    mcp_allow_stdio_interactive_approval: bool | None = None
    # Skill 安装目录（后端主机上的绝对路径），只作为技能页的默认值。
    mcp_skill_install_dir: str | None = None
    # 控制台对外地址，用于把交互表单拼成可点的链接。
    mcp_console_base_url: str | None = None


# 读写两侧共用一份字段清单：少写一个，界面上那一格就静默变成"关"，
# 而运行期其实还开着——开关会撒谎。
_MCP_SETTING_FIELDS = (
    "mcp_rate_limit_per_minute",
    "mcp_execute_sql_rate_limit_per_minute",
    "mcp_require_execution_approval",
    "mcp_allow_stdio_interactive_approval",
    "mcp_skill_install_dir",
    "mcp_console_base_url",
)


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
    values = {key: values[key] for key in _MCP_SETTING_FIELDS if key in values}
    values.pop("updated_at", None)
    return values


@router.put("/mcp/settings", dependencies=[Depends(require_role("publisher"))])
def update_mcp_settings(
    payload: McpSettingsPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.api.deps import settings_service

    # 用 `is not None` 而不是真值判断：False 是合法取值（把闸关掉），
    # truthy 过滤会让"关"这个动作静默失效。
    data = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    try:
        values = settings_service.update_mcp_settings(db, data)
    except ValueError as exc:
        _audit_management(request, "settings:mcp", data, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_management(request, "settings:mcp", data, success=True)
    values.pop("updated_at", None)
    return values


class FlowFormSubmitPayload(BaseModel):
    """页面填完后提交的取值（键是字段 key）。

    ``confirm`` 只在执行审查那一步为真（人点了「确认执行方案」）；``plan_digest`` 是页面上
    显示的那份方案的指纹，对不上说明提交前方案变了，服务端会退回让人重看。
    """

    values: dict = {}
    confirm: bool = False
    plan_digest: str = ""


@router.get("/mcp/flow-forms/{form_id}")
def mcp_flow_form(form_id: str, db: Session = Depends(get_db)):
    """交互式建数流程的一次性表单：字段与候选每次实时算，不读快照。"""
    from app.mcp.flow_forms import form_state, get_form

    form = get_form(db, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="表单不存在或已被清理")
    return form_state(db, form)


@router.post(
    "/mcp/flow-forms/{form_id}/submit", dependencies=[Depends(require_role("editor"))]
)
def submit_mcp_flow_form(
    form_id: str,
    payload: FlowFormSubmitPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    """提交表单。参数没填齐、或执行审查还没被确认，就原地退回继续；填齐并确认才落 submitted。"""
    from app.mcp.flow_forms import get_form, submit_form

    form = get_form(db, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="表单不存在或已被清理")
    result = submit_form(
        db,
        form,
        payload.values or {},
        confirm=payload.confirm,
        plan_digest=payload.plan_digest,
    )
    _audit_management(
        request,
        "flow-form:submit",
        {
            "form_id": form_id,
            "stage": form.stage,
            "confirm": payload.confirm,
            "accepted": result.get("accepted"),
        },
        success=True,
    )
    return result


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


@router.post("/mcp/skills/install", dependencies=[Depends(require_role("publisher"))])
def install_mcp_skills(
    payload: SkillInstallPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    """把生效 Skill 写进目标目录，省掉"下载 ZIP → 找到目录 → 解压"这三步。

    路径是**后端主机**上的绝对路径（安装是服务端写盘）。``dry_run`` 先给计划，
    界面确认后再真写；两次都记审计——这是一个往主机上写文件的动作。
    """
    from app.api.deps import settings_service

    action = "skill:install-preview" if payload.dry_run else "skill:install"
    arguments = {
        "target_dir": payload.target_dir,
        "names": payload.names,
        "dry_run": payload.dry_run,
    }
    try:
        result = install_to_dir(
            db,
            target_dir=payload.target_dir,
            names=payload.names,
            dry_run=payload.dry_run,
        )
    except ValueError as exc:
        _audit_management(request, action, arguments, success=False, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.dry_run and payload.remember:
        # 记的是**规范化后**的目录：下次默认值与这次真正写进去的地方是同一个。
        settings_service.update_mcp_settings(
            db, {"mcp_skill_install_dir": result["target_dir"]}
        )
    _audit_management(request, action, arguments, success=True)
    return result


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
