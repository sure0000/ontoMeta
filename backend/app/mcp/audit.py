"""MCP 工具调用审计的写入点。

每次工具调用（成功、业务失败、被授权拦下、异常）都在服务器的 ``handle_call_tool`` 里
落一条 ``McpAuditLog``。三条纪律：

1. **绝不影响主链路**：``record_call`` 整体吞异常并 rollback，审计写失败不改变调用结果，
   也不向客户端泄露。照 ``chat_bi_ledger.record_decision`` 的先例。
2. **脱敏**：入参里的凭据类键（token/password/dsn…）在落库前 redact，并按上限截断——
   直接复用 ledger 的 ``_redact`` / ``_dumps_capped``，不另写一套脱敏。
3. **append-only**：只 insert，不 update。
"""

from __future__ import annotations

import logging
from typing import Any

from app.database import SessionLocal
from app.models.mcp_audit import McpAuditLog
from app.services.chat_bi_ledger import _dumps_capped

logger = logging.getLogger(__name__)


def record_call(
    *,
    auth: Any,
    tool_name: str,
    arguments: dict | None,
    success: bool,
    denied: bool = False,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """追加一条审计。失败只记日志，绝不抛。"""
    try:
        with SessionLocal() as db:
            db.add(
                McpAuditLog(
                    principal_id=getattr(auth, "principal_id", None),
                    principal_role=getattr(auth, "role", None),
                    client_type=getattr(auth, "client_type", "mcp_local"),
                    tool_name=tool_name,
                    arguments_json=_dumps_capped(arguments),
                    success=bool(success),
                    denied=bool(denied),
                    error=(str(error)[:500] if error else None),
                    duration_ms=duration_ms,
                )
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计是增强，不是主流程
        logger.info("mcp audit record failed: %s", exc)
