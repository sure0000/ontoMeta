"""MCP 自省/审计/统计的**共享数据层**。

MCP 工具（`server_info` / `get_mcp_stats` / `list_audit_logs`）和前端用的 REST 端点
（`/api/mcp/*`）都调这里的纯函数——聚合与目录逻辑只一处，两条出口不分叉。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, case, cast, func, or_
from sqlalchemy.orm import Session

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


def _runtime(db=None):
    if db is None:
        from app.database import SessionLocal
        from app.services.settings_service import SettingsService
        with SessionLocal() as session:
            return SettingsService().get_mcp_runtime(session)
    from app.services.settings_service import SettingsService
    return SettingsService().get_mcp_runtime(db)


def rate_limit_config(runtime=None) -> dict[str, Any]:
    runtime = runtime or _runtime()
    return {
        "default_per_minute": runtime.mcp_rate_limit_per_minute,
        "execute_sql_per_minute": (
            runtime.mcp_execute_sql_rate_limit_per_minute
            or runtime.mcp_rate_limit_per_minute
        ),
        "enabled": runtime.mcp_rate_limit_per_minute > 0,
    }


def service_status(db=None) -> dict[str, Any]:
    """MCP 服务当前形态：传输、鉴权策略、限流、工具清单。不含运行期会话身份
    （那是 stdio 一进程一身份 / HTTP 逐请求的东西，与「服务配置」不是一回事）。"""
    tools = tool_catalog()
    runtime = _runtime(db)
    return {
        "server": {"name": "ontometa", "version": "1.0.0"},
        "transports": {
            "http": {
                "enabled": True,
                "path": "/mcp/",
                "allow_anonymous": False,
            },
        },
        "default_role": (runtime.mcp_default_role or "").strip() or None,
        "rate_limit": rate_limit_config(runtime),
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
    """审计聚合：总量、延迟、趋势、错误聚合和工具/角色分组。"""
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

    durations = [
        int(value[0])
        for value in _scope(
            db.query(McpAuditLog.duration_ms).filter(McpAuditLog.duration_ms.isnot(None))
        ).all()
        if value[0] is not None
    ]
    durations.sort()
    average_duration = round(sum(durations) / len(durations), 1) if durations else None
    p95_duration = durations[max(0, int(len(durations) * 0.95) - 1)] if durations else None

    tool_q = _scope(
        db.query(
            McpAuditLog.tool_name,
            func.count(McpAuditLog.id).label("calls"),
            func.sum(cast(McpAuditLog.success, Integer)).label("succeeded"),
            func.sum(cast(McpAuditLog.denied, Integer)).label("denied"),
            func.sum(
                case((McpAuditLog.error.like(f"{RATE_LIMITED_PREFIX}%"), 1), else_=0)
            ).label("rate_limited"),
            func.avg(McpAuditLog.duration_ms).label("avg_duration_ms"),
        )
    ).group_by(McpAuditLog.tool_name).order_by(func.count(McpAuditLog.id).desc()).limit(top_tools)
    by_tool = [
        {
            "tool_name": r[0],
            "calls": int(r[1]),
            "succeeded": int(r[2] or 0),
            "denied": int(r[3] or 0),
            "rate_limited": int(r[4] or 0),
            "failed": max(0, int(r[1]) - int(r[2] or 0) - int(r[3] or 0) - int(r[4] or 0)),
            "avg_duration_ms": round(float(r[5]), 1) if r[5] is not None else None,
        }
        for r in tool_q.all()
    ]

    role_q = _scope(
        db.query(
            McpAuditLog.principal_role,
            func.count(McpAuditLog.id),
            func.sum(cast(McpAuditLog.success, Integer)),
            func.sum(cast(McpAuditLog.denied, Integer)),
        )
    ).group_by(McpAuditLog.principal_role)
    by_role = [
        {
            "role": r[0] or "(anonymous)",
            "calls": int(r[1]),
            "succeeded": int(r[2] or 0),
            "denied": int(r[3] or 0),
            "failed": max(0, int(r[1]) - int(r[2] or 0)),
        }
        for r in role_q.all()
    ]

    errors_q = _scope(
        db.query(
            McpAuditLog.tool_name,
            McpAuditLog.error,
            func.count(McpAuditLog.id).label("count"),
            func.max(McpAuditLog.created_at).label("last_at"),
        )
        .filter(McpAuditLog.success.is_(False))
        .filter(McpAuditLog.denied.is_(False))
        .filter(~McpAuditLog.error.like(f"{RATE_LIMITED_PREFIX}%"))
        .filter(McpAuditLog.error.isnot(None))
        .group_by(McpAuditLog.tool_name, McpAuditLog.error)
        .order_by(func.count(McpAuditLog.id).desc())
    ).limit(20)
    error_groups = [
        {
            "tool_name": r[0],
            "error": str(r[1])[:240],
            "count": int(r[2]),
            "last_at": r[3].isoformat() if r[3] else None,
        }
        for r in errors_q.all()
    ]

    # Bucket in Python so this remains portable across SQLite and PostgreSQL.
    trend_rows = _scope(
        db.query(
            McpAuditLog.created_at,
            McpAuditLog.success,
            McpAuditLog.denied,
            McpAuditLog.error,
        )
    ).all()
    if window_minutes is None or window_minutes <= 60:
        bucket_seconds = 15 * 60
    elif window_minutes <= 24 * 60:
        bucket_seconds = 60 * 60
    elif window_minutes <= 7 * 24 * 60:
        bucket_seconds = 6 * 60 * 60
    else:
        bucket_seconds = 24 * 60 * 60
    buckets: dict[int, dict[str, Any]] = {}
    for created_at, success, denied_flag, error in trend_rows:
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        timestamp = created_at.timestamp()
        bucket = int(timestamp // bucket_seconds) * bucket_seconds
        item = buckets.setdefault(
            bucket,
            {"bucket": datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat(), "calls": 0, "succeeded": 0, "failed": 0, "denied": 0, "rate_limited": 0},
        )
        item["calls"] += 1
        if success:
            item["succeeded"] += 1
        elif denied_flag:
            item["denied"] += 1
        elif error and str(error).startswith(RATE_LIMITED_PREFIX):
            item["rate_limited"] += 1
        else:
            item["failed"] += 1
    timeline = [buckets[key] for key in sorted(buckets)]

    unique_principals = _scope(
        db.query(McpAuditLog.principal_id)
        .filter(McpAuditLog.principal_id.isnot(None))
        .distinct()
    ).count()
    last_call = _scope(db.query(func.max(McpAuditLog.created_at))).scalar()

    return {
        "window_minutes": window_minutes,
        "totals": {
            "calls": total,
            "succeeded": succeeded,
            "business_failed": max(0, failed - denied - rate_limited),
            "denied": denied,
            "rate_limited": rate_limited,
            "error_rate": round((max(0, failed - denied - rate_limited) / total) * 100, 1)
            if total
            else 0.0,
            "average_duration_ms": average_duration,
            "p95_duration_ms": p95_duration,
        },
        "by_tool": by_tool,
        "by_role": by_role,
        "error_groups": error_groups,
        "timeline": timeline,
        "unique_principals": unique_principals,
        "last_call_at": last_call.isoformat() if last_call else None,
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
    principal_id: str | None = None,
    principal_role: str | None = None,
    result: str | None = None,
    window_minutes: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """审计日志分页查询。返回 (rows, total)。arguments 已在写入时脱敏。"""
    q = db.query(McpAuditLog)
    if window_minutes:
        since = datetime.now(timezone.utc) - timedelta(minutes=int(window_minutes))
        q = q.filter(McpAuditLog.created_at >= since)
    if tool_name:
        q = q.filter(McpAuditLog.tool_name == tool_name)
    if principal_id:
        q = q.filter(McpAuditLog.principal_id == principal_id)
    if principal_role:
        if principal_role == "anonymous":
            q = q.filter(McpAuditLog.principal_role.is_(None))
        else:
            q = q.filter(McpAuditLog.principal_role == principal_role)
    if success is not None:
        q = q.filter(McpAuditLog.success.is_(bool(success)))
    if denied_only:
        q = q.filter(McpAuditLog.denied.is_(True))
    if result == "success":
        q = q.filter(McpAuditLog.success.is_(True))
    elif result == "denied":
        q = q.filter(McpAuditLog.denied.is_(True))
    elif result == "rate_limited":
        q = q.filter(McpAuditLog.error.like(f"{RATE_LIMITED_PREFIX}%"))
    elif result == "failed":
        q = q.filter(McpAuditLog.success.is_(False), McpAuditLog.denied.is_(False))
        q = q.filter(
            or_(
                McpAuditLog.error.is_(None),
                ~McpAuditLog.error.like(f"{RATE_LIMITED_PREFIX}%"),
            )
        )
    total = q.count()
    rows = (
        q.order_by(McpAuditLog.created_at.desc())
        .offset(max(0, offset))
        .limit(limit)
        .all()
    )
    return [_row_to_dict(r) for r in rows], total
