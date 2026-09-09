"""Deterministic analysis of a read-only warehouse query."""

from __future__ import annotations

from app.config import settings
from app.services.result_analysis import analyze_rows

from . import AuthContext, ToolResult, register_tool
from .sql import ExecuteSqlTool


@register_tool
class AnalyzeQueryTool:
    """Run a governed query and summarize its returned rows."""

    name = "analyze_query"
    display_name = "查询统计分析"
    category = "query"
    # Analysis reads real warehouse data just like execute_sql.  Keep the same
    # configured minimum so this cannot become a lower-privilege SQL side door.
    required_role = settings.agent_run_sql_min_role
    description = (
        "在默认 Doris 数仓执行一条受治理的只读查询，并对返回结果做确定性统计分析："
        "最小/最大/均值/中位数、IQR 离群值，以及指定排序列后的趋势和突变。\n"
        "它复用 execute_sql 的只读校验、语义证明、落点映射和就绪闸门；分析范围是本次"
        "实际返回的行，若 `truncated=true` 只能把结果说成样本，不能说成全表统计。"
        "需要已有业务口径时先用 compile_metric；自由 SQL 仍先核对 JOIN 和字面量。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "单条只读 SELECT/WITH 查询"},
            "limit": {
                "type": "integer",
                "description": "参与分析的返回行上限（默认与 execute_sql 相同）",
                "minimum": 1,
                "maximum": 1000,
            },
            "timeout": {
                "type": "integer",
                "description": "查询超时秒数",
                "minimum": 1,
                "maximum": 300,
            },
            "ontology_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "语义证明和落点映射的本体范围；留空＝全部已发布本体",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只分析这些结果列；留空分析所有数值列",
            },
            "order_by": {
                "type": "string",
                "description": "结果中的时间/序号列；给出后计算趋势和突变",
            },
            "max_outliers": {
                "type": "integer",
                "description": "每列最多返回的离群/突变样例数（默认 5）",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["sql"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        # Calling the existing tool keeps this operation on exactly the same
        # SQL safety path as execute_sql and avoids a second resolver.
        query_result = await ExecuteSqlTool().execute(arguments, auth)
        if not query_result.success:
            return query_result

        data = query_result.data if isinstance(query_result.data, dict) else {}
        raw_columns = data.get("columns") or []
        raw_rows = data.get("rows") or []
        selected = arguments.get("columns")
        selected_columns = (
            [str(value).strip() for value in selected if str(value).strip()]
            if isinstance(selected, list)
            else []
        )
        order_by = str(arguments.get("order_by") or "").strip()
        try:
            max_outliers = int(arguments.get("max_outliers") or 5)
        except (TypeError, ValueError):
            max_outliers = 5

        analysis, error = analyze_rows(
            raw_rows,
            raw_columns,
            selected_columns=selected_columns,
            order_by=order_by,
            max_outliers=max_outliers,
        )
        if error:
            return ToolResult(
                success=False,
                error=error,
                data={
                    "columns": raw_columns,
                    "row_count": data.get("row_count", len(raw_rows)),
                    "truncated": bool(data.get("truncated")),
                    "proved": data.get("proved") or {},
                },
                metadata={
                    **query_result.metadata,
                    "analysis_error": True,
                    "analysis_scope": "returned_rows",
                },
            )

        assert analysis is not None
        return ToolResult(
            success=True,
            data={
                "analysis": analysis,
                "row_count": data.get("row_count", len(raw_rows)),
                "truncated": bool(data.get("truncated")),
                "proved": data.get("proved") or {},
                "query_target": data.get("query_target"),
            },
            metadata={
                **query_result.metadata,
                "analysis_scope": "returned_rows",
                "sample_note": data.get("sample_note"),
            },
        )


__all__ = ["AnalyzeQueryTool"]
