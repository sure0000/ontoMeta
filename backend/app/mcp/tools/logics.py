"""业务口径（指标 / 标签 / 规则）工具集：查口径 → 读口径 → 让口径自己编译成 SQL。

口径是本体侧的**权威定义**。谁照着 ``expression_summary`` 那段文字重写一遍 SQL，
同一个「订单总额」就会算出与其它系统不一致的数——语义层就是在这一步丢掉的。所以
这里不止把表达式回给调用方自己翻译，而是给 ``compile_metric``：由
``services/metric_compiler`` 按 AST 确定性地编译出 SQL 与口径展开轨迹，模型的职责
收缩成「选哪个口径 + 按什么维度看」。

三个工具都只读，且都不连数据仓库——``compile_metric`` 只产 SQL，真要跑把它交给
``execute_sql``（那一步才按 ``agent_run_sql_min_role`` 收权）。

**详情必须做精简投影**：``BusinessLogicDetail`` 的 ``available_object_types`` /
``available_properties`` 是给编辑界面下拉框用的候选全集，在 erpnext 本体上分别是
约 1.5 MB / 5 MB，原样回给 MCP 客户端等于把会话打爆。
"""

from __future__ import annotations

from typing import Any

from app.services.logic_query import OntologyQueryService
from app.services.metric_compiler import MetricCompileError, compile_metric

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, dump, session

_query = OntologyQueryService()

# 编译目标恒为默认 Doris 数仓——与 execute_sql 同一个执行目标。两处方言一旦分叉，
# 编译出来的 SQL 就跑不动了。
_DIALECT = "doris"

_SUMMARY_KEYS = (
    "id",
    "name",
    "display_name",
    "logic_type",
    "status",
    "description",
    "expression_summary",
    "ontology_id",
    "domain_context_id",
    "domain_name",
    "category_name",
    "bound_object_count",
    "bound_property_count",
    "landing",
    "updated_at",
)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    """口径摘要 + 一个派生位 ``formalized``。

    「已形式化」的判据只取 ``expression_json``——``metric_compiler._load_ast`` 认的
    就是这一个字段，``propose_metric`` 要求的「已形式化」也是它。只有文字口径
    （formalized=false）的条目 compile/propose 都用不了，这一位让调用方在拉详情
    之前就知道，而不是撞一次 ``no_expression`` 才知道。
    """
    # 只投影**这一行真有**的键：``BusinessLogicOut``（列表读模型）没有 ontology_id，
    # ``BusinessLogicDetail`` 才有。统一 ``get`` 成 None 会让检索结果看起来「这条口径
    # 不属于任何本体」——派生值对不上、静默把判断带偏的正是这类坑。
    out = {key: row[key] for key in _SUMMARY_KEYS if key in row}
    out["formalized"] = bool(row.get("expression_json"))
    return out


def _object_ref(row: dict[str, Any]) -> dict[str, Any]:
    """口径关联对象的紧凑引用：够定位对象、够找到物理落点，不带判定证据。"""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "table_role": row.get("table_role"),
        "landing": row.get("landing"),
    }


def _property_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "data_type": row.get("data_type"),
        "semantic_type": row.get("semantic_type"),
    }


