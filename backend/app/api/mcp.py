"""MCP 服务的管理 REST（供前端设置页的「MCP 服务」Tab 用）。

前端不能直接说 MCP 协议，故把「服务状态 / 审计 / 统计」用普通 REST 暴露。这些走
主后端的 AdminAuthMiddleware：
- ``/api/mcp/info``：服务形态 + 工具清单（不敏感）→ reader 起（GET 默认）。
- ``/api/mcp/audit`` / ``/api/mcp/stats``：含每次调用的主体与入参 → 显式要求 publisher。

数据逻辑一律走 ``app.mcp.introspection``（与 MCP 工具 server_info/get_mcp_stats/
list_audit_logs 共用同一份），REST 只是薄壳。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import require_role
from app.database import get_db
from app.mcp import introspection

router = APIRouter()


@router.get("/mcp/info")
def mcp_info(db: Session = Depends(get_db)):
    """MCP 服务形态：传输、鉴权策略、限流、工具清单、审计可达性。"""
    status = introspection.service_status()
    status["audit"] = introspection.audit_health(db)
    return status


@router.get("/mcp/stats", dependencies=[Depends(require_role("publisher"))])
def mcp_stats(
    window_minutes: int | None = Query(None, ge=1, le=43200),
    top_tools: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """审计聚合统计。仅 publisher。"""
    return introspection.compute_stats(
        db, window_minutes=window_minutes, top_tools=top_tools
    )


@router.get("/mcp/audit", dependencies=[Depends(require_role("publisher"))])
def mcp_audit(
    tool_name: str | None = Query(None),
    success: bool | None = Query(None),
    denied_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """审计日志分页。入参在写入时已脱敏。仅 publisher。"""
    logs, total = introspection.query_audit(
        db,
        tool_name=tool_name,
        success=success,
        denied_only=denied_only,
        limit=limit,
        offset=offset,
    )
    return {"logs": logs, "total": total, "limit": limit, "offset": offset}
