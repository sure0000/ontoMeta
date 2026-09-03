"""运维自省与监控工具。

Phase 4：把「这台 MCP 服务器现在什么状态、被怎么用了」做成可回读的东西。两件：

- ``server_info``（reader）：服务器版本、工具清单与各自最低角色、**当前会话身份**、限流
  配置、审计表可达性。运维/调试自查——「我这条会话是什么权限、为什么某工具被拒」一眼看清。
- ``get_mcp_stats``（publisher）：基于审计表的使用统计（总量、成功/失败/被拒/被限流、
  按工具与角色分组、最近窗口的可疑信号）。设计稿里的「锁账户/通知管理员」在 stdio 下
  无机制可依（用户自己就能重启进程），故不做；这里只做**可观测**，把异常暴露出来。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, cast, func

from app.config import settings
from app.models.mcp_audit import McpAuditLog

from . import (
    AuthContext,
    ToolResult,
    TOOL_REGISTRY,
    register_tool,
    tool_required_role,
)
from ._common import as_int, session

# 限流命中的审计以此前缀标记（见 server.handle_call_tool）。
_RATE_LIMITED_PREFIX = "RATE_LIMITED:"


@register_tool
class ServerInfoTool:
    """服务器自省"""

    required_role = "reader"
    name = "server_info"
    description = (
        "回读本 MCP 服务器状态：版本、工具清单与各自最低角色、当前会话身份、限流配置、"
        "审计表是否可达。用于自查「我这条会话是什么权限、某工具为什么被拒」。"
    )
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        # 延迟导入 server 常量，避免 tools ↔ server 的模块级循环导入。
        from app.mcp.server import SERVER_NAME, SERVER_VERSION

        tools = [
            {"name": t.name, "required_role": tool_required_role(t)}
            for t in sorted(TOOL_REGISTRY.values(), key=lambda t: t.name)
        ]

        # 审计表可达性：读一次 count，失败即视为不可达（不抛，作为状态回报）。
        audit_reachable = True
        audit_error = None
        try:
            with session() as db:
                db.query(func.count(McpAuditLog.id)).scalar()
        except Exception as exc:  # noqa: BLE001
            audit_reachable = False
            audit_error = str(exc)[:200]

        return ToolResult(
            success=True,
            data={
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "tool_count": len(tools),
                "tools": tools,
                "identity": {
                    "role": auth.role,
                    "principal_id": auth.principal_id,
                    "principal_name": auth.principal_name,
                    "client_type": auth.client_type,
                    "authenticated": auth.is_authenticated,
                },
                "rate_limit": {
                    "default_per_minute": settings.mcp_rate_limit_per_minute,
                    "execute_sql_per_minute": (
                        settings.mcp_execute_sql_rate_limit_per_minute
                        or settings.mcp_rate_limit_per_minute
                    ),
                    "enabled": settings.mcp_rate_limit_per_minute > 0,
                },
                "audit": {"reachable": audit_reachable, "error": audit_error},
            },
            metadata={"role": auth.role},
        )


@register_tool
class GetMcpStatsTool:
    """MCP 使用统计"""

    required_role = "publisher"
    name = "get_mcp_stats"
    description = (
        "基于审计表的 MCP 使用统计：总调用量、成功/失败/被拒/被限流数、按工具与角色分组、"
        "最近窗口内的被拒次数（异常信号）。仅 publisher。"
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
                base = db.query(McpAuditLog)
                since = None
                if window_minutes:
                    since = datetime.now(timezone.utc) - timedelta(
                        minutes=int(window_minutes)
                    )
                    base = base.filter(McpAuditLog.created_at >= since)

                total = base.count()
                succeeded = base.filter(McpAuditLog.success.is_(True)).count()
                denied = base.filter(McpAuditLog.denied.is_(True)).count()
                rate_limited = base.filter(
                    McpAuditLog.error.like(f"{_RATE_LIMITED_PREFIX}%")
                ).count()
                # 失败但非「被拒」「被限流」= 业务失败。
                failed = base.filter(McpAuditLog.success.is_(False)).count()
                business_failed = failed - denied - rate_limited

                # 按工具分组（调用量倒序）。
                tool_q = (
                    db.query(
                        McpAuditLog.tool_name,
                        func.count(McpAuditLog.id).label("calls"),
                        func.sum(cast(McpAuditLog.denied, Integer)).label("denied"),
                    )
                    .group_by(McpAuditLog.tool_name)
                    .order_by(func.count(McpAuditLog.id).desc())
                    .limit(top_tools)
                )
                if since is not None:
                    tool_q = tool_q.filter(McpAuditLog.created_at >= since)
                by_tool = [
                    {"tool_name": r[0], "calls": int(r[1]), "denied": int(r[2] or 0)}
                    for r in tool_q.all()
                ]

                # 按身份角色分组。
                role_q = db.query(
                    McpAuditLog.principal_role, func.count(McpAuditLog.id)
                ).group_by(McpAuditLog.principal_role)
                if since is not None:
                    role_q = role_q.filter(McpAuditLog.created_at >= since)
                by_role = [
                    {"role": r[0] or "(anonymous)", "calls": int(r[1])}
                    for r in role_q.all()
                ]

                return ToolResult(
                    success=True,
                    data={
                        "window_minutes": window_minutes,
                        "totals": {
                            "calls": total,
                            "succeeded": succeeded,
                            "business_failed": max(0, business_failed),
                            "denied": denied,
                            "rate_limited": rate_limited,
                        },
                        "by_tool": by_tool,
                        "by_role": by_role,
                    },
                    metadata={"total_calls": total},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"统计失败：{exc}")
