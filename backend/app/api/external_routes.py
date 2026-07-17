"""外部 API 管理接口 + MCP 协议端点 + 对外只读 v1 接口。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExternalApp
from app.schemas import (
    ALL_EXTERNAL_SCOPES,
    BusinessLogicDetail,
    BusinessLogicOut,
    DomainContextSummary,
    ExternalApiCallLogOut,
    ExternalApiCatalogItem,
    ExternalAppCreate,
    ExternalAppCreated,
    ExternalAppOut,
    ExternalAppUpdate,
    McpToolCallRequest,
    McpToolCallResult,
    ObjectTypeDetail,
    ObjectTypeSummary,
    RelationTypeDetail,
    RelationTypeOut,
)
from app.services.external_api import (
    MCP_ENDPOINT,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_INFO,
    ExternalApiService,
    require_scope,
)
from app.services.query import OntologyQueryService, WorkspaceService

router = APIRouter()
external_api = ExternalApiService()
query = OntologyQueryService()
workspace = WorkspaceService()


def _require_external_app(
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> ExternalApp:
    return external_api.authenticate(db, x_api_key or authorization)


def _log_v1_call(
    db: Session,
    app: ExternalApp,
    *,
    path: str,
    tool_name: str,
    status_code: int,
    started: float,
    error_message: str | None = None,
) -> None:
    try:
        external_api.record_call(
            db,
            app_id=app.id,
            tool_name=tool_name,
            path=path,
            status_code=status_code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_message=error_message,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 管理端：应用创建 / MCP Tool 目录（供 ontoMeta 控制台使用，需 Admin Token）
# ---------------------------------------------------------------------------


@router.get("/external-apps", response_model=list[ExternalAppOut])
def list_external_apps(db: Session = Depends(get_db)):
    return external_api.list_apps(db)


@router.post("/external-apps", response_model=ExternalAppCreated)
def create_external_app(data: ExternalAppCreate, db: Session = Depends(get_db)):
    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="应用名称不能为空")
    try:
        return external_api.create_app(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/external-apps/scopes")
def list_external_scopes():
    """可用 scope 枚举（控制台多选）。"""
    return {"scopes": list(ALL_EXTERNAL_SCOPES)}


@router.get("/external-apps/{app_id}", response_model=ExternalAppOut)
def get_external_app(
    app_id: str,
    db: Session = Depends(get_db),
):
    app = external_api.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="应用不存在")
    return app


@router.patch("/external-apps/{app_id}", response_model=ExternalAppOut)
def update_external_app(
    app_id: str,
    data: ExternalAppUpdate,
    db: Session = Depends(get_db),
):
    try:
        app = external_api.update_app(db, app_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not app:
        raise HTTPException(status_code=404, detail="应用不存在")
    return app


@router.post("/external-apps/{app_id}/regenerate-key", response_model=ExternalAppCreated)
def regenerate_external_app_key(app_id: str, db: Session = Depends(get_db)):
    app = external_api.regenerate_key(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="应用不存在")
    return app


@router.delete("/external-apps/{app_id}")
def delete_external_app(app_id: str, db: Session = Depends(get_db)):
    if not external_api.delete_app(db, app_id):
        raise HTTPException(status_code=404, detail="应用不存在")
    return {"id": app_id, "deleted": True}


@router.get("/external-apps/{app_id}/call-logs", response_model=list[ExternalApiCallLogOut])
def list_external_app_call_logs(
    app_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if not external_api.get_app(db, app_id):
        raise HTTPException(status_code=404, detail="应用不存在")
    return external_api.list_call_logs(db, app_id=app_id, limit=limit)


@router.get("/external-api/catalog", response_model=list[ExternalApiCatalogItem])
def list_external_api_catalog():
    return external_api.list_catalog()


@router.get("/external-api/catalog/{api_id}", response_model=ExternalApiCatalogItem)
def get_external_api_catalog_item(api_id: str):
    item = external_api.get_catalog_item(api_id)
    if not item:
        raise HTTPException(status_code=404, detail="MCP 工具不存在")
    return item


@router.get("/external-api/call-logs", response_model=list[ExternalApiCallLogOut])
def list_external_api_call_logs(
    app_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return external_api.list_call_logs(db, app_id=app_id, limit=limit)


# ---------------------------------------------------------------------------
# MCP 协议端点：供 Agent / MCP Client 发现并调用工具
# ---------------------------------------------------------------------------


@router.get("/mcp")
def mcp_server_info():
    """MCP 服务发现信息（无需鉴权）。"""
    return {
        "endpoint": MCP_ENDPOINT,
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "serverInfo": MCP_SERVER_INFO,
        "transport": "http-jsonrpc",
        "auth": "X-API-Key 或 Authorization: Bearer <api_key>",
        "scopes": list(ALL_EXTERNAL_SCOPES),
        "methods": ["initialize", "tools/list", "tools/call", "ping"],
    }


@router.post("/mcp")
async def mcp_jsonrpc(
    request: Request,
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    """MCP JSON-RPC over HTTP。Agent 通过此端点 initialize / tools/list / tools/call。"""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP 请求体须为 JSON 对象")

    result = external_api.handle_mcp_rpc(
        db,
        payload,
        query_service=query,
        workspace_service=workspace,
        app=app,
    )
    if result is None:
        return Response(status_code=204)
    return JSONResponse(content=result)


@router.post("/mcp/tools/call", response_model=McpToolCallResult)
def mcp_tools_call(
    data: McpToolCallRequest,
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    """简化版 tools/call，供控制台在线试用；走真实 API Key 鉴权 + Scope。"""
    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="工具名称不能为空")
    tool_name = data.name.strip()
    started = time.perf_counter()
    status_code = 200
    error_message = None
    try:
        result = external_api.call_tool(
            db,
            tool_name,
            data.arguments,
            query_service=query,
            workspace_service=workspace,
            app=app,
        )
        if result.isError:
            status_code = 400
            if result.content and isinstance(result.content[0].get("text"), str):
                error_message = result.content[0]["text"]
        return result
    except HTTPException as exc:
        status_code = exc.status_code
        error_message = str(exc.detail)
        raise
    finally:
        _log_v1_call(
            db,
            app,
            path="/api/mcp/tools/call",
            tool_name=tool_name,
            status_code=status_code,
            started=started,
            error_message=error_message,
        )


@router.get("/mcp/tools")
def mcp_tools_list(
    db: Session = Depends(get_db),
    _: ExternalApp = Depends(_require_external_app),
):
    """简化版 tools/list。"""
    return {"tools": external_api.list_mcp_tools()}


# ---------------------------------------------------------------------------
# 对外只读 API（/api/v1/*）：仅已发布的业务对象 / 关系 / 逻辑（兼容 REST）
# ---------------------------------------------------------------------------

v1_router = APIRouter(prefix="/v1", tags=["External API v1"])


@v1_router.get("/domains", response_model=list[DomainContextSummary])
def v1_list_domains(
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    try:
        require_scope(app, "domains:read")
        data = workspace.list_domains(db)
        _log_v1_call(
            db, app, path="/api/v1/domains", tool_name="list_domains", status_code=200, started=started
        )
        return data
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path="/api/v1/domains",
            tool_name="list_domains",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/object-types", response_model=list[ObjectTypeSummary])
def v1_list_object_types(
    domain_id: str | None = Query(None),
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    try:
        require_scope(app, "objects:read")
        data = query.list_object_types(
            db,
            domain_context_id=domain_id,
            published_only=True,
        ).items
        _log_v1_call(
            db,
            app,
            path="/api/v1/object-types",
            tool_name="list_object_types",
            status_code=200,
            started=started,
        )
        return data
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path="/api/v1/object-types",
            tool_name="list_object_types",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/object-types/{object_type_id}", response_model=ObjectTypeDetail)
def v1_get_object_type(
    object_type_id: str,
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    path = f"/api/v1/object-types/{object_type_id}"
    try:
        require_scope(app, "objects:read")
        detail = query.get_object_type(db, object_type_id)
        if not detail or detail.status != "published":
            raise HTTPException(status_code=404, detail="业务对象不存在或未发布")
        _log_v1_call(
            db, app, path=path, tool_name="get_object_type", status_code=200, started=started
        )
        return detail
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path=path,
            tool_name="get_object_type",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/relation-types", response_model=list[RelationTypeOut])
def v1_list_relation_types(
    domain_id: str | None = Query(None),
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    try:
        require_scope(app, "relations:read")
        data = query.list_relation_types(
            db,
            domain_context_id=domain_id,
            published_only=True,
        ).items
        _log_v1_call(
            db,
            app,
            path="/api/v1/relation-types",
            tool_name="list_relation_types",
            status_code=200,
            started=started,
        )
        return data
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path="/api/v1/relation-types",
            tool_name="list_relation_types",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/relation-types/{relation_type_id}", response_model=RelationTypeDetail)
def v1_get_relation_type(
    relation_type_id: str,
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    path = f"/api/v1/relation-types/{relation_type_id}"
    try:
        require_scope(app, "relations:read")
        detail = query.get_relation_type(db, relation_type_id)
        if not detail or detail.status != "published":
            raise HTTPException(status_code=404, detail="业务关系不存在或未发布")
        _log_v1_call(
            db, app, path=path, tool_name="get_relation_type", status_code=200, started=started
        )
        return detail
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path=path,
            tool_name="get_relation_type",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/business-logics", response_model=list[BusinessLogicOut])
def v1_list_business_logics(
    domain_id: str | None = Query(None),
    category_id: str | None = Query(None),
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    try:
        require_scope(app, "logics:read")
        data = query.list_business_logics(
            db,
            domain_context_id=domain_id,
            category_id=category_id,
            published_only=True,
        ).items
        _log_v1_call(
            db,
            app,
            path="/api/v1/business-logics",
            tool_name="list_business_logics",
            status_code=200,
            started=started,
        )
        return data
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path="/api/v1/business-logics",
            tool_name="list_business_logics",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise


@v1_router.get("/business-logics/{logic_id}", response_model=BusinessLogicDetail)
def v1_get_business_logic(
    logic_id: str,
    db: Session = Depends(get_db),
    app: ExternalApp = Depends(_require_external_app),
):
    started = time.perf_counter()
    path = f"/api/v1/business-logics/{logic_id}"
    try:
        require_scope(app, "logics:read")
        detail = query.get_business_logic(db, logic_id)
        if not detail or detail.status != "published":
            raise HTTPException(status_code=404, detail="业务逻辑不存在或未发布")
        _log_v1_call(
            db, app, path=path, tool_name="get_business_logic", status_code=200, started=started
        )
        return detail
    except HTTPException as exc:
        _log_v1_call(
            db,
            app,
            path=path,
            tool_name="get_business_logic",
            status_code=exc.status_code,
            started=started,
            error_message=str(exc.detail),
        )
        raise
