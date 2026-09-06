"""数仓落点目录（只读）。

与 ``get_landing`` 的分工是「列」与「查」：那个回答「**这个**对象落在哪」，这个回答
「这个本体**都**落了些什么、哪些能查了」。少了列的那一半，调用方想知道「有哪些表可以拿
来加工」时唯一的办法是逐个对象去问，或者——实际发生的——按命名规则自己拼一个 `ods_xxx`。

同步/加工**不产生新的业务对象**：ODS、DWD 表都是既有实体的物理投影，在本体里没有独立
身份。所以「数仓里那张表叫什么」只有这份目录说了算。
"""

from __future__ import annotations

from app.models import Ontology, OntologyStatus

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_LAYERS = ["ods", "dwd", "dws", "ads"]
_DEFAULT_LIMIT = 30
_MAX_LIMIT = 200


@register_tool
class ListDatasetsTool:
    """列出本体在数仓里的物理落点"""

    name = "list_datasets"
    required_role = "reader"
    description = (
        "列出一个本体在数仓里的**物理落点目录**：哪个对象/口径落到了哪张表、在哪一层、"
        "建了吗、数搬了吗、现在能不能查。\n"
        "回答「数据同步到哪了 / 哪些表可以拿来加工 / 这个域落了多少张表」时用它；"
        "问单个对象落在哪用 `get_landing` 更直接。\n"
        "**只列已登记的落点**：没登记就是没落地，此时禁止按命名规则推测表名——"
        "推出来的表通常并不存在。数仓里的无主表也不在其中（那是治理问题，不是可选项）。\n"
        "ontology_id 留空时列**全部已发布本体**的落点。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {
                "type": "string",
                "description": "限定本体；留空＝全部已发布本体",
            },
            "layer": {
                "type": "string",
                "enum": _LAYERS,
                "description": "只看某一层（ods=同步落点，dwd/dws=加工结果，ads=指标结果）",
            },
            "q": {"type": "string", "description": "按实体名/表名过滤"},
            "source_ready_only": {
                "type": "boolean",
                "description": "只看上游已就绪的落点",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": f"返回条数上限（默认 {_DEFAULT_LIMIT}）",
                "default": _DEFAULT_LIMIT,
                "minimum": 1,
                "maximum": _MAX_LIMIT,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services import dataset_catalog

        ontology_id = str(arguments.get("ontology_id") or "").strip() or None
        layer = str(arguments.get("layer") or "").strip() or None
        keyword = str(arguments.get("q") or "").strip() or None
        source_ready_only = bool(arguments.get("source_ready_only"))
        limit = as_int(arguments.get("limit"), _DEFAULT_LIMIT, low=1, high=_MAX_LIMIT)

        try:
            with session() as db:
                if ontology_id:
                    if db.get(Ontology, ontology_id) is None:
                        return ToolResult(success=False, error="本体不存在")
                    scope = [ontology_id]
                else:
                    scope = [
                        row[0]
                        for row in db.query(Ontology.id)
                        .filter(Ontology.status == OntologyStatus.PUBLISHED.value)
                        .all()
                    ]
                    if not scope:
                        return ToolResult(
                            success=True,
                            data={"items": [], "total": 0, "shown": 0},
                            metadata={"note": "当前没有已发布本体，数仓里也就没有登记的落点"},
                        )

                entries = []
                for oid in scope:
                    entries.extend(
                        dataset_catalog.list_datasets(
                            db,
                            oid,
                            layer=layer,
                            q=keyword,
                            source_ready_only=source_ready_only,
                        )
                    )
                items = [
                    {
                        "ref": e.ref,
                        "entity": e.entity_display_name,
                        "entity_name": e.entity_name,
                        "kind": e.entity_kind,
                        "layer": e.layer,
                        "physical": e.physical,
                        "state": e.state,
                        "source_ready": e.source_ready,
                        "queryable": e.queryable,
                        **({"mode": e.mode} if e.mode else {}),
                    }
                    for e in entries[:limit]
                ]
                return ToolResult(
                    success=True,
                    data={"items": items, "total": len(entries), "shown": len(items)},
                    metadata={
                        "ontology_ids": scope,
                        "truncated": len(entries) > len(items),
                        "filters": {
                            "layer": layer,
                            "q": keyword,
                            "source_ready_only": source_ready_only,
                        },
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取落点目录失败：{exc}")
