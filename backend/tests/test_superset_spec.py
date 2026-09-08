"""图表规格编译器：形状约束 + params/query_context 同源 + 看板布局。

这些用例钉住的是三类"Superset 会收下、但打开是坏的"的产物：

1. **形状不对**（指标卡带维度、饼图两个度量、柱状图没有 x 轴）——Superset 不校验，
   建出来是一张空图，故在编译器这一侧就拒绝。
2. **params 与 query_context 分叉**——前者渲染控件、后者取数，各写一份就会出现
   "控件上看着对、图里没数"。
3. **看板 position_json 缺图**——只把图关联到看板而不放进布局，看板打开是空的。
"""

from __future__ import annotations

import pytest

from app.services.superset_spec import (
    ChartSpec,
    ChartSpecError,
    FilterSpec,
    MetricSpec,
    build_position_json,
    compile_chart,
)


def _spec(**overrides) -> ChartSpec:
    base = {
        "name": "各渠道订单量",
        "viz": "bar",
        "dataset_id": 12,
        "dimensions": ["channel"],
        "metrics": [MetricSpec(aggregate="SUM", column="amount")],
    }
    base.update(overrides)
    return ChartSpec(**base)


# ------------------------------------------------------------------ 形状约束


def test_unknown_viz_is_rejected_with_the_options_listed():
    with pytest.raises(ChartSpecError) as exc:
        compile_chart(_spec(viz="sankey"))
    assert "sankey" in str(exc.value)
    assert "bar" in str(exc.value)  # 报错要带上可选项，否则调用方只能猜


def test_kpi_refuses_dimensions():
    with pytest.raises(ChartSpecError, match="不能带维度"):
        compile_chart(_spec(viz="kpi", dimensions=["channel"]))


def test_kpi_needs_exactly_one_metric():
    with pytest.raises(ChartSpecError, match="1 个度量"):
        compile_chart(
            _spec(
                viz="kpi",
                dimensions=[],
                metrics=[
                    MetricSpec(aggregate="SUM", column="amount"),
                    MetricSpec(aggregate="COUNT"),
                ],
            )
        )


def test_pie_needs_exactly_one_dimension():
    with pytest.raises(ChartSpecError, match="1 个维度"):
        compile_chart(_spec(viz="pie", dimensions=["channel", "region"]))


def test_bar_without_an_x_axis_is_rejected():
    with pytest.raises(ChartSpecError, match="x 轴"):
        compile_chart(_spec(dimensions=[], time_column=None))


def test_sum_without_a_column_is_rejected():
    with pytest.raises(ChartSpecError, match="需要指定字段"):
        compile_chart(_spec(metrics=[MetricSpec(aggregate="SUM")]))


def test_count_star_needs_no_column():
    params, _ = compile_chart(_spec(metrics=[MetricSpec(aggregate="COUNT")]))
    assert params["metrics"][0]["label"] == "COUNT(*)"
    assert params["metrics"][0]["column"] is None


def test_in_operator_requires_a_list():
    with pytest.raises(ChartSpecError, match="列表"):
        compile_chart(_spec(filters=[FilterSpec(column="c", operator="in", value="x")]))


def test_missing_dataset_id_points_at_the_previous_step():
    with pytest.raises(ChartSpecError, match="ensure_superset_dataset"):
        compile_chart(_spec(dataset_id=0))


# --------------------------------------------------------------- golden 产物


def test_bar_params_are_stable():
    params, _ = compile_chart(
        _spec(
            dimensions=["channel"],
            time_column="order_date",
            metrics=[MetricSpec(aggregate="SUM", column="amount", label="金额")],
            filters=[FilterSpec(column="status", operator="eq", value="paid")],
            time_grain="P1M",
        )
    )
    assert params == {
        "viz_type": "echarts_timeseries_bar",
        "datasource": "12__table",
        "adhoc_filters": [
            {
                "expressionType": "SIMPLE",
                "subject": "status",
                "operator": "==",
                "comparator": "paid",
                "clause": "WHERE",
                "filterOptionName": "filter_status_eq",
            }
        ],
        "row_limit": 1000,
        "time_range": "No filter",
        "time_grain_sqla": "P1M",
        "x_axis": "order_date",
        "groupby": ["channel"],
        "metrics": [
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": "amount"},
                "aggregate": "SUM",
                "label": "金额",
                "optionName": "metric_sum_amount",
            }
        ],
        "show_legend": True,
        "sort_series_ascending": False,
    }


