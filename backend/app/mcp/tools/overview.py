"""一次性本体概览工具。

把常见的「查本体 → 看角色分布 → 拉业务对象」压成一次只读调用；明细仍通过
``query_objects`` 分页取得，概览只返回业务对象的精简投影。
"""

from __future__ import annotations

from typing import Any

from app.models.ontology import OntologyStatus
from app.services.logic_query import OntologyQueryService

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, dump, session

_query = OntologyQueryService()

# 本体记录上的这三个计数器是**草稿域全量**，而同一份回包里的分布/清单是按
# published_only 算的。两组数听起来是一回事（"对象数"），差着一个数量级
# （erpnext：草稿 1035 / 已发布 154），却没有任何字段说明谁是谁——调用方把
# 「1035 个对象，其中 44 个业务对象」讲出去，两个数字来自不同的世界。
# 所以从 ontology 块里摘掉，改到 counts 里按口径分别命名。
_AMBIGUOUS_COUNTS = ("object_type_count", "relation_type_count", "business_logic_count")

# 业务对象清单的字段面。默认不带 segment_id——板块的 id→名映射在同一份回包的
# object_distribution.by_segment 里已经有了，逐行再抄一个 UUID 只是把清单撑大。
_ITEM_FIELDS = ("id", "name", "display_name", "table_role", "segment_name")
_ITEM_ALWAYS = ("id", "name", "display_name")
_ITEM_ALL_FIELDS = (
    "id",
    "name",
    "display_name",
    "table_role",
    "segment_id",
    "segment_name",
)


def _ontology_meta(ontology) -> dict[str, Any]:
    meta = dump(ontology)
    if isinstance(meta, dict):
        for key in _AMBIGUOUS_COUNTS:
            meta.pop(key, None)
    return meta


def _counts(
    ontology,
    *,
    published_only: bool,
    scoped_object_count: int,
    scoped_relation_count: int,
    scoped_logic_count: int,
) -> dict[str, Any]:
    raw = dump(ontology)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "scope": "published" if published_only else "draft",
        "in_scope_object_types": scoped_object_count,
        "in_scope_relation_types": scoped_relation_count,
        "in_scope_business_logics": scoped_logic_count,
        "draft_object_types": raw.get("object_type_count"),
        "draft_relation_types": raw.get("relation_type_count"),
        "draft_business_logics": raw.get("business_logic_count"),
        "note": (
            "in_scope_* 是本次 published_only 口径下的数；draft_* 是草稿域全量。"
            "两者不可混着说——差额是还没发布的草稿，不是「丢失的对象」。"
        ),
    }


@register_tool
class GetOntologyOverviewTool:
    name = "get_ontology_overview"
    required_role = "reader"
    description = (
        "一次返回本体元信息、对象角色/板块分布和业务对象精简清单。"
        "用于快速建立本体地图；需要完整字段时再调用 query_objects 或 query_object_detail。\n"
        "**计数看 counts 块，别混口径**：`in_scope_*` 是本次 published_only 口径下的数，"
        "`draft_*` 是草稿域全量。两者常差一个数量级，差额是未发布草稿；"
        "分布与清单一律按 `in_scope_*` 那个口径，报数时要说明是哪个口径。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {"type": "string", "description": "本体 ID（必填）"},
            "published_only": {
                "type": "boolean",
                "description": "只看已发布本体和对象",
                "default": True,
            },
            "object_limit": {
                "type": "integer",
                "description": "业务对象清单上限；超出时用 business_objects.truncated 提示",
                "default": 300,
                "minimum": 1,
                "maximum": 500,
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "business_objects 每行要哪些字段（id/name/display_name 恒在）。"
                    "留空 = 精简面（不含 segment_id，板块映射见 object_distribution）；"
                    "[\"*\"] = 全量"
                ),
            },
        },
        "required": ["ontology_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        ontology_id = str(arguments.get("ontology_id") or "").strip()
        if not ontology_id:
            return ToolResult(success=False, error="缺少 ontology_id")
        published_only = bool(arguments.get("published_only", True))
        object_limit = as_int(arguments.get("object_limit"), 300, low=1, high=500)
        raw_fields = arguments.get("fields")
        fields = (
            [str(f).strip() for f in raw_fields if str(f).strip()]
            if isinstance(raw_fields, list)
            else None
        ) or None

        try:
            with session() as db:
                ontology = _query.get_ontology(db, ontology_id)
                if ontology is None:
                    return ToolResult(success=False, error=f"本体不存在：{ontology_id}")
                if published_only and ontology.status != OntologyStatus.PUBLISHED.value:
                    return ToolResult(success=False, error=f"本体不存在：{ontology_id}")

                role_distribution = _query.group_object_types(
                    db,
                    group_by="role",
                    ontology_id=ontology_id,
                    published_only=published_only,
                )
                segment_distribution = _query.group_object_types(
                    db,
                    group_by="segment",
                    ontology_id=ontology_id,
                    published_only=published_only,
                )
                page = _query.list_object_types(
                    db,
                    ontology_id=ontology_id,
                    published_only=published_only,
                    role_in=["business_object"],
                    limit=object_limit,
                    offset=0,
                )
                wanted = (
                    set(_ITEM_ALL_FIELDS)
                    if fields and "*" in fields
                    else (set(fields or _ITEM_FIELDS) | set(_ITEM_ALWAYS))
                )
                objects = [
                    {k: item.get(k) for k in _ITEM_ALL_FIELDS if k in wanted}
                    for item in (dump(row) for row in page.items)
                ]
                relation_page = _query.list_relation_types(
                    db,
                    ontology_id=ontology_id,
                    published_only=published_only,
                    limit=1,
                )
                logic_page = _query.list_business_logics(
                    db,
                    ontology_id=ontology_id,
                    published_only=published_only,
                    limit=1,
                )
                return ToolResult(
                    success=True,
                    data={
                        "ontology": _ontology_meta(ontology),
                        "counts": _counts(
                            ontology,
                            published_only=published_only,
                            scoped_object_count=sum(
                                (role_distribution.get("by_role") or {}).values()
                            ),
                            scoped_relation_count=relation_page.total,
                            scoped_logic_count=logic_page.total,
                        ),
                        "object_distribution": {
                            **role_distribution,
                            **segment_distribution,
                        },
                        "business_objects": {
                            "items": objects,
                            "total": page.total,
                            "truncated": page.total > len(objects),
                        },
                    },
                    metadata={
                        "ontology_id": ontology_id,
                        "published_only": published_only,
                        "object_limit": object_limit,
                        "item_fields": (
                            "all" if (fields and "*" in fields) else (fields or "lean")
                        ),
                        # 每个计数属于哪个口径，写死在回包里而不是靠调用方推。
                        "counts_scope": {
                            "draft_*": "草稿域全量（本体记录上的存量计数器）",
                            "in_scope_*": "本次 published_only 口径下重算",
                            "object_distribution": "本次 published_only 口径",
                            "business_objects": "本次 published_only 口径",
                        },
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询本体概览失败：{exc}")
