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
        "回读本 MCP 服务器状态：版本、传输方式、当前会话身份、限流配置、审计表可达性，"
        "以及**工具名 → 最低角色**的对照表。用于自查「我这条会话是什么权限、某工具为什么被拒」。\n"
        "默认不重复各工具的完整描述——那份你的工具清单里已经有了一份；"
        "确实要读全文时传 verbose=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "verbose": {
                "type": "boolean",
                "description": "连各工具的完整描述一起回（约 8KB，通常不需要）",
                "default": False,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        verbose = bool(arguments.get("verbose", False))
        status = introspection.service_status()
        with session() as db:
            audit = introspection.audit_health(db)
        tools = status.pop("tools", [])
        if verbose:
            status["tools"] = tools
        else:
            # 调用方的工具清单里已经有一份完整描述了；skill 又要求开局先调 server_info，
            # 于是同一份文本被付两次费（清单 15.7KB + 这里 8.1KB）。默认只回
            # 「哪个工具要什么角色」——这才是自查权限时真正要看的那一列。
            status["tool_roles"] = {
                item["name"]: item["required_role"] for item in tools
            }
            status["tools_note"] = (
                "工具描述已省略（你的工具清单里有完整版）；"
                "确需全文传 verbose=true。跨工具的调用顺序看 get_playbook。"
            )
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
            metadata={"role": auth.role, "verbose": verbose},
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
