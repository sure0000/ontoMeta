"""取数辅助：关联路径与字段取值画像（决定 SQL 写得对不对的两件事）。

两个工具堵的都是「SQL 语法完全合法、结果却是错的」那一类失败：

- ``find_join_path``：JOIN 该怎么连、连了会不会扇出。语义层在这里从「事后否决」
  转成「事前给答案」——**找不到路径不是错误**，「本体中这两个对象无从关联」本身
  就是一条可作答的事实；把它当错误报，模型下一步就会自己编一个 JOIN。
- ``profile_values``：某字段实际存着什么值。本体只定义字段存在，不保证你猜的枚举值
  （「已完成」「广东省」）真的在库里；**猜错的字面量让查询返回 0 行而不报错**，
  答案就错得看不出来。

**权限**：``profile_values`` 读真实数据，与 ``execute_sql`` 同价——取同一份
``agent_run_sql_min_role``，不写死。``find_join_path`` 只读本体结构，reader 即可。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.data_app import resolve_domain_data_source
from app.services.logic_query import OntologyQueryService
from app.services.ontology_projection import build_projection
from app.services.query_routing import prepare_object_read

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_query = OntologyQueryService()


def _resolve_ontology_id(db, token: str, given: str | None) -> str | None:
    """本体作用域：调用方给了就用，没给就从对象自己身上取。

    **不猜「当前锚定本体」**——锚错本体会画出一张空图 / 找不到路径，而调用方会把它
    读成「这两个对象确实无从关联」，一个看起来有值的错答案。
    """
    if given:
        return given
    detail = _query.get_object_type(db, token)
    return detail.ontology_id if detail is not None else None


def _resolve_object(proj, db, token: str):
    """对象标识：名字和 id 都认（模型两者常混着传）。"""
    obj = proj.object_of(token)
    if obj is not None:
        return obj
    detail = _query.get_object_type(db, token)
    return proj.object_of(detail.name) if detail is not None else None


@register_tool
class FindJoinPathTool:
    """两个对象之间的关联路径"""

    name = "find_join_path"
    required_role = "reader"
    description = (
        "查两个业务对象之间**本体认可的**关联路径：每一跳的关系、ON 连接键、基数链，"
        "以及可直接用的 `sql_hint`（FROM/JOIN 片段）。\n"
        "跨对象查询前先调它，不要自己按字段名猜 JOIN——猜出来的连接键往往不存在，"
        "或者连出一张会扇出的表把 SUM 放大。\n"
        "**找不到路径不是错误**：返回 found=0 和说明，意思是本体中这两个对象确实无从关联，"
        "此时如实说明或换对象，不得自行构造 JOIN。\n"
        "`measure_object` 指定扇出相对谁判定（问「订单金额按客户区域汇总」时度量在订单）；"
        "`fanout_risk` 非空表示该路径会放大度量，`safe_aggs` 是仍然安全的聚合。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "from_object": {"type": "string", "description": "起点对象的标识名或 ID"},
            "to_object": {"type": "string", "description": "终点对象的标识名或 ID"},
            "ontology_id": {
                "type": "string",
                "description": "本体 ID；留空则从 from_object 反查（此时 from_object 要给 ID）",
            },
            "measure_object": {
                "type": "string",
                "description": "度量所在对象；留空按起点判定扇出",
            },
            "max_hops": {
                "type": "integer",
                "description": "最大跳数（默认 3）",
                "minimum": 1,
                "maximum": 5,
            },
            "limit": {
                "type": "integer",
                "description": "最多返回几条路径（默认 3）",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["from_object", "to_object"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services.semantic_navigator import (
            DEFAULT_MAX_HOPS,
            DEFAULT_PATH_LIMIT,
            describe_paths,
            find_join_path,
        )

        source_token = (arguments.get("from_object") or "").strip()
        target_token = (arguments.get("to_object") or "").strip()
        if not source_token or not target_token:
            return ToolResult(success=False, error="需要 from_object 与 to_object")
        max_hops = as_int(arguments.get("max_hops"), DEFAULT_MAX_HOPS, low=1, high=5)
        limit = as_int(arguments.get("limit"), DEFAULT_PATH_LIMIT, low=1, high=10)

        try:
            with session() as db:
                ontology_id = _resolve_ontology_id(
                    db, source_token, (arguments.get("ontology_id") or "").strip() or None
                )
                if not ontology_id:
                    return ToolResult(
                        success=False,
                        error="无法确定本体作用域",
                        data={"hint": "给上 ontology_id（用 query_ontology 取），或把 from_object 传成对象 ID"},
                    )
                proj = build_projection(db, ontology_id, None)
                resolved: dict[str, Any] = {}
                for token, label in ((source_token, "from_object"), (target_token, "to_object")):
                    obj = _resolve_object(proj, db, token)
                    if obj is None:
                        return ToolResult(
                            success=False,
                            error=f"{label}「{token}」不是该本体里的已发布业务对象",
                            data={"ontology_id": ontology_id},
                        )
                    resolved[label] = obj.name

                measure = (arguments.get("measure_object") or "").strip() or None
                if measure:
                    measure_obj = _resolve_object(proj, db, measure)
                    measure = measure_obj.name if measure_obj is not None else measure

                paths = find_join_path(
                    proj,
                    resolved["from_object"],
                    resolved["to_object"],
                    max_hops=max_hops,
                    limit=limit,
                    measure_object=measure,
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询关联路径失败：{exc}")

        data: dict[str, Any] = {
            "from": resolved["from_object"],
            "to": resolved["to_object"],
            "ontology_id": ontology_id,
            "found": len(paths),
            "paths": describe_paths(paths),
        }
        if not paths:
            # success=True：这是一条结论，不是一次故障。
            data["note"] = (
                "本体中这两个对象之间没有可用的关联路径；不得自行构造 JOIN，"
                "请如实说明无法关联，或换用其它对象。"
            )
            return ToolResult(
                success=True,
                data=data,
                metadata={"found": 0, "ontology_id": ontology_id, "joinable": False},
            )

        best = paths[0]
        return ToolResult(
            success=True,
            data=data,
            metadata={
                "found": len(paths),
                "ontology_id": ontology_id,
                "shortest_hops": best.hop_count,
                # 每一跳的 ON 都推得出才给 sql_hint；半截 SQL 只会误导。
                "joinable": best.joinable,
                "fanout_risk": best.fanout_risk,
                "measure_object": best.measure_object,
                "safe_aggs": best.safe_aggs,
            },
        )


@register_tool
class ProfileValuesTool:
    """字段取值画像"""

    name = "profile_values"
    # 读真实数据，与 execute_sql 同价。写死 reader 就等于开了一个绕过 SQL 权限的后门：
    # 一次画像等于一句 SELECT DISTINCT。
    required_role = settings.agent_run_sql_min_role
    description = (
        "查某个字段**实际存着什么值**：类别/标识字段给 TopN 取值与频次、去重数；"
        "度量字段给最小/最大/均值；时间字段给时间区间；另有空值率。\n"
        "**写带字面量的 WHERE 之前先调它**——本体只保证字段存在，不保证你猜的枚举值"
        "（如「已完成」）真在库里；猜错的字面量会让查询返回 0 行而不报错。\n"
        "数据没落地或数仓投影未就绪时返回 available=false 与原因，不报错、也不要据此猜值。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "object_id": {"type": "string", "description": "业务对象的 ID 或标识名"},
            "property": {"type": "string", "description": "字段标识名（本体属性 name）"},
            "ontology_id": {
                "type": "string",
                "description": "本体 ID；留空则从 object_id 反查（此时要传对象 ID）",
            },
            "top_n": {
                "type": "integer",
                "description": "类别字段返回的取值个数上限",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["object_id", "property"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services.column_profiler import profile_property

        object_token = (arguments.get("object_id") or "").strip()
        property_token = (arguments.get("property") or "").strip()
        if not object_token or not property_token:
            return ToolResult(success=False, error="需要 object_id 与 property")
        top_n = arguments.get("top_n")
        top_n = as_int(top_n, 0, low=1, high=50) if top_n else None

        try:
            with session() as db:
                ontology_id = _resolve_ontology_id(
                    db, object_token, (arguments.get("ontology_id") or "").strip() or None
                )
                if not ontology_id:
                    return ToolResult(
                        success=False,
                        error="无法确定本体作用域",
                        data={"hint": "给上 ontology_id（用 query_ontology 取），或把 object_id 传成对象 ID"},
                    )
                proj = build_projection(db, ontology_id, None)
                obj = _resolve_object(proj, db, object_token)
                if obj is None:
                    return ToolResult(
                        success=False,
                        error=f"对象「{object_token}」不存在或未发布",
                        data={"ontology_id": ontology_id},
                    )
                prop = obj.resolve_property(property_token)
                if prop is None:
                    return ToolResult(
                        success=False,
                        error=f"字段「{property_token}」不属于对象「{obj.display_name}」",
                        data={
                            "available_columns": sorted(p.name for p in obj.props.values())[:20]
                        },
                    )

                source = resolve_domain_data_source(db)
                if source is None:
                    return ToolResult(
                        success=True,
                        data={
                            "object": obj.name,
                            "property": prop.name,
                            "available": False,
                            "note": "当前未配置默认 Doris 数仓，无法读取真实取值。不得据此猜测字面量。",
                        },
                        metadata={"available": False, "reason": "no_warehouse"},
                    )

                prepared = prepare_object_read(
                    db, datasource=source, ontology_id=ontology_id, object_names=[obj.name]
                )
                if not prepared.readable:
                    # 未就绪是**结论**不是故障：数据还没搬过来，画像自然没有。
                    return ToolResult(
                        success=True,
                        data={
                            "object": obj.name,
                            "property": prop.name,
                            "available": False,
                            "note": prepared.blocked,
                        },
                        metadata={"available": False, "reason": "not_ready"},
                    )

                profile = profile_property(
                    proj,
                    obj,
                    prop,
                    dsn=source.dsn_secret_ref,
                    mapping=prepared.mapping,
                    backend="doris",
                    top_n=top_n,
                    # 本体重新发布或换了数据源，同名字段的分布就不是同一回事，
                    # 必须落在不同的缓存键上。
                    scope_key=f"{ontology_id}|{source.id}",
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"字段画像失败：{str(exc)[:300]}")

        data = profile.to_dict()
        return ToolResult(
            success=True,
            data=data,
            metadata={
                "object": obj.name,
                "property": prop.name,
                "available": profile.available,
                "strategy": profile.strategy,
                "distinct_count": profile.distinct_count,
                "top_value_count": len(profile.top_values),
            },
        )
