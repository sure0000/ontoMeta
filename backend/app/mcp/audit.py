"""MCP 工具调用审计的写入点。

每次工具调用（成功、业务失败、被授权拦下、异常）都在服务器的 ``handle_call_tool`` 里
落一条 ``McpAuditLog``。三条纪律：

1. **绝不影响主链路**：``record_call`` 整体吞异常并 rollback，审计写失败不改变调用结果，
   也不向客户端泄露。
2. **脱敏**：入参里的凭据类键（token/password/dsn…）在落库前 redact，并按上限截断。
   这套脱敏原先借自旧对话的决策账本；账本随会话六环一起退场后搬到这里，成为
   审计自己的东西——审计不能因为另一个模块被删而失去脱敏。
3. **append-only**：只 insert，不 update。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.database import SessionLocal
from app.models.mcp_audit import McpAuditLog

logger = logging.getLogger(__name__)

_SECRET_HINTS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "credential", "dsn",
    "access_key", "secret_key", "private_key", "api_key", "auth",
)

#: 单份 JSON 的上限。防止有人把整张结果集塞进入参撑爆库。
_JSON_CAP = 16_384


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return any(hint in low for hint in _SECRET_HINTS)


def _redact(value: Any, _depth: int = 0) -> Any:
    """递归剔除疑似凭据的键。深度上限防环。"""
    if _depth > 6:
        return "***"
    if isinstance(value, dict):
        return {
            k: ("***" if _is_secret_key(k) else _redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, _depth + 1) for v in value[:200]]
    return value


def _dumps_capped(value: Any | None) -> str | None:
    """脱敏后序列化；超限则截断为一条可读的占位，不抛。"""
    if value is None:
        return None
    try:
        text = json.dumps(_redact(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return json.dumps({"_unserializable": str(exc)}, ensure_ascii=False)
    if len(text) > _JSON_CAP:
        return json.dumps(
            {"_truncated": True, "_bytes": len(text), "_head": text[:2000]},
            ensure_ascii=False,
        )
    return text


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
