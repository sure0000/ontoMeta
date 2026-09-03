"""MCP 自省/审计/统计的**共享数据层**。

MCP 工具（`server_info` / `get_mcp_stats` / `list_audit_logs`）和前端用的 REST 端点
（`/api/mcp/*`）都调这里的纯函数——聚合与目录逻辑只一处，两条出口不分叉。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, cast, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.mcp_audit import McpAuditLog

# 限流命中的审计以此前缀标记（见 server.handle_call_tool）。
RATE_LIMITED_PREFIX = "RATE_LIMITED:"


def tool_catalog() -> list[dict[str, Any]]:
    """全部已注册工具：名称、描述、最低角色。按名称排序。"""
    # 局部导入避免 import 期循环（tools 包在导入时注册，本模块被工具/REST 双向引用）。
    from .tools import TOOL_REGISTRY, tool_required_role

    return [
        {
            "name": t.name,
            "description": t.description,
            "required_role": tool_required_role(t),
        }
        for t in sorted(TOOL_REGISTRY.values(), key=lambda t: t.name)
    ]


def rate_limit_config() -> dict[str, Any]:
    return {
        "default_per_minute": settings.mcp_rate_limit_per_minute,
        "execute_sql_per_minute": (
            settings.mcp_execute_sql_rate_limit_per_minute
            or settings.mcp_rate_limit_per_minute
        ),
        "enabled": settings.mcp_rate_limit_per_minute > 0,
    }


def service_status() -> dict[str, Any]:
    """MCP 服务当前形态：传输、鉴权策略、限流、工具清单。不含运行期会话身份
    （那是 stdio 一进程一身份 / HTTP 逐请求的东西，与「服务配置」不是一回事）。"""
    tools = tool_catalog()
    return {
        "server": {"name": "ontometa", "version": "1.0.0"},
        "transports": {
            "stdio": True,  # 始终可用（客户端以子进程拉起）
            "http": {
                "enabled": settings.mcp_http_enabled,
                "path": "/mcp/",
                "allow_anonymous": settings.mcp_http_allow_anonymous,
            },
        },
        "default_role": (settings.mcp_default_role or "").strip() or None,
        "rate_limit": rate_limit_config(),
        "tool_count": len(tools),
        "tools": tools,
    }


def _audit_reachable(db: Session) -> tuple[bool, str | None]:
    try:
        db.query(func.count(McpAuditLog.id)).scalar()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def audit_health(db: Session) -> dict[str, Any]:
    reachable, error = _audit_reachable(db)
    return {"reachable": reachable, "error": error}


def compute_stats(
    db: Session, *, window_minutes: int | None = None, top_tools: int = 20
) -> dict[str, Any]:
    """审计聚合：总量、成功/业务失败/被拒/被限流、按工具与角色分组。"""
    since = None
    if window_minutes:
        since = datetime.now(timezone.utc) - timedelta(minutes=int(window_minutes))

    def _scope(q):
        return q.filter(McpAuditLog.created_at >= since) if since is not None else q

    total = _scope(db.query(McpAuditLog)).count()
    succeeded = _scope(db.query(McpAuditLog).filter(McpAuditLog.success.is_(True))).count()
    denied = _scope(db.query(McpAuditLog).filter(McpAuditLog.denied.is_(True))).count()
    rate_limited = _scope(
        db.query(McpAuditLog).filter(McpAuditLog.error.like(f"{RATE_LIMITED_PREFIX}%"))
    ).count()
    failed = _scope(db.query(McpAuditLog).filter(McpAuditLog.success.is_(False))).count()

    tool_q = _scope(
        db.query(
            McpAuditLog.tool_name,
            func.count(McpAuditLog.id).label("calls"),
            func.sum(cast(McpAuditLog.denied, Integer)).label("denied"),
        )
    ).group_by(McpAuditLog.tool_name).order_by(func.count(McpAuditLog.id).desc()).limit(top_tools)
    by_tool = [
        {"tool_name": r[0], "calls": int(r[1]), "denied": int(r[2] or 0)}
        for r in tool_q.all()
    ]

    role_q = _scope(
        db.query(McpAuditLog.principal_role, func.count(McpAuditLog.id))
    ).group_by(McpAuditLog.principal_role)
    by_role = [
        {"role": r[0] or "(anonymous)", "calls": int(r[1])} for r in role_q.all()
    ]

    return {
        "window_minutes": window_minutes,
        "totals": {
            "calls": total,
            "succeeded": succeeded,
            "business_failed": max(0, failed - denied - rate_limited),
            "denied": denied,
            "rate_limited": rate_limited,
        },
        "by_tool": by_tool,
        "by_role": by_role,
    }


def _row_to_dict(r: McpAuditLog) -> dict[str, Any]:
    from .tools._common import loads

    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "principal_id": r.principal_id,
        "principal_role": r.principal_role,
        "client_type": r.client_type,
        "tool_name": r.tool_name,
        "arguments": loads(r.arguments_json),
        "success": r.success,
        "denied": r.denied,
        "rate_limited": bool(r.error and r.error.startswith(RATE_LIMITED_PREFIX)),
        "error": r.error,
        "duration_ms": r.duration_ms,
    }


def query_audit(
    db: Session,
    *,
    tool_name: str | None = None,
    success: bool | None = None,
    denied_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """审计日志分页查询。返回 (rows, total)。arguments 已在写入时脱敏。"""
    q = db.query(McpAuditLog)
    if tool_name:
        q = q.filter(McpAuditLog.tool_name == tool_name)
    if success is not None:
        q = q.filter(McpAuditLog.success.is_(bool(success)))
    if denied_only:
        q = q.filter(McpAuditLog.denied.is_(True))
    total = q.count()
    rows = (
        q.order_by(McpAuditLog.created_at.desc())
        .offset(max(0, offset))
        .limit(limit)
        .all()
    )
    return [_row_to_dict(r) for r in rows], total
