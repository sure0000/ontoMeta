"""MCP 会话鉴权。

stdio 传输没有逐请求的 HTTP 头：一个子进程就是一条会话、一个身份。身份由启动 MCP
服务器的客户端（Claude Desktop / Cursor）在其配置的 ``env`` 块里传入一个 Token——与
REST 的 ``X-Admin-Token`` / Principal Token **同价**，解析走 ``app.auth`` 里唯一的那份
判定逻辑（``resolve_principal_token``），MCP 侧不另抄哈希/比对。

``ONTOMETA_MCP_TOKEN``：
- 匹配 ``ONTOMETA_ADMIN_TOKEN`` → publisher（superuser）
- 匹配某个启用中的 Principal → 该主体的角色
- 缺失/不匹配 → 无身份，角色回落 ``mcp_default_role``（默认 reader，供本地只读开箱即用；
  置空则无匿名身份，需要角色的工具一律拒绝）
"""

from __future__ import annotations

import logging
import os

from app.auth import resolve_principal_token
from app.config import settings

from .tools import AuthContext

logger = logging.getLogger(__name__)

_TOKEN_ENV = "ONTOMETA_MCP_TOKEN"


def _configured_token() -> str | None:
    # 进程环境优先于 .env（客户端在 config 的 env 块里传的就是进程环境）。
    return (os.environ.get(_TOKEN_ENV) or settings.ontometa_mcp_token or "").strip() or None


def resolve_auth_context() -> AuthContext:
    """解析当前 MCP 会话的身份。整条会话共用一份（在服务器启动时解析一次）。"""
    token = _configured_token()
    role, principal_id = resolve_principal_token(token)

    if role is None:
        # 无有效 Token：回落到匿名默认角色。空字符串表示不授予任何角色。
        default_role = (settings.mcp_default_role or "").strip() or None
        return AuthContext(
            client_type="mcp_local",
            role=default_role,
            principal_id=None,
            principal_name=None,
        )

    # 有身份：admin token 无 principal_id（superuser），Principal Token 带上 id。
    name = None
    if principal_id:
        from app.database import SessionLocal
        from app.models.principal import Principal

        with SessionLocal() as db:
            principal = db.get(Principal, principal_id)
            name = principal.name if principal else None

    return AuthContext(
        client_type="mcp_local",
        role=role,
        principal_id=principal_id,
        principal_name=name,
        user_id=principal_id,
    )


def _bearer_from_headers(headers) -> str | None:
    """从请求头取 Bearer 令牌。``headers`` 可为 Starlette Headers 或 ``(bytes,bytes)`` 列表。"""
    value = None
    try:
        value = headers.get("authorization")  # Starlette Headers（大小写不敏感）
    except AttributeError:
        for k, v in headers or []:
            if k.lower() == b"authorization":
                value = v.decode("latin-1")
                break
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return value or None


def resolve_http_auth(request) -> AuthContext:
    """远程 HTTP 传输的**逐请求**身份。

    与 stdio 的「一进程一 env Token」不同：每个 HTTP 请求各带各的 Authorization 头，
    绝不能复用进程级会话身份。令牌解析与 REST/stdio 共用 ``resolve_principal_token``。

    无有效令牌时：``mcp_http_allow_anonymous`` 为真才回落 ``mcp_default_role``，否则无身份
    （role=None）——由服务器的授权闸门 fail-closed 拦下（公网默认不匿名）。
    """
    headers = getattr(request, "headers", None)
    token = _bearer_from_headers(headers) if headers is not None else None
    role, principal_id = resolve_principal_token(token)

    if role is None and settings.mcp_http_allow_anonymous:
        role = (settings.mcp_default_role or "").strip() or None

    name = None
    if principal_id:
        from app.database import SessionLocal
        from app.models.principal import Principal

        with SessionLocal() as db:
            principal = db.get(Principal, principal_id)
            name = principal.name if principal else None

    return AuthContext(
        client_type="mcp_remote",
        role=role,
        principal_id=principal_id,
        principal_name=name,
        user_id=principal_id,
    )
