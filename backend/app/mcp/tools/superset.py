"""Superset 可视化：把治理好的落点建成数据集，在 Superset 里出图、拼看板。

**ontoMeta 不做图表呈现**，Superset 才是画图与存图的地方；这里的工具只负责把
ontoMeta 这边已经确定的两件事推过去：数据集绑在哪个**已发布本体的落点**上、
图表的口径长什么样。

图表规格是**受限的**（见 ``services/superset_spec.py``）：Superset 没有按 viz_type
暴露参数 schema，让模型去凑一份自由字典，产出的多半是一张能建出来但打不开的图。
所以这里只收维度/度量/过滤，由 ontoMeta 编译成 Superset 的 params 与 query_context。
"""

from __future__ import annotations

from app.services import superset_service as svc
from app.services.superset_spec import (
    AGGREGATES,
    OPERATORS,
    VIZ_TYPES,
    ChartSpec,
    ChartSpecError,
    FilterSpec,
    MetricSpec,
    validate_chart_spec,
)

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_VIZ_HELP = (
    "table=明细/汇总表格，bar=柱状，line=折线，pie=饼图，kpi=单值指标卡"
)

#: Superset 的对象 id 上界。取 32 位有符号整数上限即可——这里只是不让一个
#: 手滑写成天文数字的入参把请求发出去，不是业务约束。
_MAX_ID = 2**31 - 1


def _as_id(value: object) -> int:
    """把入参读成 Superset 对象 id；读不出来回 0，由调用方给出可读的缺参错误。"""
    return as_int(value, 0, low=0, high=_MAX_ID)

_METRIC_SCHEMA = {
    "type": "array",
    "description": "度量。COUNT 可不带字段（即 COUNT(*)），其余聚合必须带字段。",
    "items": {
        "type": "object",
        "properties": {
            "aggregate": {"type": "string", "enum": list(AGGREGATES)},
            "column": {"type": "string", "description": "字段名（物理列名）"},
            "label": {"type": "string", "description": "图例/表头显示名，留空自动生成"},
        },
        "required": ["aggregate"],
    },
}

_FILTER_SCHEMA = {
    "type": "array",
    "description": "过滤条件（AND 关系）",
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "operator": {"type": "string", "enum": sorted(OPERATORS)},
            "value": {"description": "in/not_in 用数组，其余用标量"},
        },
        "required": ["column", "operator"],
    },
}

_CHART_PROPERTIES = {
    "name": {"type": "string", "description": "图表名称"},
    "viz": {"type": "string", "enum": sorted(VIZ_TYPES), "description": _VIZ_HELP},
    "dataset_id": {
        "type": "integer",
        "description": "Superset 数据集 id，来自 ensure_superset_dataset",
    },
    "dimensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "分组维度（物理列名）。kpi 不能有；pie 恰好一个。",
    },
    "metrics": _METRIC_SCHEMA,
    "filters": _FILTER_SCHEMA,
    "time_column": {"type": "string", "description": "时间字段；bar/line 优先作 x 轴"},
    "time_range": {
        "type": "string",
        "description": "Superset 时间范围表达式，如 'Last month'；默认不限",
    },
    "time_grain": {"type": "string", "description": "时间粒度，如 P1D / P1M"},
    "row_limit": {"type": "integer", "description": "返回行数上限，默认 1000"},
}


def _build_spec(arguments: dict, *, dataset_id: int) -> ChartSpec:
    """入参 → ChartSpec。形状是否合法交给编译器判，这里只做搬运。"""
    kwargs: dict = {
        "name": str(arguments.get("name") or "").strip(),
        "viz": str(arguments.get("viz") or "").strip(),
        "dataset_id": dataset_id,
        "dimensions": [str(d) for d in (arguments.get("dimensions") or [])],
        "metrics": [
            MetricSpec(
                aggregate=str(m.get("aggregate") or "").strip().upper(),
                column=(str(m["column"]).strip() if m.get("column") else None),
                label=(str(m["label"]).strip() if m.get("label") else None),
            )
            for m in (arguments.get("metrics") or [])
        ],
        "filters": [
            FilterSpec(
                column=str(f.get("column") or "").strip(),
                operator=str(f.get("operator") or "").strip().lower(),
                value=f.get("value"),
            )
            for f in (arguments.get("filters") or [])
        ],
        "time_column": str(arguments.get("time_column") or "").strip() or None,
        "time_grain": str(arguments.get("time_grain") or "").strip() or None,
    }
    if arguments.get("time_range"):
        kwargs["time_range"] = str(arguments["time_range"]).strip()
    if arguments.get("row_limit"):
        kwargs["row_limit"] = as_int(arguments.get("row_limit"), 1000, low=1, high=50_000)
    spec = ChartSpec(**kwargs)
    # 形状先自检：规格不对时不该先开会话、登录 Superset，再在那边失败。
    validate_chart_spec(spec)
    return spec


