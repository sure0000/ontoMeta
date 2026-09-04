"""血缘 / 落点 / 运行记录工具集（全部只读）。

这三个补的是**「已经发生过什么」**：MCP 此前能查本体、能提案、能推任务，唯独读不回
运行事实——2026-09-04 dsh 真机跑的三条 sync 全部失败，agent 手上只有一个 `run_url`，
自己查不出失败在哪一步。

三条各自的不可替代性：

- ``get_lineage``：某对象的上下游邻域。DataHub 表级血缘在 ingest 时就落成
  ``structure_type=derivation`` 的关系边，所以血缘不是另一套图，而是已发布关系图的
  一个视角——查它即可，不引第二份血缘存储。
- ``get_landing``：这个对象/口径落到哪张物理表了。**没有登记就是没落地**，
  绝不按 `ods_{域}_{表}` 之类的命名规则推一个表名出来——推出来的表往往压根不存在。
- ``get_ops_record``：按问题族读权威运行记录，薄壳套 ``services/ops_records.REGISTRY``
  的 reader，不在这里重写任何读模型。

**scope 与身份**：``ops_records`` 里 `decision` 族按会话组织、`task_run` 的
`scope=conversation` 也要会话 id——MCP 是无会话协议，这两条在本工具里明确拒绝并说明
原因，而不是塞一个假的会话 id 进去（那会读出别人的决策记录）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import func

from app.models import BusinessLogic, EntityStatus, ObjectType, RelationType
from app.services.logic_query import OntologyQueryService
from app.services.ops_records import (
    OPS_RECORD_ALLOWED_SCOPES,
    REGISTRY,
    default_ops_record_scope,
    read_landing,
)

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_query = OntologyQueryService()

# 这两族在 MCP 下无法成立：REGISTRY 里有它们，但 reader 要会话上下文。
# `landing` 也在 REGISTRY 里，但它需要先解析主体，那是 get_landing 的活。
_CONVERSATION_BOUND_FAMILIES = ("decision",)
_OPS_FAMILIES = tuple(
    key
    for key in REGISTRY
    if key not in _CONVERSATION_BOUND_FAMILIES and key != "landing"
)


def _node_id(raw: str) -> str:
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"n{digest}"


def _mermaid_label(value: Any) -> str:
    text = str(value or "未命名").replace("\n", " ").replace("\r", " ")
    return text.replace('"', "'")[:120]


def _lineage_mermaid(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, center_id: str, truncated: bool
) -> str:
    """邻域子图的 Mermaid。加工血缘（derivation）用粗箭头，与业务关系区分开。"""
    label_by_id = {
        str(node.get("id")): _mermaid_label(node.get("display_name") or node.get("label"))
        for node in nodes
    }
    lines = ["```mermaid", "flowchart LR"]
    for node_key, label in label_by_id.items():
        marker = ":::center" if node_key == center_id else ""
        lines.append(f'  {_node_id(node_key)}["{label}"]{marker}')
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        arrow = "==>" if edge.get("structure_type") == "derivation" else "-->"
        lines.append(
            f'  {_node_id(source)} {arrow}|"{_mermaid_label(edge.get("label"))}"| {_node_id(target)}'
        )
    if not edges:
        lines.append("  %% 中心对象在该跳数内没有任何关系边")
    if truncated:
        lines.append("  %% 邻域已截断；缩小 depth 或改用 query_relations 分页")
    lines.append("  classDef center stroke-width:3px")
    lines.append("```")
    return "\n".join(lines)


@register_tool
class GetLineageTool:
    """对象的血缘 / 上下游邻域子图"""

    name = "get_lineage"
    required_role = "reader"
    description = (
        "查某个业务对象的血缘与上下游邻域（中心对象 + depth 跳关系）。\n"
        "`structure_type=derivation` 的边是**数据加工血缘**（同步/清洗产生），"
        "其余是业务关系（外键/引用/包含/转化）——回答影响面时不要把两者混为一谈。\n"
        "结果按中心对象直接给出 `direct_upstream`（数据从哪来）与 `direct_downstream`"
        "（谁依赖它）；更远的跳数只在 nodes/edges 里。中心对象 id 用 query_objects 取。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "center_id": {
                "type": "string",
                "description": "中心业务对象 ID（来自 query_objects / query_object_detail）",
            },
            "depth": {
                "type": "integer",
                "description": "邻域跳数，默认 1，最多 3",
                "default": 1,
                "minimum": 1,
                "maximum": 3,
            },
            "include_mermaid": {
                "type": "boolean",
                "description": "附加当前邻域的 Mermaid 图（加工血缘用粗箭头）",
                "default": False,
            },
            "published_only": {
                "type": "boolean",
                "description": (
                    "只画已发布实体（默认 true）。数据加工血缘（derivation 边）常常是草稿状态——"
                    "已发布视图下为空时，metadata 会告诉你草稿里还有多少条，再决定要不要转 false"
                ),
                "default": True,
            },
        },
        "required": ["center_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        center_id = (arguments.get("center_id") or "").strip()
        if not center_id:
            return ToolResult(
                success=False,
                error="缺少 center_id",
                data={"hint": "先用 query_objects 定位对象拿到 id"},
            )
        depth = as_int(arguments.get("depth"), 1, low=1, high=3)
        published_only = bool(arguments.get("published_only", True))

        try:
            with session() as db:
                center = _query.get_object_type(db, center_id)
                if center is None:
                    return ToolResult(
                        success=False,
                        error=f"中心对象不存在：{center_id}",
                        data={"hint": "先用 query_objects 定位对象拿到 id"},
                    )
                if published_only and center.status != EntityStatus.PUBLISHED.value:
                    return ToolResult(
                        success=False,
                        error=f"中心对象未发布：{center_id}",
                        data={
                            "status": center.status,
                            "hint": "传 published_only=false 可以看草稿视图下的邻域",
                        },
                    )
                # 本体作用域从**中心对象自己**取，不从「当前锚定本体」猜。锚到别的本体
                # 会画出一张空图，并让调用方以为这个对象没有任何上下游。
                ontology_id = center.ontology_id
                if not ontology_id:
                    return ToolResult(
                        success=False,
                        error=f"对象 {center_id} 没有归属本体，无法取邻域",
                    )
                graph = _query.get_ontology_graph(
                    db,
                    ontology_id,
                    center_id=center_id,
                    depth=depth,
                    published_only=published_only,
                )
                # 已发布视图下一条加工血缘都没有时，必须分清两件事：**本体里真的没登记过
                # 血缘**，还是**登记了但没随发布晋级**（发布只提升业务对象，derivation 边
                # 常年停在草稿）。不分清，调用方就会把「图是空的」读成「这个对象没有上游」。
                # 这是给 note 用的计数，不是读模型——故不走 OntologyQueryService。
                hidden_derivations = 0
                if published_only:
                    hidden_derivations = (
                        db.query(func.count(RelationType.id))
                        .filter(
                            RelationType.ontology_id == ontology_id,
                            RelationType.structure_type == "derivation",
                            RelationType.status != EntityStatus.PUBLISHED.value,
                        )
                        .scalar()
                        or 0
                    )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询血缘失败：{exc}")

        payload = graph.model_dump(mode="json")
        nodes: list[dict[str, Any]] = payload.get("nodes") or []
        edges: list[dict[str, Any]] = payload.get("edges") or []
        label_by_id = {
            str(node.get("id")): node.get("display_name") or node.get("label")
            for node in nodes
        }

        def _neighbor(edge: dict[str, Any], *, other_key: str) -> dict[str, Any]:
            other = str(edge.get(other_key) or "")
            return {
                "object_id": other,
                "display_name": label_by_id.get(other),
                "relation": edge.get("label"),
                "structure_type": edge.get("structure_type"),
                # 血缘边和业务关系边混在同一张图里，这一位让调用方一眼分开
                "is_derivation": edge.get("structure_type") == "derivation",
            }

        upstream = [_neighbor(e, other_key="source") for e in edges if str(e.get("target")) == center_id]
        downstream = [_neighbor(e, other_key="target") for e in edges if str(e.get("source")) == center_id]
        derivation_count = sum(1 for e in edges if e.get("structure_type") == "derivation")

        data: dict[str, Any] = {
            "center_id": center_id,
            "center_name": center.display_name or center.name,
            "ontology_id": ontology_id,
            "depth": depth,
            "published_only": published_only,
            "direct_upstream": upstream,
            "direct_downstream": downstream,
            "nodes": nodes,
            "edges": edges,
        }
        if arguments.get("include_mermaid"):
            data["mermaid"] = _lineage_mermaid(
                nodes, edges, center_id=center_id, truncated=bool(payload.get("truncated"))
            )
        return ToolResult(
            success=True,
            data=data,
            metadata={
                "node_count": len(nodes),
                "edge_count": len(edges),
                "derivation_edge_count": derivation_count,
                "upstream_count": len(upstream),
                "downstream_count": len(downstream),
                "truncated": bool(payload.get("truncated")),
                "unpublished_derivation_edges": hidden_derivations,
                # 0 条 derivation 边时，调用方最容易把外键关系当成「数据从这里来」，
                # 或把空图读成「没有上游」。这句话把两种情形分开说清。
                "lineage_note": (
                    None
                    if derivation_count
                    else (
                        f"已发布视图里没有 derivation 边，但本体中还有 {hidden_derivations} 条"
                        "未发布的加工血缘（发布只提升业务对象，血缘边常停在草稿）。"
                        "要看它们请传 published_only=false；当前图上全是业务关系，不是数据来源。"
                        if hidden_derivations
                        else "该邻域没有 derivation 边：只有业务关系，没有登记过的数据加工血缘。"
                    )
                ),
            },
        )


@register_tool
class GetLandingTool:
    """对象 / 口径的物理落点"""

    name = "get_landing"
    required_role = "reader"
    description = (
        "读已发布业务对象或业务口径的**真实物理落点**：落到哪张表、表建了吗、数搬了吗、"
        "现在能不能查。\n"
        "只返回既有的同步契约 / 数仓投影登记；**没有登记就是没落地**，"
        "此时禁止按命名规则推测表名——推出来的表通常并不存在。\n"
        "target_id 来自 query_objects / search_logics；不知道 id 时给 keyword，"
        "命中不唯一会返回 candidates 让你选。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "target_kind": {
                "type": "string",
                "enum": ["object", "logic"],
                "description": "主体类型：object=业务对象，logic=业务口径",
            },
            "target_id": {"type": "string", "description": "对象或口径 ID"},
            "keyword": {
                "type": "string",
                "description": "主体显示名/标识符（无 id 时用它做候选定位）",
            },
            "ontology_id": {
                "type": "string",
                "description": "限定本体；留空则跨本体（keyword 定位时建议给上，避免同名歧义）",
            },
        },
        "required": ["target_kind"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        target_kind = (arguments.get("target_kind") or "").strip().lower()
        if target_kind not in {"object", "logic"}:
            return ToolResult(success=False, error="target_kind 必须是 object 或 logic")

        target_id = (arguments.get("target_id") or "").strip()
        keyword = (arguments.get("keyword") or "").strip()
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        if not target_id and not keyword:
            return ToolResult(
                success=False,
                error="需要 target_id，或给 keyword 做候选定位",
            )

        model = ObjectType if target_kind == "object" else BusinessLogic
        try:
            with session() as db:
                subject: str | None = None
                if target_id:
                    entity = db.get(model, target_id)
                    if entity is None or entity.status != EntityStatus.PUBLISHED.value:
                        return ToolResult(
                            success=False,
                            error="主体不存在或未发布",
                            data={"target_kind": target_kind, "target_id": target_id},
                        )
                    if ontology_id and entity.ontology_id != ontology_id:
                        return ToolResult(
                            success=False,
                            error="主体不属于指定本体",
                            data={
                                "target_id": target_id,
                                "ontology_id": ontology_id,
                                "actual_ontology_id": entity.ontology_id,
                            },
                        )
                    subject = entity.display_name or entity.name
                else:
                    if target_kind == "object":
                        page = _query.list_object_types(
                            db, ontology_id=ontology_id, published_only=True, q=keyword, limit=5
                        )
                    else:
                        page = _query.list_business_logics(
                            db, ontology_id=ontology_id, published_only=True, q=keyword, limit=5
                        )
                    # 带上数据域：keyword 默认跨本体，odoo 与 erpnext 各有一个「公司」，
                    # 只给 id 和显示名的话调用方分不出该选哪个。
                    candidates = [
                        {
                            "id": item.id,
                            "name": item.name,
                            "display_name": item.display_name,
                            "domain_name": getattr(item, "domain_name", None),
                            "domain_context_id": getattr(item, "domain_context_id", None),
                        }
                        for item in page.items
                    ]
                    if len(candidates) != 1:
                        # 主体不唯一时**不猜**：返回候选让调用方拿 id 再问一次。
                        return ToolResult(
                            success=True,
                            data={
                                "family": "landing",
                                "target_kind": target_kind,
                                "subject": keyword,
                                "facts": [],
                                "candidates": candidates,
                                "note": (
                                    "没有唯一主体，请从 candidates 里选 id 后重新调用 get_landing。"
                                    if candidates
                                    else "没有匹配的已发布主体。"
                                ),
                            },
                            metadata={
                                "resolved": False,
                                "candidate_count": len(candidates),
                                "total": page.total,
                            },
                        )
                    target_id = candidates[0]["id"]
                    subject = candidates[0]["display_name"] or candidates[0]["name"]

                answer = read_landing(
                    db,
                    {
                        "object_id": target_id if target_kind == "object" else "",
                        "logic_id": target_id if target_kind == "logic" else "",
                        "subject": subject,
                    },
                ).to_dict()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询落点失败：{exc}")

        answer["target_kind"] = target_kind
        answer["target_id"] = target_id
        facts = {fact.get("key"): fact.get("value") for fact in answer.get("facts") or []}
        return ToolResult(
            success=True,
            data=answer,
            metadata={
                "resolved": True,
                "target_kind": target_kind,
                "target_id": target_id,
                "state": facts.get("state"),
                "queryable": facts.get("queryable"),
            },
        )


@register_tool
class GetOpsRecordTool:
    """按问题族读运行记录"""

    name = "get_ops_record"
    required_role = "reader"
    description = (
        "读**已经发生过**的权威运行记录，只读，不创建也不执行任何任务。按 family 选族：\n"
        + "\n".join(f"- `{key}`（{REGISTRY[key].display}）：{REGISTRY[key].answers}" for key in _OPS_FAMILIES)
        + "\n物理落点问 get_landing。没有匹配记录时返回明确的空结果与 note，不要自己补。\n"
        "`as_of` 是记录自身的权威时点（上次成功搬数 / 执行完成），"
        "`observed_at` 是本次读取时刻——报告时别把后者当成前者。\n"
        "task_run 的失败原因只来自**投递回执自陈**：远端 Airflow/Flink 跑挂的任务，"
        "回执往往是「投递成功」而终态是 failed，此时这里给不出原因——"
        "改用 get_task_status 拿 run_url 去看远端日志，不要编一个原因。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "family": {
                "type": "string",
                "enum": list(_OPS_FAMILIES),
                "description": "运行记录问题族",
            },
            "ontology_id": {
                "type": "string",
                "description": "本体作用域；按本体组织的族（task_run/pipeline/draft_run 等）必填",
            },
            "scope": {
                "type": "string",
                "enum": ["ontology", "all", "global"],
                "description": "读取范围；留空取该族默认。MCP 无会话，不支持 conversation",
            },
            "artifact_id": {"type": "string", "description": "指定任务制品 id（task_run）"},
            "pipeline_id": {"type": "string", "description": "指定任务链 id（pipeline）"},
            "task_id": {"type": "string", "description": "指定草稿生成任务 id（draft_run）"},
            "app_id": {"type": "string", "description": "指定数据应用 id（data_app）"},
            "batch_id": {"type": "string", "description": "指定生产割接批次 id（migration）"},
            "component_key": {
                "type": "string",
                "description": "依赖组件 key，如 airflow/datahub/llm（component）",
            },
            "kind": {
                "type": "string",
                "description": "任务类型过滤：sync/transform/materialize/metric（task_run）",
            },
            "keyword": {"type": "string", "description": "按数据源/数据应用/组件名称过滤"},
            "version": {"type": "integer", "description": "指定本体发布版本（ontology_version）"},
            "limit": {
                "type": "integer",
                "description": "列表型结果条数上限（默认按族，通常 5）",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["family"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        family = (arguments.get("family") or "").strip()
        if family not in _OPS_FAMILIES:
            hint = None
            if family in _CONVERSATION_BOUND_FAMILIES:
                hint = (
                    f"`{family}` 族按会话组织，MCP 是无会话协议，读不到也不该读别人的会话记录。"
                )
            elif family == "landing":
                hint = "物理落点用 get_landing（它会先解析主体）。"
            return ToolResult(
                success=False,
                error=f"不支持的运行记录族：{family or '(空)'}",
                data={
                    "hint": hint,
                    "available_families": [
                        {
                            "key": key,
                            "display": REGISTRY[key].display,
                            "answers": REGISTRY[key].answers,
                        }
                        for key in _OPS_FAMILIES
                    ],
                },
            )

        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        scope = (arguments.get("scope") or "").strip() or default_ops_record_scope(family)
        if scope == "conversation":
            return ToolResult(
                success=False,
                error="MCP 无会话上下文，不支持 scope=conversation",
                data={"hint": "改用 scope=ontology（给 ontology_id）或 scope=all"},
            )
        allowed = OPS_RECORD_ALLOWED_SCOPES.get(
            family, frozenset({"ontology", "all", "global"})
        ) - {"conversation"}
        if scope not in allowed:
            return ToolResult(
                success=False,
                error=f"family={family} 的 scope 只能是 {'、'.join(sorted(allowed))}",
            )
        if scope == "ontology" and not ontology_id:
            return ToolResult(
                success=False,
                error=f"family={family} 需要 ontology_id",
                data={"hint": "先用 query_ontology 取真实本体 id"},
            )

        params = {
            key: value
            for key, value in arguments.items()
            if key not in {"family", "scope", "ontology_id"} and value not in (None, "")
        }
        params["scope"] = scope
        if ontology_id and scope not in {"all", "global"}:
            params["ontology_id"] = ontology_id

        try:
            with session() as db:
                answer = REGISTRY[family].reader(db, params).to_dict()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取运行记录失败：{str(exc)[:300]}")

        items = answer.get("items") or []
        facts = {fact.get("key"): fact.get("value") for fact in answer.get("facts") or []}
        metadata: dict[str, Any] = {
            "family": family,
            "display": REGISTRY[family].display,
            "scope": scope,
            "item_count": len(items),
            "fact_count": len(answer.get("facts") or []),
            "truncated": bool(answer.get("truncated")),
            "empty": not items and not answer.get("facts"),
        }
        if family == "task_run":
            # 失败了却没有 failure 字段 = 回执自陈「投递成功」、终态是 Airflow 对账回读的。
            # 沉默地少一个字段，调用方要么以为「失败但没原因就是没事」，要么自己编一个。
            unexplained = [
                row.get("artifact_id")
                for row in ([facts] if facts else []) + items
                if row.get("status") == "failed" and not row.get("failure")
            ]
            if unexplained:
                metadata["failed_without_reason"] = unexplained
                metadata["hint"] = (
                    "这些任务的失败发生在远端（Airflow/Flink），投递回执没有自陈原因。"
                    "用 get_task_status 取 run_url 去看远端日志，不要推测失败原因。"
                )
        return ToolResult(success=True, data=answer, metadata=metadata)
