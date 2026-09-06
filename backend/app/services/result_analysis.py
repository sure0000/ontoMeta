"""Deterministic analysis of tabular query results.

The Data Agent used to own this calculation inside ``chat_bi.py``.  Keeping the
algorithm in a neutral service lets MCP expose the same result without making
the generic Agent ship all rows back into its context.
"""

from __future__ import annotations

import statistics
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _column_names(columns: list[Any] | None, rows: list[Any]) -> list[str]:
    names = []
    for column in columns or []:
        if isinstance(column, dict):
            value = column.get("key") or column.get("title")
        else:
            value = column
        text = str(value or "").strip()
        if text:
            names.append(text)
    if names:
        return list(dict.fromkeys(names))
    return list(dict.fromkeys(
        key for row in rows if isinstance(row, dict) for key in row
    ))


def analyze_rows(
    rows: list[Any],
    columns: list[Any] | None = None,
    *,
    selected_columns: list[str] | None = None,
    order_by: str | None = None,
    max_outliers: int = 5,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return numeric distribution, IQR outliers, and optional trend analysis.

    The returned statistics describe only ``rows`` supplied by the caller.  A
    caller that received a truncated query result must surface that fact in
    its own response rather than presenting these as full-table statistics.
    """
    rows = rows if isinstance(rows, list) else []
    if not rows:
        return None, "尚无查询结果，请先用 run_sql 取到数据再分析。"

    names = _column_names(columns, rows)
    wanted = {str(value).strip() for value in (selected_columns or []) if str(value).strip()}
    order_key = str(order_by or "").strip()
    if order_key and order_key not in names:
        order_key = ""
    max_outliers = max(1, min(int(max_outliers or 5), 20))
    dict_rows = [row for row in rows if isinstance(row, dict)]

    ordered_rows = dict_rows
    if order_key:
        def sort_key(row: dict) -> tuple[int, float | str]:
            value = row.get(order_key)
            numeric = _number(value)
            return (0, numeric) if numeric is not None else (1, str(value))

        ordered_rows = sorted(dict_rows, key=sort_key)

    def trend_and_jumps(pairs: list[tuple[Any, float]]) -> tuple[dict | None, list[dict]]:
        values = [value for _, value in pairs]
        count = len(values)
        if count < 4:
            return None, []
        xs = list(range(count))
        mean_x = statistics.fmean(xs)
        mean_y = statistics.fmean(values)
        denominator = sum((x - mean_x) ** 2 for x in xs)
        slope = (
            sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=False))
            / denominator
            if denominator
            else 0.0
        )
        first, last = values[0], values[-1]
        change = last - first
        span = max(values) - min(values)
        epsilon = (span or abs(mean_y) or 1.0) * 0.01
        direction = "up" if slope > epsilon else ("down" if slope < -epsilon else "flat")
        trend = {
            "direction": direction,
            "slope": round(slope, 4),
            "first": first,
            "last": last,
            "change": round(change, 4),
            "change_pct": round(change / abs(first) * 100, 2) if first else None,
        }

        deltas = [values[index] - values[index - 1] for index in range(1, count)]
        jumps: list[dict] = []
        if len(deltas) >= 3:
            threshold = max(epsilon, 3 * statistics.median(abs(delta) for delta in deltas))
            for index, delta in enumerate(deltas, start=1):
                if abs(delta) > threshold:
                    jumps.append(
                        {
                            "at": pairs[index][0],
                            "from": pairs[index - 1][1],
                            "to": pairs[index][1],
                            "delta": round(delta, 4),
                        }
                    )
        jumps.sort(key=lambda item: -abs(item["delta"]))
        return trend, jumps[:max_outliers]

    reports: list[dict[str, Any]] = []
    total_outliers = 0
    total_jumps = 0
    for name in names:
        if wanted and name not in wanted:
            continue
        if order_key and name == order_key:
            continue
        raw_values = [row.get(name) for row in dict_rows]
        values = [number for number in (_number(value) for value in raw_values) if number is not None]
        non_null_count = len([value for value in raw_values if value is not None])
        if not values or len(values) < max(1, non_null_count / 2):
            continue

        report: dict[str, Any] = {
            "column": name,
            "count": len(values),
            "nulls": len(raw_values) - len(values),
            "min": min(values),
            "max": max(values),
            "mean": round(statistics.fmean(values), 4),
        }
        if len(values) >= 2:
            quartiles = statistics.quantiles(values, n=4)
            p25, median, p75 = quartiles[0], quartiles[1], quartiles[2]
            iqr = p75 - p25
            report.update(
                {
                    "p25": round(p25, 4),
                    "median": round(median, 4),
                    "p75": round(p75, 4),
                    "std": round(statistics.stdev(values), 4),
                }
            )
            low, high = p25 - 1.5 * iqr, p75 + 1.5 * iqr
            outliers = [value for value in values if value < low or value > high]
            report["outlier_count"] = len(outliers)
            report["outliers"] = sorted(
                outliers, key=lambda value: -abs(value - report["mean"])
            )[:max_outliers]
            total_outliers += len(outliers)

        if order_key:
            pairs = [
                (row.get(order_key), _number(row.get(name)))
                for row in ordered_rows
            ]
            pairs = [(label, value) for label, value in pairs if value is not None]
            trend, jumps = trend_and_jumps(pairs)
            if trend:
                report["trend"] = trend
            if jumps:
                report["jumps"] = jumps
                total_jumps += len(jumps)
        reports.append(report)

    if not reports:
        return None, "结果里没有可分析的数值列。"
    analysis: dict[str, Any] = {
        "row_count": len(rows),
        "columns": reports,
        "total_outliers": total_outliers,
        "total_jumps": total_jumps,
    }
    if order_key:
        analysis["ordered_by"] = order_key
    return analysis, None


__all__ = ["analyze_rows"]
