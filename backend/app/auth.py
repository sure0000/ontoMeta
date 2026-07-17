"""管理端鉴权：共享 Admin Token（Batch B1）。

请求头（二选一）：
  - X-Admin-Token: <token>
  - Authorization: Bearer <token>

环境变量：ONTOMETA_ADMIN_TOKEN
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import settings

# 管理鉴权豁免：健康检查、对外 v1、MCP（已有 External App API Key）
_ADMIN_EXEMPT_EXACT = frozenset({"/health"})
_ADMIN_EXEMPT_PREFIXES = ("/api/v1", "/api/mcp")


def is_admin_auth_exempt(path: str) -> bool:
    if path in _ADMIN_EXEMPT_EXACT:
        return True
    if not path.startswith("/api"):
        return True
    for prefix in _ADMIN_EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def extract_admin_token(request: Request) -> str | None:
    header = request.headers.get("x-admin-token")
    if header and header.strip():
        return header.strip()
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


def verify_admin_token(provided: str | None) -> None:
    """校验管理 Token；失败抛出 HTTPException。"""
    expected = (settings.ontometa_admin_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="未配置 ONTOMETA_ADMIN_TOKEN，管理 API 不可用。请在 backend/.env 中设置后重启。",
        )
    if not provided:
        raise HTTPException(
            status_code=401,
            detail="缺少管理鉴权：请在请求头传入 X-Admin-Token 或 Authorization: Bearer <token>",
        )
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="管理 Token 无效")


def hash_api_key(raw_key: str, pepper: str | None = None) -> str:
    material = f"{pepper or ''}{raw_key}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def api_key_prefix(raw_key: str, length: int = 12) -> str:
    return raw_key[:length] if raw_key else ""


def generate_dev_admin_token() -> str:
    """仅用于文档示例，勿在生产使用固定值。"""
    return f"om_admin_{secrets.token_urlsafe(24)}"


class AdminAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if is_admin_auth_exempt(path):
            return await call_next(request)
        try:
            verify_admin_token(extract_admin_token(request))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
