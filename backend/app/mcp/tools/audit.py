"""审计查询工具（薄包装）。

审计若不可读，「全量记录」就只是写进黑洞。开一个 publisher 门控的回读入口，让管理员
能在同一通道里核对「谁用什么身份调了什么」。查询逻辑在 `app.mcp.introspection`
（与前端 REST 共用）。它自己也会被审计。
"""

from __future__ import annotations

from app.mcp import introspection

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session


@register_tool
class ListAuditLogsTool:
    """回读 MCP 工具调用审计"""

    # 审计含每一次调用的主体与入参，只有 publisher 能看。
    required_role = "publisher"
    name = "list_audit_logs"
    description = (
        "回读 MCP 工具调用审计日志（谁、什么身份、调了哪个工具、成没成、是否被授权拦下）。"
        "按时间倒序，可按工具名、是否成功、是否被拒过滤。仅 publisher。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "按工具名过滤"},
            "success": {
                "type": "boolean",
                "description": "只看成功（true）/ 失败（false）；留空不过滤",
            },
            "denied_only": {
                "type": "boolean",
                "description": "只看被授权拦下的调用（403 类安全事件）",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限",
                "default": 50,
                "minimum": 1,
                "maximum": 500,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        tool_name = (arguments.get("tool_name") or "").strip() or None
        success = arguments.get("success")
        denied_only = bool(arguments.get("denied_only", False))
        limit = as_int(arguments.get("limit"), 50, low=1, high=500)

        try:
            with session() as db:
                logs, total = introspection.query_audit(
                    db,
                    tool_name=tool_name,
                    success=None if success is None else bool(success),
                    denied_only=denied_only,
                    limit=limit,
                )
            return ToolResult(
                success=True,
                data={"logs": logs},
                metadata={"count": len(logs), "total": total, "truncated": total > limit},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询审计失败：{exc}")
