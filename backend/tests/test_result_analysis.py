"""Shared result analysis used by both Data Agent and MCP."""

from app.services.result_analysis import analyze_rows


def test_analyze_rows_reports_distribution_outliers_and_trend():
    columns = [{"key": "month"}, {"key": "amount"}, {"key": "region"}]
    rows = [
        {"month": month, "amount": amount, "region": "华东"}
        for month, amount in [
            ("01", 10), ("02", 11), ("03", 9), ("04", 10),
            ("05", 12), ("06", 10), ("07", 11), ("08", 200),
        ]
    ]

    analysis, error = analyze_rows(rows, columns, order_by="month")

    assert error is None
    assert analysis is not None
    report = analysis["columns"][0]
    assert report["column"] == "amount"
    assert report["min"] == 9.0
    assert report["max"] == 200.0
    assert report["trend"]["direction"] == "up"
    assert report["jumps"][0]["at"] == "08"
    assert report["outlier_count"] >= 1


def test_analyze_rows_only_selected_numeric_columns():
    rows = [{"a": 1, "b": "not numeric"}, {"a": 3, "b": "still not numeric"}]

    analysis, error = analyze_rows(rows, selected_columns=["b"])

    assert analysis is None
    assert error == "结果里没有可分析的数值列。"


def test_analyze_rows_empty_result_is_explicit():
    analysis, error = analyze_rows([])

    assert analysis is None
    assert error == "尚无查询结果，请先用 run_sql 取到数据再分析。"
