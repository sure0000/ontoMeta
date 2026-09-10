"""图表规格 → Superset ``params`` / ``query_context`` 的确定性编译器。

**为什么不让 Agent 直接写 Superset 的 params。** Superset 没有按 ``viz_type`` 暴露参数
schema：``params`` 是一个 JSON 字符串，``query_context`` 是另一套结构，两者必须自洽，
社区里"用 API 建出来的图打开是空的 / metric 那一格是空的"基本都出在这儿。让模型去凑
一份自由字典，等于把一个没有契约的接口交给它猜。

所以这里收一个**受限的内部规格**（viz 白名单 + 维度/度量/过滤），一次性同源产出
``params`` 与 ``query_context``。这与仓库里"绑定 → 编译 SQL"是同一种做法：能编译出来的
才允许提交，编译不出来在这里就报错，而不是等 Superset 那边给一张打不开的图。

纯函数、无 IO，golden 测试钉住每种 viz 的产物。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 内部 viz 名 → Superset viz_type。白名单之外一律拒绝：Superset 有几十种图，
# 每多放一种就要多验一次它的 params 形状，没验过的等于没支持。
VIZ_TYPES: dict[str, str] = {
    "table": "table",
    "bar": "echarts_timeseries_bar",
    "line": "echarts_timeseries_line",
    "pie": "pie",
    "kpi": "big_number_total",
}

# 聚合函数白名单（Superset 的 SIMPLE 度量只认这几个）。
AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT")

# 过滤运算符：内部名 → Superset ``adhoc_filters`` 的 operator。
OPERATORS: dict[str, str] = {
    "eq": "==",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "like": "LIKE",
}

DEFAULT_TIME_RANGE = "No filter"
DEFAULT_ROW_LIMIT = 1000
MAX_ROW_LIMIT = 50_000


class ChartSpecError(ValueError):
    """规格不合法。消息直接面向调用方（Agent / 前端表单），要说清怎么改。"""


@dataclass(frozen=True)
class MetricSpec:
    """一个度量。``column`` 为空表示 ``COUNT(*)``。"""

    aggregate: str
    column: str | None = None
    label: str | None = None

    def resolved_label(self) -> str:
        if self.label:
            return self.label
        if self.aggregate == "COUNT" and not self.column:
            return "COUNT(*)"
        return f"{self.aggregate}({self.column})"


@dataclass(frozen=True)
class FilterSpec:
    column: str
    operator: str
    value: Any = None


@dataclass(frozen=True)
class ChartSpec:
    name: str
    viz: str
    dataset_id: int
    dimensions: list[str] = field(default_factory=list)
    metrics: list[MetricSpec] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    time_column: str | None = None
    time_range: str = DEFAULT_TIME_RANGE
    time_grain: str | None = None
    row_limit: int = DEFAULT_ROW_LIMIT
    sort_descending: bool = True


# --------------------------------------------------------------------- 校验


def validate_chart_spec(spec: ChartSpec) -> None:
    """规格自检。**在任何 IO 之前调用**——形状不对时不该先去登录 Superset 再失败。"""
    if spec.viz not in VIZ_TYPES:
        raise ChartSpecError(
            f"不支持的图表类型 {spec.viz!r}，可选：{'、'.join(sorted(VIZ_TYPES))}"
        )
    if not str(spec.name or "").strip():
        raise ChartSpecError("图表缺少名称")
    if not spec.dataset_id:
        raise ChartSpecError("缺少 dataset_id：先用 ensure_superset_dataset 登记数据集")
    if spec.row_limit <= 0 or spec.row_limit > MAX_ROW_LIMIT:
        raise ChartSpecError(f"row_limit 需在 1..{MAX_ROW_LIMIT} 之间")

    for metric in spec.metrics:
        if metric.aggregate not in AGGREGATES:
            raise ChartSpecError(
                f"不支持的聚合 {metric.aggregate!r}，可选：{'、'.join(AGGREGATES)}"
            )
        if metric.aggregate != "COUNT" and not metric.column:
            raise ChartSpecError(f"{metric.aggregate} 需要指定字段")
    for f in spec.filters:
        if f.operator not in OPERATORS:
            raise ChartSpecError(
                f"不支持的过滤运算符 {f.operator!r}，可选：{'、'.join(sorted(OPERATORS))}"
            )
        if f.operator in ("in", "not_in") and not isinstance(f.value, list | tuple):
            raise ChartSpecError(f"{f.operator} 的值应当是列表")

    # 各 viz 的形状约束在这里拦，不指望调用方自觉：形状不对的图 Superset 会收下，
    # 但打开是空白的——那种失败发生在另一个系统里，排查成本高得多。
    if spec.viz == "kpi":
        if len(spec.metrics) != 1:
            raise ChartSpecError("指标卡需要且只需要 1 个度量")
        if spec.dimensions:
            raise ChartSpecError("指标卡不能带维度（它只显示一个数）")
    elif spec.viz == "pie":
        if len(spec.dimensions) != 1:
            raise ChartSpecError("饼图需要且只需要 1 个维度")
        if len(spec.metrics) != 1:
            raise ChartSpecError("饼图需要且只需要 1 个度量")
    elif spec.viz in ("bar", "line"):
        if not spec.metrics:
            raise ChartSpecError(f"{spec.viz} 至少需要 1 个度量")
        if not spec.dimensions and not spec.time_column:
            raise ChartSpecError(
                f"{spec.viz} 需要一个 x 轴：给 dimensions 或 time_column 其中之一"
            )
    elif spec.viz == "table" and not spec.dimensions and not spec.metrics:
        raise ChartSpecError("表格至少需要一个维度或度量")


# ------------------------------------------------------------------- 编译


def _metric(metric: MetricSpec) -> dict[str, Any]:
    """Superset 的 adhoc 度量。``column`` 只放 ``column_name``——多余的字段会被
    Superset 忽略，但写错的字段名会让它当成保存过的度量去找，然后找不到。

    ``COUNT(*)`` 必须走 SQL 表达式，不能用 ``SIMPLE`` + ``column: null``：后者 Superset
    会拿它去构造 ``COUNT(<无名列>)``，取数时 500 报
    ``Cannot compile Column object until its 'name' is assigned``。
    这条最容易踩——「数一数有多少条」是最自然的度量，而图**建得出来**，只有打开时才失败。
    """
    if metric.aggregate == "COUNT" and not metric.column:
        return {
            "expressionType": "SQL",
            "sqlExpression": "COUNT(*)",
            "label": metric.resolved_label(),
            "optionName": "metric_count_star",
        }
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": metric.column},
        "aggregate": metric.aggregate,
        "label": metric.resolved_label(),
        "optionName": f"metric_{metric.aggregate.lower()}_{metric.column}",
    }


def _adhoc_filter(f: FilterSpec) -> dict[str, Any]:
    return {
        "expressionType": "SIMPLE",
        "subject": f.column,
        "operator": OPERATORS[f.operator],
        "comparator": list(f.value) if isinstance(f.value, list | tuple) else f.value,
        "clause": "WHERE",
        "filterOptionName": f"filter_{f.column}_{f.operator}",
    }


def _query_filter(f: FilterSpec) -> dict[str, Any]:
    """``query_context`` 用的是另一套过滤形状（col/op/val），不是 adhoc。"""
    return {"col": f.column, "op": OPERATORS[f.operator], "val": f.value}


def _x_axis(spec: ChartSpec) -> str | None:
    """bar/line 的 x 轴：有时间列优先用时间列，否则用第一个维度。"""
    if spec.time_column:
        return spec.time_column
    return spec.dimensions[0] if spec.dimensions else None


def _series_dimensions(spec: ChartSpec) -> list[str]:
    """除 x 轴之外的维度即分组序列。"""
    x = _x_axis(spec)
    return [d for d in spec.dimensions if d != x]


def build_params(spec: ChartSpec) -> dict[str, Any]:
    viz_type = VIZ_TYPES[spec.viz]
    metrics = [_metric(m) for m in spec.metrics]
    params: dict[str, Any] = {
        "viz_type": viz_type,
        "datasource": f"{spec.dataset_id}__table",
        "adhoc_filters": [_adhoc_filter(f) for f in spec.filters],
        "row_limit": spec.row_limit,
        "time_range": spec.time_range,
    }
    if spec.time_grain:
        params["time_grain_sqla"] = spec.time_grain

    if spec.viz == "kpi":
        params["metric"] = metrics[0]
    elif spec.viz == "pie":
        params.update({
            "groupby": list(spec.dimensions),
            "metric": metrics[0],
            "show_legend": True,
        })
    elif spec.viz in ("bar", "line"):
        params.update({
            "x_axis": _x_axis(spec),
            "groupby": _series_dimensions(spec),
            "metrics": metrics,
            "show_legend": True,
            "sort_series_ascending": not spec.sort_descending,
        })
    else:  # table
        params.update({
            "query_mode": "aggregate" if metrics else "raw",
            "groupby": list(spec.dimensions),
            "metrics": metrics,
            "all_columns": [] if metrics else list(spec.dimensions),
            "order_desc": spec.sort_descending,
        })
    return params


def build_query_context(spec: ChartSpec, params: dict[str, Any]) -> dict[str, Any]:
    """``query_context`` 必须与 ``params`` 同源。

    两者对不上正是"图建出来了、打开却没数"的成因：Superset 用 ``params`` 渲染控件、
    用 ``query_context`` 取数，各写一份迟早会分叉。故这里只从已经编译好的 ``params``
    里取，不重新解释一遍规格。
    """
    metrics = (
        [params["metric"]] if spec.viz in ("kpi", "pie") else params.get("metrics", [])
    )
    if spec.viz == "table" and not spec.metrics:
        columns: list[Any] = list(spec.dimensions)
        metrics = []
    elif spec.viz in ("bar", "line"):
        x = _x_axis(spec)
        columns = ([x] if x else []) + _series_dimensions(spec)
    else:
        columns = list(spec.dimensions)

    query: dict[str, Any] = {
        "filters": [_query_filter(f) for f in spec.filters],
        "extras": {"having": "", "where": ""},
        "applied_time_extras": {},
        "columns": columns,
        "metrics": metrics,
        "annotation_layers": [],
        "row_limit": spec.row_limit,
        "series_limit": 0,
        "order_desc": spec.sort_descending,
        "url_params": {},
        "custom_params": {},
        "custom_form_data": {},
    }
    if spec.time_grain:
        query["extras"]["time_grain_sqla"] = spec.time_grain
    if spec.time_column and spec.time_range != DEFAULT_TIME_RANGE:
        query["filters"].append(
            {"col": spec.time_column, "op": "TEMPORAL_RANGE", "val": spec.time_range}
        )
    return {
        "datasource": {"id": spec.dataset_id, "type": "table"},
        "force": False,
        "queries": [query],
        "form_data": params,
        "result_format": "json",
        "result_type": "full",
    }


def compile_chart(spec: ChartSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    """规格 → ``(params, query_context)``。不合法直接抛 ``ChartSpecError``。"""
    validate_chart_spec(spec)
    params = build_params(spec)
    return params, build_query_context(spec, params)


# --------------------------------------------------------- 看板布局（确定性）

# 栅格是 12 列；每行放 2 张图 → 每张 6 列宽。高度用 Superset 默认的 50（约 400px）。
DASHBOARD_COLUMNS = 12
PANEL_WIDTH = 6
PANEL_HEIGHT = 50


def build_position_json(chart_ids: list[int]) -> dict[str, Any]:
    """把图表排进看板栅格。

    **不能省。** 只把图关联到看板（chart 的 ``dashboards`` 字段）而不写进
    ``position_json``，Superset 会认下这个关联，但看板打开是空的——图在"未放置"状态。
    这是 API 建看板最容易踩的一脚，故布局在这里确定性生成并有 golden 测试。
    """
    position: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": ""}},
    }
    for index in range(0, len(chart_ids), 2):
        row_id = f"ROW-{index // 2}"
        row_children: list[str] = []
        for chart_id in chart_ids[index : index + 2]:
            chart_key = f"CHART-{chart_id}"
            row_children.append(chart_key)
            position[chart_key] = {
                "type": "CHART",
                "id": chart_key,
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "meta": {
                    "chartId": chart_id,
                    "width": PANEL_WIDTH,
                    "height": PANEL_HEIGHT,
                    "sliceName": "",
                },
            }
        position[row_id] = {
            "type": "ROW",
            "id": row_id,
            "children": row_children,
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        position["GRID_ID"]["children"].append(row_id)
    return position
