"""业务对象 / 关系查询工具集（只读）。

**读模型复用 ``OntologyQueryService``，不在这里另写一遍 ORM 查询**：对象摘要里的
``source_provenance`` / ``landing`` / ``property_count`` 等都是派生值，绕开服务层
直接查表就会静默丢掉它们（见 memory「派生值没对上会静默毁掉界面」）——同一批
字段前端和 Agent 都在用，两套推导迟早分叉。
"""

from __future__ import annotations

from typing import Any

from app.services.logic_query import OntologyQueryService

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, dump, session

_query = OntologyQueryService()

# 判定证据快照，只给复核界面的人看；对定位实体毫无用处却随列表成倍放大。
# 与 services/tool_result_compaction 的第 1 级丢弃同口径。
_NOISE_KEYS = ("role_signals",)

# 对象角色取值来自 ObjectType.table_role（services/object_classifier 判定）。
_TABLE_ROLES = ["business_object", "data_table", "bridge", "technical"]


def _lean(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _NOISE_KEYS}


@register_tool
class QueryObjectsTool:
    """查询业务对象列表"""

    name = "query_objects"
    description = (
        "查询本体中的业务对象（表/实体）。可按角色、关键词过滤。"
        "关键词同时匹配对象标识名、显示名、描述和物理源表名（source_ref）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {
                "type": "string",
                "description": "本体 ID；留空则跨本体查询（配合 domain_context_id 或 search 使用）",
            },
            "domain_context_id": {
                "type": "string",
                "description": "数据域 ID（按域查询该域下的本体）",
            },
            "role": {
                "type": "string",
                "description": "对象角色过滤（对应 table_role）",
                "enum": _TABLE_ROLES,
            },
            "search": {
                "type": "string",
                "description": "关键词；匹配对象名/显示名/描述/物理源表名",
            },
            "needs_review": {
                "type": "boolean",
                "description": "只看待复核（true）或只看已判定（false）；留空不过滤",
            },
            "published_only": {
                "type": "boolean",
                "description": "只看已发布的本体与对象",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限（total 字段给出未截断的总数）",
                "default": 50,
                "minimum": 1,
                "maximum": 500,
            },
            "offset": {"type": "integer", "description": "翻页偏移", "default": 0},
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        domain_context_id = (arguments.get("domain_context_id") or "").strip() or None
        role = (arguments.get("role") or "").strip() or None
        limit = as_int(arguments.get("limit"), 50, low=1, high=500)
        offset = as_int(arguments.get("offset"), 0, low=0, high=1_000_000)
        needs_review = arguments.get("needs_review")

        try:
            with session() as db:
                page = _query.list_object_types(
                    db,
                    ontology_id=ontology_id,
                    domain_context_id=domain_context_id,
                    published_only=bool(arguments.get("published_only", False)),
                    q=(arguments.get("search") or "").strip() or None,
                    role_in=[role] if role else None,
                    needs_review=(
                        None if needs_review is None else bool(needs_review)
                    ),
                    limit=limit,
                    offset=offset,
                )
                objects = [_lean(dump(item)) for item in page.items]
                return ToolResult(
                    success=True,
                    data={"objects": objects},
                    metadata={
                        "count": len(objects),
                        "total": page.total,
                        "truncated": page.total > offset + len(objects),
                        "ontology_id": ontology_id,
                        "filters": {
                            "role": role,
                            "search": arguments.get("search"),
                            "needs_review": needs_review,
                            "published_only": bool(
                                arguments.get("published_only", False)
                            ),
                        },
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询对象失败：{exc}")


@register_tool
class QueryObjectDetailTool:
    """查询对象详情"""

    name = "query_object_detail"
    description = (
        "查询单个业务对象的详情：属性（字段）、进出关系、绑定的业务口径、物理落点。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "object_id": {"type": "string", "description": "对象 ID（必填）"},
            "published_only": {
                "type": "boolean",
                "description": "已发布视角：对象/关系未发布则视为不存在",
                "default": False,
            },
        },
        "required": ["object_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        object_id = (arguments.get("object_id") or "").strip()
        if not object_id:
            return ToolResult(success=False, error="缺少 object_id")

        try:
            with session() as db:
                detail = _query.get_object_type(
                    db,
                    object_id,
                    published_only=bool(arguments.get("published_only", False)),
                )
                if detail is None:
                    return ToolResult(
                        success=False, error=f"对象不存在：{object_id}"
                    )
                data = _lean(dump(detail))
                return ToolResult(
                    success=True,
                    data=data,
                    metadata={
                        "object_id": object_id,
                        "property_count": len(data.get("properties") or []),
                        "outgoing_relation_count": len(
                            data.get("outgoing_relations") or []
                        ),
                        "incoming_relation_count": len(
                            data.get("incoming_relations") or []
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询对象详情失败：{exc}")


@register_tool
class QueryRelationsTool:
    """查询关系列表"""

    name = "query_relations"
    description = (
        "查询本体中的业务对象关系（外键/引用/包含/转化）。"
        "关系两端给的是对象名，写 JOIN 时的连接键在 source_evidence 里。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {"type": "string", "description": "本体 ID"},
            "domain_context_id": {"type": "string", "description": "数据域 ID"},
            "search": {
                "type": "string",
                "description": "关键词；匹配关系名与两端对象名",
            },
            "display_name": {
                "type": "string",
                "description": "按关系显示名精确过滤（如「属于」）",
            },
            "needs_review": {
                "type": "boolean",
                "description": "只看待复核（true）或只看已判定（false）",
            },
            "published_only": {
                "type": "boolean",
                "description": "只看已发布的关系",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限",
                "default": 50,
                "minimum": 1,
                "maximum": 500,
            },
            "offset": {"type": "integer", "description": "翻页偏移", "default": 0},
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        domain_context_id = (arguments.get("domain_context_id") or "").strip() or None
        limit = as_int(arguments.get("limit"), 50, low=1, high=500)
        offset = as_int(arguments.get("offset"), 0, low=0, high=1_000_000)
        needs_review = arguments.get("needs_review")

        try:
            with session() as db:
                page = _query.list_relation_types(
                    db,
                    ontology_id=ontology_id,
                    domain_context_id=domain_context_id,
                    published_only=bool(arguments.get("published_only", False)),
                    q=(arguments.get("search") or "").strip() or None,
                    display_name=(arguments.get("display_name") or "").strip() or None,
                    needs_review=(
                        None if needs_review is None else bool(needs_review)
                    ),
                    limit=limit,
                    offset=offset,
                )
                relations = [dump(item) for item in page.items]
                return ToolResult(
                    success=True,
                    data={"relations": relations},
                    metadata={
                        "count": len(relations),
                        "total": page.total,
                        "truncated": page.total > offset + len(relations),
                        "ontology_id": ontology_id,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询关系失败：{exc}")
