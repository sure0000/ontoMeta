"""运维自省与监控工具（薄包装）。

数据逻辑在 `app.mcp.introspection`（与前端 REST 共用）；这里只把它包成 MCP 工具 +
当前会话身份/审计可达这类「当前调用上下文」的东西。

设计稿里的「锁账户/通知管理员/多租户」在 stdio 下无机制可依（用户自己就能重启进程），
故不做；只做可观测，把异常暴露出来。
"""

from __future__ import annotations

from app.mcp import introspection

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session


@register_tool
class ServerInfoTool:
    """服务器自省"""

    required_role = "reader"
    name = "server_info"
    description = (
        "回读本 MCP 服务器状态：版本、传输方式、工具清单与各自最低角色、当前会话身份、"
        "限流配置、审计表可达性。用于自查「我这条会话是什么权限、某工具为什么被拒」。"
    )
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        status = introspection.service_status()
        with session() as db:
            audit = introspection.audit_health(db)
        return ToolResult(
            success=True,
            data={
                **status,
                "identity": {
                    "role": auth.role,
                    "principal_id": auth.principal_id,
                    "principal_name": auth.principal_name,
                    "client_type": auth.client_type,
                    "authenticated": auth.is_authenticated,
                },
                "audit": audit,
            },
            metadata={"role": auth.role},
        )


@register_tool
class GetMcpStatsTool:
    """MCP 使用统计"""

    required_role = "publisher"
    name = "get_mcp_stats"
    description = (
        "基于审计表的 MCP 使用统计：总调用量、成功/失败/被拒/被限流数、按工具与角色分组。"
        "仅 publisher。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "window_minutes": {
                "type": "integer",
                "description": "只统计最近这么多分钟；留空统计全部",
                "minimum": 1,
                "maximum": 43200,
            },
            "top_tools": {
                "type": "integer",
                "description": "按调用量返回前 N 个工具",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        window_minutes = arguments.get("window_minutes")
        top_tools = as_int(arguments.get("top_tools"), 20, low=1, high=100)
        try:
            with session() as db:
                data = introspection.compute_stats(
                    db, window_minutes=window_minutes, top_tools=top_tools
                )
            return ToolResult(success=True, data=data, metadata={"total_calls": data["totals"]["calls"]})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"统计失败：{exc}")