@register_tool
class SearchLogicsTool:
    """检索业务口径"""

    name = "search_logics"
    required_role = "reader"
    description = (
        "按关键词检索业务口径：指标（GMV/客单价）、标签（客户分层）、规则（金额必须为正）。"
        "关键词匹配标识名、显示名与描述；默认只看已发布口径。\n"
        "结果里的 formalized=false 表示该口径只有文字、尚未形式化，"
        "compile_metric 和 propose_metric 都用不了它。\n"
        "检索结果只带 domain_context_id/domain_name（列表读模型没有本体列）；"
        "propose_metric 要的 ontology_id 用 query_ontology 或 get_logic 取。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "关键词；留空则列出全部"},
            "ontology_id": {
                "type": "string",
                "description": "本体 ID；留空则跨本体检索",
            },
            "domain_context_id": {"type": "string", "description": "数据域 ID"},
            "logic_type": {
                "type": "string",
                "description": "按类型过滤：metric / tag / rule",
                "enum": ["metric", "tag", "rule"],
            },
            "published_only": {
                "type": "boolean",
                "description": "只看已发布口径（默认 true）",
                "default": True,
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限（默认 20）",
                "default": 20,
                "minimum": 1,
                "maximum": 200,
            },
            "offset": {"type": "integer", "description": "分页偏移", "default": 0},
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        search = (arguments.get("search") or "").strip() or None
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        domain_context_id = (arguments.get("domain_context_id") or "").strip() or None
        logic_type = (arguments.get("logic_type") or "").strip().lower() or None
        published_only = bool(arguments.get("published_only", True))
        limit = as_int(arguments.get("limit"), 20, low=1, high=200)
        offset = max(0, as_int(arguments.get("offset"), 0, low=0, high=1_000_000))

        try:
            with session() as db:
                page = _query.list_business_logics(
                    db,
                    ontology_id=ontology_id,
                    domain_context_id=domain_context_id,
                    published_only=published_only,
                    q=search,
                    limit=limit,
                    offset=offset,
                )
                logics = [_summary(dump(item)) for item in page.items]
                total = page.total
                # 服务层不按 logic_type 过滤，只能在这一页上筛。total 是**筛之前**的
                # 命中数，不能拿它当「该类型共有多少条」——所以另给 type_filtered，
                # 免得调用方把两个数当成一回事。
                type_filtered = False
                if logic_type:
                    logics = [item for item in logics if item.get("logic_type") == logic_type]
                    type_filtered = True
                return ToolResult(
                    success=True,
                    data={"logics": logics},
                    metadata={
                        "count": len(logics),
                        "total": total,
                        "truncated": total > offset + len(page.items),
                        "published_only": published_only,
                        "search": search,
                        "logic_type": logic_type,
                        "type_filtered_within_page": type_filtered,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"检索业务口径失败：{exc}")


@register_tool
class GetLogicTool:
    """查询单个业务口径详情"""

    name = "get_logic"
    required_role = "reader"
    description = (
        "查询单个业务口径的完整定义：表达式（文字口径 + 形式化 AST）、"
        "绑定的业务对象与字段、ADS 落点。"
        "要拿可执行 SQL 用 compile_metric，不要照着表达式自己重写。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "logic_id": {
                "type": "string",
                "description": "业务口径 ID（来自 search_logics 或 query_object_detail）",
            },
        },
        "required": ["logic_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = (arguments.get("logic_id") or "").strip()
        if not logic_id:
            return ToolResult(success=False, error="缺少 logic_id")

        try:
            with session() as db:
                detail = _query.get_business_logic(db, logic_id)
                if detail is None:
                    return ToolResult(success=False, error=f"业务口径不存在：{logic_id}")
                row = dump(detail)
                data = _summary(row)
                data["expression_draft"] = row.get("expression_draft")
                data["expression_json"] = row.get("expression_json")
                data["related_objects"] = [
                    _object_ref(item) for item in (row.get("related_object_types") or [])
                ]
                data["related_properties"] = [
                    _property_ref(item) for item in (row.get("related_properties") or [])
                ]
                return ToolResult(
                    success=True,
                    data=data,
                    metadata={
                        "logic_id": logic_id,
                        "logic_type": data.get("logic_type"),
                        "status": data.get("status"),
                        "formalized": data.get("formalized"),
                        "related_object_count": len(data["related_objects"]),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询业务口径失败：{exc}")


@register_tool
class CompileMetricTool:
    """把已发布口径编译成 SQL"""

    name = "compile_metric"
    # 只编译、不执行：产物是一段 SQL 文本，没有任何数据暴露。要跑它得再过
    # execute_sql 那道 agent_run_sql_min_role 闸门。
    required_role = "reader"
    description = (
        "把一条**已发布且已形式化**的口径按给定维度/过滤/时间粒度编译成 Doris SQL，"
        "并返回口径展开轨迹（caliber_trace）、JOIN 路径与语义证书。\n"
        "支持指标（→ 聚合查询）、标签（→ 各分桶取值分布）、规则（→ 统计违规行数）。\n"
        "**问已有指标/标签/规则一律用它，不要自己写 SQL**——口径以本体为准，"
        "自己重写会算出与其它系统不一致的数。产出的 sql 可直接交给 execute_sql。\n"
        "编译失败会给出 code 与 hint（口径尚未形式化、维度不可关联、会扇出等），"
        "照着修比换一段 SQL 有用。\n"
        "注意产出的表名是**本体标识名**：对象还没有物理落点时 execute_sql 会报 "
        "Unknown table——落点先看 get_logic 的 related_objects[].landing 或 "
        "query_object_detail，没落点就是这条口径还不能取数，不要改写 SQL 去凑。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "logic_id": {
                "type": "string",
                "description": "业务口径 ID（来自 search_logics；指标/标签/规则均可）",
            },
            "dimensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "拆分维度，写成 对象.字段（如 customer.region）或本对象字段名",
            },
            "filters": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "追加过滤，每项 {property, op, value|values}；"
                    "op ∈ =,!=,>,>=,<,<=,like,in。字面量要用库里真实存在的取值，不要凭空猜。"
                ),
            },
            "grain": {
                "type": "string",
                "description": "时间粒度：day/week/month/quarter/year",
                "enum": ["day", "week", "month", "quarter", "year"],
            },
            "time_property": {
                "type": "string",
                "description": "时间粒度作用的时间字段（有多个时间字段时必填）",
            },
        },
        "required": ["logic_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = (arguments.get("logic_id") or "").strip()
        if not logic_id:
            return ToolResult(success=False, error="缺少 logic_id")

        dimensions = [
            str(item).strip()
            for item in (arguments.get("dimensions") or [])
            if str(item).strip()
        ]
        filters = [item for item in (arguments.get("filters") or []) if isinstance(item, dict)]
        grain = (arguments.get("grain") or "").strip() or None
        time_property = (arguments.get("time_property") or "").strip() or None

        try:
            with session() as db:
                compiled = compile_metric(
                    db,
                    logic_id,
                    dimensions=dimensions,
                    filters=filters,
                    grain=grain,
                    time_property=time_property,
                    dialect=_DIALECT,
                )
        except MetricCompileError as exc:
            # 编译失败是**可修的业务结论**，不是工具故障：code + hint 原样回给调用方，
            # 它比一句「编译失败」更能指出下一步（与 propose_* 的缺参回灌同一取向）。
            return ToolResult(
                success=False,
                error=exc.message,
                data={
                    "compiled": False,
                    "logic_id": logic_id,
                    "code": exc.code,
                    "hint": exc.hint,
                },
                metadata={"logic_id": logic_id, "code": exc.code},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"口径编译失败：{str(exc)[:300]}")

        data = compiled.to_dict()
        data["compiled"] = True
        return ToolResult(
            success=True,
            data=data,
            metadata={
                "logic_id": compiled.logic_id,
                "logic_type": compiled.logic_type,
                "dialect": _DIALECT,
                "dimension_count": len(compiled.dimensions),
                "join_hop_count": len(compiled.join_hops),
                # 编译产物已过 prove_sql_sound 自证；扇出提示是「这个数可能被 JOIN 放大」，
                # 不是报错——但它必须一路传到答案里，不能在这里被吞掉。
                "fanout_note": data.get("fanout_note"),
                "warnings": data.get("warnings") or [],
            },
        )
