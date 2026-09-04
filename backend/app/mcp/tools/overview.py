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


@register_tool
class GetOntologyOverviewTool:
    name = "get_ontology_overview"
    required_role = "reader"
    description = (
        "一次返回本体元信息、对象角色/板块分布和业务对象精简清单。"
        "用于快速建立本体地图；需要完整字段时再调用 query_objects 或 query_object_detail。"
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
        },
        "required": ["ontology_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        ontology_id = str(arguments.get("ontology_id") or "").strip()
        if not ontology_id:
            return ToolResult(success=False, error="缺少 ontology_id")
        published_only = bool(arguments.get("published_only", True))
        object_limit = as_int(arguments.get("object_limit"), 300, low=1, high=500)

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
                objects = [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "display_name": item.get("display_name"),
                        "table_role": item.get("table_role"),
                        "segment_id": item.get("segment_id"),
                        "segment_name": item.get("segment_name"),
                    }
                    for item in (dump(row) for row in page.items)
                ]
                return ToolResult(
                    success=True,
                    data={
                        "ontology": dump(ontology),
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
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询本体概览失败：{exc}")