def _actor(auth: AuthContext) -> str | None:
    return auth.principal_name or auth.principal_id or auth.user_id


def _failed(exc: Exception) -> ToolResult:
    """把"没配好"与"调用失败"分开：前者要把人指回设置页，后者才是查网络。"""
    if isinstance(exc, svc.SupersetNotConfigured):
        return ToolResult(success=False, error=str(exc), metadata={"gate": "superset_not_configured"})
    if isinstance(exc, ChartSpecError | ValueError):
        return ToolResult(success=False, error=str(exc))
    return ToolResult(success=False, error=f"Superset 调用失败：{exc}")


@register_tool
class ListSupersetDatasetsTool:
    """列出 Superset 里的数据集"""

    name = "list_superset_datasets"
    required_role = "reader"
    description = (
        "列出 Superset 里已有的数据集，并标出哪些是由 ontoMeta 从落点登记过去的"
        "（带 dataset_ref 的那些口径可追溯到本体）。\n"
        "建图前先看这里有没有现成的；没有再用 `ensure_superset_dataset` 建。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "按表名过滤"},
            "limit": {
                "type": "integer",
                "description": "返回条数上限（默认 50）",
                "default": 50,
                "minimum": 1,
                "maximum": 200,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        keyword = str(arguments.get("q") or "").strip() or None
        limit = as_int(arguments.get("limit"), 50, low=1, high=200)
        try:
            with session() as db:
                cfg = svc.runtime(db)
                registered = {
                    row.superset_id: row.dataset_ref
                    for row in svc.list_assets(db, asset_type="dataset", limit=500)
                }
                with svc.client(cfg) as sc:
                    rows = sc.list_datasets(keyword, page_size=limit)
                items = [
                    {
                        "dataset_id": r.get("id"),
                        "table": r.get("table_name"),
                        "schema": r.get("schema"),
                        "dataset_ref": registered.get(r.get("id")),
                        "from_ontometa": r.get("id") in registered,
                    }
                    for r in rows
                ]
                return ToolResult(
                    success=True,
                    data={"items": items, "shown": len(items)},
                    metadata={"superset": cfg.public_base_url},
                )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


@register_tool
class EnsureSupersetDatasetTool:
    """把本体落点登记成 Superset 数据集"""

    name = "ensure_superset_dataset"
    required_role = "editor"
    description = (
        "把一个**已发布本体的落点**登记成 Superset 数据集（幂等：已存在就复用），"
        "并把本体的中文字段名与描述推成 Superset 的 verbose_name/description。\n"
        "入参是落点引用（形如 `obj:<对象id>@serving`），用 `list_datasets` 查得到。"
        "**不要在 Superset 里凭空建虚拟数据集绕开这一步**——那样图表口径就脱离治理了。\n"
        "返回可用的字段与已保存度量，直接拿去 `create_superset_chart`。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "dataset_ref": {
                "type": "string",
                "description": "落点引用，如 obj:1a2b@serving / logic:3c4d@ads",
            }
        },
        "required": ["dataset_ref"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        dataset_ref = str(arguments.get("dataset_ref") or "").strip()
        if not dataset_ref:
            return ToolResult(success=False, error="缺少 dataset_ref")
        try:
            with session() as db:
                cfg = svc.runtime(db)
                with svc.client(cfg) as sc:
                    result = svc.ensure_dataset(
                        db, cfg, sc, dataset_ref, created_by=_actor(auth)
                    )
                return ToolResult(
                    success=True,
                    data=result,
                    metadata={"created": result["created"]},
                )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


@register_tool
class CreateSupersetChartTool:
    """在 Superset 里建图表"""

    name = "create_superset_chart"
    required_role = "editor"
    description = (
        "在 Superset 里建一张图，返回 chart_id 与可直接打开的链接。\n"
        f"图表类型：{_VIZ_HELP}。\n"
        "维度/度量用的是**数据集里的物理列名**——先 `ensure_superset_dataset` 拿到列清单，"
        "不要凭对象属性名猜列名。\n"
        "形状不对（指标卡带维度、饼图两个度量、柱状图没有 x 轴）会被当场拒绝并说明原因，"
        "改了再提交即可。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            **_CHART_PROPERTIES,
            "dataset_ref": {
                "type": "string",
                "description": "这张图对应的落点引用，填上便于溯源",
            },
        },
        "required": ["name", "viz", "dataset_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        try:
            spec = _build_spec(arguments, dataset_id=_as_id(arguments.get("dataset_id")))
            with session() as db:
                cfg = svc.runtime(db)
                with svc.client(cfg) as sc:
                    result = svc.create_chart(
                        db,
                        cfg,
                        sc,
                        spec,
                        dataset_ref=str(arguments.get("dataset_ref") or "").strip() or None,
                        created_by=_actor(auth),
                    )
                return ToolResult(success=True, data=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


@register_tool
class UpdateSupersetChartTool:
    """改写已有图表的口径或形态"""

    name = "update_superset_chart"
    required_role = "editor"
    description = (
        "整体改写一张已有图表（换图形、换维度度量、改过滤）。\n"
        "**是整体替换不是增量**：没传的部分会被清掉，所以要把这张图完整的规格再给一遍。\n"
        "改的是同一张图，平台上的登记也就地更新，不会多出一条。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "chart_id": {"type": "integer", "description": "Superset 图表 id"},
            **_CHART_PROPERTIES,
        },
        "required": ["chart_id", "name", "viz", "dataset_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        chart_id = _as_id(arguments.get("chart_id"))
        if chart_id <= 0:
            return ToolResult(success=False, error="缺少 chart_id")
        try:
            spec = _build_spec(arguments, dataset_id=_as_id(arguments.get("dataset_id")))
            with session() as db:
                cfg = svc.runtime(db)
                with svc.client(cfg) as sc:
                    result = svc.update_chart(db, cfg, sc, chart_id, spec)
                return ToolResult(success=True, data=result)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


@register_tool
class CreateSupersetDashboardTool:
    """把图表拼成看板"""

    name = "create_superset_dashboard"
    required_role = "editor"
    description = (
        "把若干已建好的图表拼成一个 Superset 看板，返回链接与嵌入 uuid。\n"
        "图表按每行两张排布；顺序即 chart_ids 的顺序。\n"
        "拿不到嵌入 uuid 不影响看板可用（多半是 Superset 没开 EMBEDDED_SUPERSET），"
        "只是不能在 ontoMeta 里内嵌预览，回执里会说明。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "看板名称"},
            "chart_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要放进看板的图表 id（来自 create_superset_chart）",
            },
            "ontology_id": {"type": "string", "description": "归属本体，便于按域筛选"},
        },
        "required": ["title", "chart_ids"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        chart_ids = [_as_id(c) for c in (arguments.get("chart_ids") or [])]
        chart_ids = [c for c in chart_ids if c > 0]
        if not title:
            return ToolResult(success=False, error="缺少 title")
        try:
            with session() as db:
                cfg = svc.runtime(db)
                with svc.client(cfg) as sc:
                    result = svc.create_dashboard(
                        db,
                        cfg,
                        sc,
                        title,
                        chart_ids,
                        ontology_id=str(arguments.get("ontology_id") or "").strip() or None,
                        created_by=_actor(auth),
                    )
                return ToolResult(
                    success=True,
                    data=result,
                    metadata=(
                        {"embed_unavailable": result["embed_error"]}
                        if result.get("embed_error")
                        else {}
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


@register_tool
class ListSupersetAssetsTool:
    """列出 ontoMeta 建过的 Superset 资产"""

    name = "list_superset_assets"
    required_role = "reader"
    description = (
        "列出经 ontoMeta 建到 Superset 的数据集/图表/看板：叫什么、建在哪个落点上、"
        "谁建的、链接是什么。\n"
        "`state` 是**对账结果**不是猜测：`unknown` 表示还没对过账，`missing` 表示"
        "上次对账时 Superset 那边已经没有了。要刷新就把 reconcile 设为 true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "asset_type": {
                "type": "string",
                "enum": ["dataset", "chart", "dashboard"],
                "description": "只看某一类",
            },
            "ontology_id": {"type": "string", "description": "只看某个本体下的资产"},
            "q": {"type": "string", "description": "按名称过滤"},
            "reconcile": {
                "type": "boolean",
                "default": False,
                "description": "先与 Superset 对一次账再列（会多几次 API 调用）",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限（默认 50）",
                "default": 50,
                "minimum": 1,
                "maximum": 200,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        limit = as_int(arguments.get("limit"), 50, low=1, high=200)
        try:
            with session() as db:
                cfg = svc.runtime(db)
                counts = None
                if arguments.get("reconcile"):
                    with svc.client(cfg) as sc:
                        counts = svc.reconcile(db, cfg, sc)
                rows = svc.list_assets(
                    db,
                    asset_type=str(arguments.get("asset_type") or "").strip() or None,
                    ontology_id=str(arguments.get("ontology_id") or "").strip() or None,
                    keyword=str(arguments.get("q") or "").strip() or None,
                    limit=limit,
                )
                items = [svc.serialize_asset(r, cfg) for r in rows]
                return ToolResult(
                    success=True,
                    data={"items": items, "shown": len(items)},
                    metadata={
                        "superset": cfg.public_base_url,
                        **({"reconciled": counts} if counts else {}),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