def test_time_column_becomes_the_x_axis_and_dimensions_become_series():
    params, ctx = compile_chart(
        _spec(viz="line", dimensions=["channel"], time_column="order_date")
    )
    assert params["x_axis"] == "order_date"
    assert params["groupby"] == ["channel"]
    assert ctx["queries"][0]["columns"] == ["order_date", "channel"]


def test_kpi_uses_the_singular_metric_key():
    """指标卡读的是 ``metric``（单数）；写成 ``metrics`` 那一格就是空的。"""
    params, ctx = compile_chart(
        _spec(viz="kpi", dimensions=[], metrics=[MetricSpec(aggregate="COUNT")])
    )
    assert params["viz_type"] == "big_number_total"
    assert "metrics" not in params
    assert params["metric"]["aggregate"] == "COUNT"
    assert ctx["queries"][0]["metrics"] == [params["metric"]]


def test_table_without_metrics_is_a_raw_listing():
    params, ctx = compile_chart(
        _spec(viz="table", dimensions=["channel", "region"], metrics=[])
    )
    assert params["query_mode"] == "raw"
    assert params["all_columns"] == ["channel", "region"]
    assert ctx["queries"][0]["metrics"] == []
    assert ctx["queries"][0]["columns"] == ["channel", "region"]


# ------------------------------------------------- params 与 query_context 同源


@pytest.mark.parametrize(
    "spec",
    [
        _spec(viz="bar"),
        _spec(viz="line"),
        _spec(viz="pie"),
        _spec(viz="kpi", dimensions=[]),
        _spec(viz="table"),
    ],
    ids=["bar", "line", "pie", "kpi", "table"],
)
def test_query_context_form_data_is_the_same_object_as_params(spec):
    """``form_data`` 就是 ``params``：一份规格只能编译出一套口径。"""
    params, ctx = compile_chart(spec)
    assert ctx["form_data"] == params
    assert ctx["datasource"] == {"id": spec.dataset_id, "type": "table"}
    assert ctx["queries"][0]["row_limit"] == params["row_limit"]


def test_time_range_lands_in_the_query_as_a_temporal_filter():
    _, ctx = compile_chart(
        _spec(time_column="order_date", time_range="Last month")
    )
    temporal = [f for f in ctx["queries"][0]["filters"] if f["op"] == "TEMPORAL_RANGE"]
    assert temporal == [
        {"col": "order_date", "op": "TEMPORAL_RANGE", "val": "Last month"}
    ]


def test_default_time_range_adds_no_filter():
    _, ctx = compile_chart(_spec(time_column="order_date"))
    assert all(f["op"] != "TEMPORAL_RANGE" for f in ctx["queries"][0]["filters"])


# ------------------------------------------------------------------ 看板布局


def test_every_chart_lands_in_the_grid():
    """关联而不放置 = 看板打开是空的。每张图都必须有自己的 CHART 节点并挂在某一行下。"""
    position = build_position_json([7, 8, 9])
    charts = {k: v for k, v in position.items() if k.startswith("CHART-")}
    assert sorted(charts) == ["CHART-7", "CHART-8", "CHART-9"]

    rows = position["GRID_ID"]["children"]
    assert rows == ["ROW-0", "ROW-1"]  # 每行两张
    placed = [c for row in rows for c in position[row]["children"]]
    assert placed == ["CHART-7", "CHART-8", "CHART-9"]
    for key, node in charts.items():
        assert node["meta"]["chartId"] == int(key.removeprefix("CHART-"))
        assert node["parents"][:2] == ["ROOT_ID", "GRID_ID"]


def test_empty_dashboard_still_has_a_valid_skeleton():
    position = build_position_json([])
    assert position["GRID_ID"]["children"] == []
    assert position["ROOT_ID"]["children"] == ["GRID_ID"]
