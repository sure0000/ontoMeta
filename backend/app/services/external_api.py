"""外部 API：应用密钥管理、MCP/REST 目录（单一数据源）、鉴权、Scope、限流与调用日志。"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import api_key_prefix, hash_api_key
from app.config import settings
from app.models import ExternalApiCallLog, ExternalApp
from app.schemas import (
    ALL_EXTERNAL_SCOPES,
    ExternalApiCallLogOut,
    ExternalApiCatalogItem,
    ExternalAppCreate,
    ExternalAppCreated,
    ExternalAppOut,
    ExternalAppUpdate,
    McpToolCallResult,
)
from app.services.external_rate_limit import external_rate_limiter


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _generate_app_key() -> str:
    return f"app_{secrets.token_hex(8)}"


def _generate_api_key() -> str:
    return f"om_sk_{secrets.token_urlsafe(32)}"


def _hint_from_prefix(prefix: str | None) -> str | None:
    if not prefix:
        return None
    return f"{prefix}…"


def _legacy_api_key_placeholder(key_hash: str) -> str:
    """旧表 api_key 列为 NOT NULL；写入非明文占位以满足约束，鉴权只看 api_key_hash。"""
    return f"hashed:{key_hash}"


def _serialize_scopes(scopes: list[str] | None) -> str:
    cleaned = _normalize_scopes(scopes)
    return json.dumps(cleaned, ensure_ascii=False)


def _normalize_scopes(scopes: list[str] | None) -> list[str]:
    if scopes is None:
        return list(ALL_EXTERNAL_SCOPES)
    allowed = set(ALL_EXTERNAL_SCOPES)
    cleaned = [s.strip() for s in scopes if s and s.strip()]
    unknown = sorted(set(cleaned) - allowed)
    if unknown:
        raise ValueError(f"不支持的 scope：{', '.join(unknown)}")
    # 去重且保持 ALL_EXTERNAL_SCOPES 顺序
    ordered = [s for s in ALL_EXTERNAL_SCOPES if s in cleaned]
    if not ordered:
        raise ValueError("至少选择一个 scope")
    return ordered


def parse_app_scopes(raw: str | None) -> list[str]:
    """空/NULL 视为全部默认 scope（兼容 B8 前已有应用）。"""
    if not raw or not raw.strip():
        return list(ALL_EXTERNAL_SCOPES)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return list(ALL_EXTERNAL_SCOPES)
    if not isinstance(data, list) or not data:
        return list(ALL_EXTERNAL_SCOPES)
    return _normalize_scopes([str(x) for x in data])


def app_has_scope(app: ExternalApp, scope: str) -> bool:
    return scope in parse_app_scopes(app.scopes)


def require_scope(app: ExternalApp, scope: str) -> None:
    if not app_has_scope(app, scope):
        raise HTTPException(
            status_code=403,
            detail=f"缺少权限 scope：{scope}",
        )


MCP_ENDPOINT = "/api/mcp"
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_INFO = {
    "name": "ontometa",
    "version": "1.0.0",
}

# ---------------------------------------------------------------------------
# MCP Tool + REST 目录（单一数据源）
# 「本体」是业务语义的统称；对外只暴露已发布的业务对象、关系、业务逻辑。
# ---------------------------------------------------------------------------

EXTERNAL_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "id": "list-domains",
        "name": "查询数据域列表",
        "tool_name": "list_domains",
        "category": "数据域",
        "description": "获取全部数据域及已发布业务对象/关系统计，便于按域筛选后续查询。",
        "auth_required": True,
        "required_scope": "domains:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/domains",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "数据域 ID"},
            {"name": "name", "type": "string", "description": "数据域名称"},
            {"name": "description", "type": "string", "description": "描述"},
            {
                "name": "published_object_type_count",
                "type": "integer",
                "description": "已发布业务对象数量",
            },
            {"name": "relation_type_count", "type": "integer", "description": "关系类型数量"},
        ],
        "example_result": [
            {
                "id": "dom-001",
                "name": "客户域",
                "description": "客户主数据",
                "published_object_type_count": 12,
                "relation_type_count": 8,
            }
        ],
    },
    {
        "id": "list-object-types",
        "name": "查询业务对象列表",
        "tool_name": "list_object_types",
        "category": "业务对象",
        "description": "查询已发布的业务对象，可按数据域过滤。",
        "auth_required": True,
        "required_scope": "objects:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/object-types",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain_id": {
                    "type": "string",
                    "description": "数据域 ID，不传则返回全部已发布对象",
                },
            },
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "业务对象 ID"},
            {"name": "name", "type": "string", "description": "技术名称"},
            {"name": "display_name", "type": "string", "description": "显示名称"},
            {"name": "description", "type": "string", "description": "描述"},
            {"name": "status", "type": "string", "description": "状态（对外均为 published）"},
            {"name": "property_count", "type": "integer", "description": "属性数量"},
        ],
        "example_result": [
            {
                "id": "obj-001",
                "name": "Customer",
                "display_name": "客户",
                "description": "企业客户主数据",
                "status": "published",
                "property_count": 15,
            }
        ],
    },
    {
        "id": "get-object-type",
        "name": "查询业务对象详情",
        "tool_name": "get_object_type",
        "category": "业务对象",
        "description": "获取单个已发布业务对象的完整定义，含属性、关联关系与绑定的业务逻辑。",
        "auth_required": True,
        "required_scope": "objects:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/object-types/{object_type_id}",
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type_id": {
                    "type": "string",
                    "description": "业务对象 ID",
                },
            },
            "required": ["object_type_id"],
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "业务对象 ID"},
            {"name": "properties", "type": "array", "description": "属性列表"},
            {"name": "outgoing_relations", "type": "array", "description": "出边关系"},
            {"name": "incoming_relations", "type": "array", "description": "入边关系"},
            {"name": "business_logics", "type": "array", "description": "关联业务逻辑"},
        ],
        "example_result": {
            "id": "obj-001",
            "name": "Customer",
            "display_name": "客户",
            "properties": [
                {
                    "id": "p1",
                    "name": "customer_id",
                    "display_name": "客户ID",
                    "data_type": "string",
                }
            ],
            "outgoing_relations": [],
            "incoming_relations": [],
            "business_logics": [],
        },
    },
    {
        "id": "list-relation-types",
        "name": "查询业务关系列表",
        "tool_name": "list_relation_types",
        "category": "业务关系",
        "description": "查询已发布的业务对象之间的关系定义，可按数据域过滤。",
        "auth_required": True,
        "required_scope": "relations:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/relation-types",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain_id": {
                    "type": "string",
                    "description": "数据域 ID，不传则返回全部已发布关系",
                },
            },
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "关系 ID"},
            {"name": "name", "type": "string", "description": "技术名称"},
            {"name": "display_name", "type": "string", "description": "显示名称"},
            {"name": "source_object_type_id", "type": "string", "description": "源业务对象 ID"},
            {"name": "target_object_type_id", "type": "string", "description": "目标业务对象 ID"},
            {"name": "cardinality", "type": "string", "description": "基数"},
        ],
        "example_result": [
            {
                "id": "rel-001",
                "name": "owns_account",
                "display_name": "拥有账户",
                "source_object_type_id": "obj-001",
                "target_object_type_id": "obj-002",
                "cardinality": "1:N",
            }
        ],
    },
    {
        "id": "get-relation-type",
        "name": "查询业务关系详情",
        "tool_name": "get_relation_type",
        "category": "业务关系",
        "description": "获取单个已发布业务关系的完整定义，含源/目标业务对象引用信息。",
        "auth_required": True,
        "required_scope": "relations:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/relation-types/{relation_type_id}",
        "input_schema": {
            "type": "object",
            "properties": {
                "relation_type_id": {
                    "type": "string",
                    "description": "关系 ID",
                },
            },
            "required": ["relation_type_id"],
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "关系 ID"},
            {"name": "source_object", "type": "object", "description": "源业务对象摘要"},
            {"name": "target_object", "type": "object", "description": "目标业务对象摘要"},
            {"name": "structure_type", "type": "string", "description": "结构类型"},
        ],
        "example_result": {
            "id": "rel-001",
            "name": "owns_account",
            "display_name": "拥有账户",
            "source_object": {"id": "obj-001", "display_name": "客户"},
            "target_object": {"id": "obj-002", "display_name": "账户"},
            "cardinality": "1:N",
        },
    },
    {
        "id": "list-business-logics",
        "name": "查询业务逻辑列表",
        "tool_name": "list_business_logics",
        "category": "业务逻辑",
        "description": "查询已发布的指标、标签、规则等业务逻辑，可按数据域或分类过滤。",
        "auth_required": True,
        "required_scope": "logics:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/business-logics",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain_id": {
                    "type": "string",
                    "description": "数据域 ID，不传则返回全部已发布逻辑",
                },
                "category_id": {
                    "type": "string",
                    "description": "业务逻辑分类 ID",
                },
            },
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "业务逻辑 ID"},
            {"name": "name", "type": "string", "description": "技术名称"},
            {"name": "display_name", "type": "string", "description": "显示名称"},
            {"name": "logic_type", "type": "string", "description": "类型：metric / tag / rule 等"},
            {"name": "expression_summary", "type": "string", "description": "表达式摘要"},
        ],
        "example_result": [
            {
                "id": "bl-001",
                "name": "active_customer_count",
                "display_name": "活跃客户数",
                "logic_type": "metric",
                "expression_summary": "COUNT(Customer WHERE status = active)",
            }
        ],
    },
    {
        "id": "get-business-logic",
        "name": "查询业务逻辑详情",
        "tool_name": "get_business_logic",
        "category": "业务逻辑",
        "description": "获取单个已发布业务逻辑的完整定义，含表达式与对象/属性绑定。",
        "auth_required": True,
        "required_scope": "logics:read",
        "rest_method": "GET",
        "rest_path": "/api/v1/business-logics/{logic_id}",
        "input_schema": {
            "type": "object",
            "properties": {
                "logic_id": {
                    "type": "string",
                    "description": "业务逻辑 ID",
                },
            },
            "required": ["logic_id"],
            "additionalProperties": False,
        },
        "output_fields": [
            {"name": "id", "type": "string", "description": "业务逻辑 ID"},
            {"name": "expression_summary", "type": "string", "description": "表达式摘要"},
            {"name": "expression_json", "type": "object", "description": "结构化表达式"},
            {"name": "object_bindings", "type": "array", "description": "对象绑定"},
            {"name": "property_bindings", "type": "array", "description": "属性绑定"},
        ],
        "example_result": {
            "id": "bl-001",
            "name": "active_customer_count",
            "display_name": "活跃客户数",
            "logic_type": "metric",
            "expression_summary": "COUNT(Customer WHERE status = active)",
            "object_bindings": [],
            "property_bindings": [],
        },
    },
]


def scope_for_tool(tool_name: str) -> str | None:
    tool = next(
        (
            item
            for item in EXTERNAL_MCP_TOOLS
            if item["tool_name"] == tool_name or item["id"] == tool_name
        ),
        None,
    )
    return tool["required_scope"] if tool else None


def _to_catalog_item(raw: dict[str, Any]) -> ExternalApiCatalogItem:
    return ExternalApiCatalogItem(
        id=raw["id"],
        name=raw["name"],
        tool_name=raw["tool_name"],
        category=raw["category"],
        description=raw["description"],
        auth_required=raw.get("auth_required", True),
        required_scope=raw["required_scope"],
        rest_method=raw.get("rest_method"),
        rest_path=raw.get("rest_path"),
        input_schema=raw.get("input_schema") or {"type": "object", "properties": {}},
        output_fields=raw.get("output_fields", []),
        example_result=raw.get("example_result"),
        mcp_endpoint=MCP_ENDPOINT,
    )


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    if isinstance(value, dict):
        return {k: _dump_model(v) for k, v in value.items()}
    from datetime import datetime

    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _tool_result(data: Any, *, is_error: bool = False) -> McpToolCallResult:
    text = data if isinstance(data, str) else json.dumps(_dump_model(data), ensure_ascii=False, indent=2)
    return McpToolCallResult(
        content=[{"type": "text", "text": text}],
        structuredContent=None if isinstance(data, str) else _dump_model(data),
        isError=is_error,
    )


def _validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> None:
    schema = tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        value = arguments.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"缺少必填参数：{key}")
    unknown = set(arguments) - set(properties)
    if unknown and schema.get("additionalProperties") is False:
        raise ValueError(f"不支持的参数：{', '.join(sorted(unknown))}")


class ExternalApiService:
    def list_catalog(self) -> list[ExternalApiCatalogItem]:
        return [_to_catalog_item(item) for item in EXTERNAL_MCP_TOOLS]

    def get_catalog_item(self, api_id: str) -> ExternalApiCatalogItem | None:
        for item in EXTERNAL_MCP_TOOLS:
            if item["id"] == api_id or item["tool_name"] == api_id:
                return _to_catalog_item(item)
        return None

    def get_raw_tool(self, tool_name: str) -> dict[str, Any] | None:
        for item in EXTERNAL_MCP_TOOLS:
            if item["tool_name"] == tool_name or item["id"] == tool_name:
                return item
        return None

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        """MCP tools/list 与控制台 catalog 同源（EXTERNAL_MCP_TOOLS）。"""
        return [
            {
                "name": item["tool_name"],
                "description": item["description"],
                "inputSchema": item["input_schema"],
                "annotations": {"requiredScope": item["required_scope"]},
            }
            for item in EXTERNAL_MCP_TOOLS
        ]

    def record_call(
        self,
        db: Session,
        *,
        app_id: str,
        tool_name: str | None,
        path: str | None,
        status_code: int,
        duration_ms: int | None = None,
        error_message: str | None = None,
    ) -> None:
        row = ExternalApiCallLog(
            app_id=app_id,
            tool_name=tool_name,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            error_message=error_message[:2000] if error_message else None,
        )
        db.add(row)
        db.commit()

    def list_call_logs(
        self, db: Session, *, app_id: str | None = None, limit: int = 50
    ) -> list[ExternalApiCallLogOut]:
        q = db.query(ExternalApiCallLog).order_by(ExternalApiCallLog.created_at.desc())
        if app_id:
            q = q.filter(ExternalApiCallLog.app_id == app_id)
        rows = q.limit(max(1, min(limit, 200))).all()
        return [ExternalApiCallLogOut.model_validate(r) for r in rows]

    def call_tool(
        self,
        db: Session,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        query_service: Any,
        workspace_service: Any,
        app: ExternalApp | None = None,
    ) -> McpToolCallResult:
        tool = self.get_raw_tool(tool_name)
        if not tool:
            return _tool_result(f"未知工具：{tool_name}", is_error=True)

        if app is not None:
            require_scope(app, tool["required_scope"])

        args = {k: v for k, v in (arguments or {}).items() if v is not None and v != ""}
        try:
            _validate_arguments(tool, args)
        except ValueError as exc:
            return _tool_result(str(exc), is_error=True)

        name = tool["tool_name"]
        try:
            if name == "list_domains":
                data = workspace_service.list_domains(db)
            elif name == "list_object_types":
                data = query_service.list_object_types(
                    db,
                    domain_context_id=args.get("domain_id"),
                    published_only=True,
                ).items
            elif name == "get_object_type":
                detail = query_service.get_object_type(db, args["object_type_id"])
                if not detail or detail.status != "published":
                    return _tool_result("业务对象不存在或未发布", is_error=True)
                data = detail
            elif name == "list_relation_types":
                data = query_service.list_relation_types(
                    db,
                    domain_context_id=args.get("domain_id"),
                    published_only=True,
                ).items
            elif name == "get_relation_type":
                detail = query_service.get_relation_type(db, args["relation_type_id"])
                if not detail or detail.status != "published":
                    return _tool_result("业务关系不存在或未发布", is_error=True)
                data = detail
            elif name == "list_business_logics":
                data = query_service.list_business_logics(
                    db,
                    domain_context_id=args.get("domain_id"),
                    category_id=args.get("category_id"),
                    published_only=True,
                ).items
            elif name == "get_business_logic":
                detail = query_service.get_business_logic(db, args["logic_id"])
                if not detail or detail.status != "published":
                    return _tool_result("业务逻辑不存在或未发布", is_error=True)
                data = detail
            else:
                return _tool_result(f"工具未实现：{name}", is_error=True)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — 对 Agent 返回可读错误
            return _tool_result(f"工具执行失败：{exc}", is_error=True)

        return _tool_result(data)

    def handle_mcp_rpc(
        self,
        db: Session,
        payload: dict[str, Any],
        *,
        query_service: Any,
        workspace_service: Any,
        app: ExternalApp,
    ) -> dict[str, Any] | None:
        """处理 MCP JSON-RPC 请求。notification 无 id 时返回 None。"""
        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") or {}

        def ok(result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        def err(code: int, message: str) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }

        if method == "initialize":
            return ok(
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": MCP_SERVER_INFO,
                }
            )

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return ok({})

        if method == "tools/list":
            return ok({"tools": self.list_mcp_tools()})

        if method == "tools/call":
            name = params.get("name")
            if not name:
                return err(-32602, "缺少 params.name")
            started = time.perf_counter()
            status_code = 200
            error_message = None
            try:
                result = self.call_tool(
                    db,
                    name,
                    params.get("arguments") or {},
                    query_service=query_service,
                    workspace_service=workspace_service,
                    app=app,
                )
                if result.isError:
                    status_code = 400
                    if result.content and isinstance(result.content[0].get("text"), str):
                        error_message = result.content[0]["text"]
                return ok(result.model_dump(by_alias=True))
            except HTTPException as exc:
                status_code = exc.status_code
                error_message = str(exc.detail)
                raise
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                try:
                    self.record_call(
                        db,
                        app_id=app.id,
                        tool_name=name,
                        path=MCP_ENDPOINT,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        error_message=error_message,
                    )
                except Exception:  # noqa: BLE001 — 日志失败不影响主流程
                    pass

        if req_id is None:
            return None
        return err(-32601, f"Method not found: {method}")

    def _to_out(self, app: ExternalApp, *, include_key: bool = False) -> ExternalAppOut:
        _ = include_key
        return ExternalAppOut(
            id=app.id,
            name=app.name,
            description=app.description,
            app_key=app.app_key,
            api_key_hint=_hint_from_prefix(app.api_key_prefix),
            api_key=None,
            scopes=parse_app_scopes(app.scopes),
            rate_limit_per_minute=app.rate_limit_per_minute,
            status=app.status,
            created_at=app.created_at,
            updated_at=app.updated_at,
            last_used_at=app.last_used_at,
        )

    def list_apps(self, db: Session) -> list[ExternalAppOut]:
        rows = db.query(ExternalApp).order_by(ExternalApp.created_at.desc()).all()
        return [self._to_out(row) for row in rows]

    def get_app(self, db: Session, app_id: str, *, include_key: bool = False) -> ExternalAppOut | None:
        row = db.get(ExternalApp, app_id)
        if not row:
            return None
        return self._to_out(row, include_key=include_key)

    def create_app(self, db: Session, payload: ExternalAppCreate) -> ExternalAppCreated:
        api_key = _generate_api_key()
        pepper = settings.api_key_hash_pepper
        key_hash = hash_api_key(api_key, pepper)
        scopes = _normalize_scopes(payload.scopes)
        row = ExternalApp(
            name=payload.name.strip(),
            description=(payload.description or "").strip() or None,
            app_key=_generate_app_key(),
            api_key_hash=key_hash,
            api_key_prefix=api_key_prefix(api_key),
            api_key=_legacy_api_key_placeholder(key_hash),
            scopes=_serialize_scopes(scopes),
            rate_limit_per_minute=payload.rate_limit_per_minute,
            status="active",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        out = self._to_out(row).model_dump()
        out["api_key"] = api_key
        return ExternalAppCreated(**out)

    def update_app(
        self, db: Session, app_id: str, payload: ExternalAppUpdate
    ) -> ExternalAppOut | None:
        row = db.get(ExternalApp, app_id)
        if not row:
            return None
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise ValueError("应用名称不能为空")
            row.name = name
        if payload.description is not None:
            row.description = payload.description.strip() or None
        if payload.status is not None:
            if payload.status not in {"active", "disabled"}:
                raise ValueError("状态仅支持 active 或 disabled")
            row.status = payload.status
        if payload.scopes is not None:
            row.scopes = _serialize_scopes(payload.scopes)
        if "rate_limit_per_minute" in payload.model_fields_set:
            row.rate_limit_per_minute = payload.rate_limit_per_minute
        db.commit()
        db.refresh(row)
        return self._to_out(row)

    def regenerate_key(self, db: Session, app_id: str) -> ExternalAppCreated | None:
        row = db.get(ExternalApp, app_id)
        if not row:
            return None
        api_key = _generate_api_key()
        pepper = settings.api_key_hash_pepper
        key_hash = hash_api_key(api_key, pepper)
        row.api_key_hash = key_hash
        row.api_key_prefix = api_key_prefix(api_key)
        row.api_key = _legacy_api_key_placeholder(key_hash)
        db.commit()
        db.refresh(row)
        out = self._to_out(row).model_dump()
        out["api_key"] = api_key
        return ExternalAppCreated(**out)

    def delete_app(self, db: Session, app_id: str) -> bool:
        row = db.get(ExternalApp, app_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    def _effective_rate_limit(self, app: ExternalApp) -> int:
        if app.rate_limit_per_minute is not None:
            return app.rate_limit_per_minute
        return settings.external_api_rate_limit_per_minute

    def authenticate(self, db: Session, api_key: str | None) -> ExternalApp:
        if not api_key or not api_key.strip():
            raise HTTPException(status_code=401, detail="缺少 API Key，请在请求头 X-API-Key 中传入")
        key = api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        pepper = settings.api_key_hash_pepper
        key_hash = hash_api_key(key, pepper)
        row = (
            db.query(ExternalApp)
            .filter(ExternalApp.api_key_hash == key_hash)
            .first()
        )
        if not row:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        if row.status != "active":
            raise HTTPException(status_code=403, detail="该应用已被禁用")

        limit = self._effective_rate_limit(row)
        allowed, _remaining = external_rate_limiter.check(row.id, limit)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，每分钟上限 {limit} 次",
                headers={"Retry-After": "60"},
            )

        row.last_used_at = _now()
        db.commit()
        return row


def require_external_app(
    db: Session,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> ExternalApp:
    """依赖注入：校验外部调用方身份。优先 X-API-Key，其次 Authorization Bearer。"""
    service = ExternalApiService()
    token = x_api_key or authorization
    return service.authenticate(db, token)
