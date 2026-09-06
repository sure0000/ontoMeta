"""MCP result analysis parity and permission contract."""

from __future__ import annotations

import asyncio

from app.config import settings
from app.mcp.tools import TOOL_REGISTRY, AuthContext, ToolResult, tool_required_role
from app.mcp.tools.sql import ExecuteSqlTool


def test_analyze_query_is_registered_at_sql_role():
    assert "analyze_query" in TOOL_REGISTRY
    assert tool_required_role(TOOL_REGISTRY["analyze_query"]) == settings.agent_run_sql_min_role
    assert tool_required_role(TOOL_REGISTRY["analyze_query"]) == tool_required_role(
        TOOL_REGISTRY["execute_sql"]
    )


def test_analyze_query_reuses_execute_sql_and_returns_compact_analysis(monkeypatch):
    async def fake_execute(self, arguments, auth):
        return ToolResult(
            success=True,
            data={
                "columns": [{"key": "month"}, {"key": "amount"}],
                "rows": [
                    {"month": month, "amount": amount}
                    for month, amount in [
                        ("01", 10), ("02", 11), ("03", 9), ("04", 10),
                        ("05", 12), ("06", 10), ("07", 11), ("08", 200),
                    ]
                ],
                "row_count": 5,
                "truncated": False,
                "proved": {"tables": ["orders"], "columns": ["month", "amount"]},
            },
            metadata={"sql": "SELECT month, amount FROM orders"},
        )

    monkeypatch.setattr(ExecuteSqlTool, "execute", fake_execute)
    tool = TOOL_REGISTRY["analyze_query"]
    result = asyncio.run(
        tool.execute(
            {"sql": "SELECT month, amount FROM orders", "order_by": "month"},
            AuthContext(client_type="mcp_local", role="publisher"),
        )
    )

    assert result.success is True
    assert result.data["analysis"]["columns"][0]["trend"]["direction"] == "up"
    assert result.data["truncated"] is False
    assert result.metadata["analysis_scope"] == "returned_rows"


def test_analyze_query_preserves_sql_gate_error(monkeypatch):
    async def fake_execute(self, arguments, auth):
        return ToolResult(
            success=False,
            error="SQL 语义证明未通过",
            data={"rejected": True, "code": "unknown_table"},
        )

    monkeypatch.setattr(ExecuteSqlTool, "execute", fake_execute)
    result = asyncio.run(
        TOOL_REGISTRY["analyze_query"].execute(
            {"sql": "SELECT * FROM unknown"},
            AuthContext(client_type="mcp_local", role="publisher"),
        )
    )

    assert result.success is False
    assert result.error == "SQL 语义证明未通过"
    assert result.data["rejected"] is True
