"""业务对象 / 关系查询工具集（只读）。

**读模型复用 ``OntologyQueryService``，不在这里另写一遍 ORM 查询**：对象摘要里的
``source_provenance`` / ``landing`` / ``property_count`` 等都是派生值，绕开服务层
直接查表就会静默丢掉它们（见 memory「派生值没对上会静默毁掉界面」）——同一批
字段前端和 Agent 都在用，两套推导迟早分叉。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.logic_query import OntologyQueryService

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, dump, session

_query = OntologyQueryService()

# 判定证据快照，只给复核界面的人看；对定位实体毫无用处却随列表成倍放大。
# 与 services/tool_result_compaction 的第 1 级丢弃同口径。
_NOISE_KEYS = ("role_signals",)

# 默认投影。读模型 ObjectTypeSummary 是给**前端复核工作台**用的，带着 origin /
# pinned_fields / conflicts / role_reason / top_neighbors 这些只有人在界面上拍板时
# 才用得上的字段——随列表成倍放大：真机上 `limit=5` 回了 9.2 KB，模型只好把结果
# 落盘再 grep。这里定一个"回答结构问题够用"的最小面，其余按需用 fields 取回。
_LEAN_FIELDS = (
    "id",
    "name",
    "display_name",
    "description",
    "table_role",
    "status",
    "needs_review",
    "segment_id",
    "segment_name",
    "property_count",
    "relation_count",
    "business_logic_count",
    "row_count",
    "source_ref",
    "source_provenance",
)
# 无论 fields 怎么给都必须在场：少了这几个，返回的行就没法拿去调别的工具。
_ALWAYS_FIELDS = ("id", "name", "display_name")
_ALL = "*"

# 对象角色取值来自 ObjectType.table_role（services/object_classifier 判定）。
_TABLE_ROLES = ["business_object", "data_table", "bridge", "technical"]


def _mermaid_label(value: Any) -> str:
    """Mermaid label escaping; relation/object names are data, not syntax."""
    text = str(value or "未命名").replace("\n", " ").replace("\r", " ")
    return text.replace('"', "'")[:120]


def _relations_mermaid(relations: list[dict[str, Any]], *, truncated: bool) -> str:
    def node_id(object_id: str) -> str:
        digest = hashlib.sha1(object_id.encode("utf-8")).hexdigest()[:10]
        return f"n{digest}"

    nodes: dict[str, str] = {}
    lines = ["```mermaid", "flowchart LR"]
    for index, relation in enumerate(relations):
        source_id = str(relation.get("source_object_type_id") or f"source_{index}")
        target_id = str(relation.get("target_object_type_id") or f"target_{index}")
        source_node = node_id(source_id)
        target_node = node_id(target_id)
        nodes.setdefault(
            source_node, _mermaid_label(relation.get("source_object_name") or source_id)
        )
        nodes.setdefault(
            target_node, _mermaid_label(relation.get("target_object_name") or target_id)
        )
        edge_label = _mermaid_label(
            relation.get("display_name")
            or relation.get("structure_type")
            or relation.get("name")
            or "关系"
        )
        lines.append(f'  {source_node}["{nodes[source_node]}"] -->|"{edge_label}"| {target_node}["{nodes[target_node]}"]')
    if not relations:
        lines.append("  %% 当前筛选没有关系")
    if truncated:
        lines.append("  %% 关系结果已截断；请分页查询完整关系")
    lines.extend(["```"])
    return "\n".join(lines)


def _lean(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _NOISE_KEYS}


def _project_fields(row: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """按 fields 裁剪一行。``None`` = 默认精简面；``["*"]`` = 全量（仍去掉噪音键）。"""
    row = _lean(row)
    if fields and _ALL in fields:
        return row
    wanted = set(fields or _LEAN_FIELDS) | set(_ALWAYS_FIELDS)
    return {k: v for k, v in row.items() if k in wanted}


def _unknown_fields(fields: list[str] | None, sample: dict[str, Any]) -> list[str]:
    """调用方点名了但读模型里没有的字段。静默忽略会让人以为"那个字段是空的"。"""
    if not fields or _ALL in fields:
        return []
    return sorted(f for f in fields if f not in sample and f not in _ALWAYS_FIELDS)


@register_tool
class QueryObjectsTool:
    """查询业务对象列表"""

    name = "query_objects"
    description = (
        "查询本体中的业务对象（表/实体）。可按角色、关键词过滤，或用 group_by=role/segment 只取分布统计。"
        "关键词同时匹配对象标识名、显示名、描述和物理源表名（source_ref）。\n"
        "**默认只回精简字段面**（标识/角色/状态/板块/数量/源表）。复核工作台那些字段"
        "（role_reason、conflicts、pinned_fields、top_neighbors 等）默认不回——"
        "确实需要时用 `fields` 点名，或 `fields=[\"*\"]` 取全量。\n"
        "**知道要找哪个对象时用 resolve_subject**，它精确匹配置顶并会指出同名跨域；"
        "本工具适合列举、过滤和分布统计。"
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
            "group_by": {
                "type": "string",
                "description": "只返回分布统计，不返回对象明细",
                "enum": ["role", "segment"],
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
            "include_mermaid": {
                "type": "boolean",
                "description": "附加当前结果页的 Mermaid 关系图文本",
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
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "点名要哪些字段（id/name/display_name 恒在）。"
                    "留空 = 精简面；[\"*\"] = 全量（含 role_reason、conflicts、"
                    "top_neighbors 等复核字段，很占上下文，只在确实要看判定依据时用）"
                ),
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        domain_context_id = (arguments.get("domain_context_id") or "").strip() or None
        role = (arguments.get("role") or "").strip() or None
        group_by = (arguments.get("group_by") or "").strip() or None
        limit = as_int(arguments.get("limit"), 50, low=1, high=500)
        offset = as_int(arguments.get("offset"), 0, low=0, high=1_000_000)
        needs_review = arguments.get("needs_review")
        raw_fields = arguments.get("fields")
        fields = (
            [str(f).strip() for f in raw_fields if str(f).strip()]
            if isinstance(raw_fields, list)
            else None
        ) or None

        try:
            with session() as db:
                if group_by:
                    distribution = _query.group_object_types(
                        db,
                        group_by=group_by,
                        ontology_id=ontology_id,
                        domain_context_id=domain_context_id,
                        published_only=bool(arguments.get("published_only", False)),
                        q=(arguments.get("search") or "").strip() or None,
                        role_in=[role] if role else None,
                        needs_review=(
                            None if needs_review is None else bool(needs_review)
                        ),
                    )
                    total = sum(
                        distribution.get("by_role", {}).values()
                    ) or sum(
                        item.get("count", 0)
                        for item in distribution.get("by_segment", [])
                    )
                    return ToolResult(
                        success=True,
                        data=distribution,
                        metadata={
                            "group_by": group_by,
                            "total": total,
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
                dumped = [dump(item) for item in page.items]
                objects = [_project_fields(row, fields) for row in dumped]
                unknown = _unknown_fields(fields, dumped[0] if dumped else {})
                return ToolResult(
                    success=True,
                    data={"objects": objects},
                    metadata={
                        "count": len(objects),
                        "total": page.total,
                        "truncated": page.total > offset + len(objects),
                        # 说破投影，免得调用方把"默认没回"读成"这个字段是空的"。
                        "fields": "all" if (fields and "*" in fields) else (fields or "lean"),
                        **({"unknown_fields": unknown} if unknown else {}),
                        "ontology_id": ontology_id,
                        "filters": {
                            "role": role,
                            "group_by": group_by,
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
                data: dict[str, Any] = {"relations": relations}
                if arguments.get("include_mermaid"):
                    data["mermaid"] = _relations_mermaid(
                        relations,
                        truncated=page.total > offset + len(relations),
                    )
                return ToolResult(
                    success=True,
                    data=data,
                    metadata={
                        "count": len(relations),
                        "total": page.total,
                        "truncated": page.total > offset + len(relations),
                        "ontology_id": ontology_id,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询关系失败：{exc}")
