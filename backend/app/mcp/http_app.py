"""MCP 远程 HTTP 传输：把 MCP 挂到主 FastAPI 的 /mcp 路由。

**为什么 json_response + stateless**：主后端的 ``AdminAuthMiddleware`` 是 ``BaseHTTPMiddleware``，
会缓冲响应——SSE 长连流会被它挂住。用 JSON 响应模式（每个请求一个 JSON-RPC 响应，非 SSE
流）绕开这个坑；stateless（不保存跨请求 session）契合我们纯请求-响应的只读工具，也免去
mount 子应用 lifespan 不触发导致的 session 清理问题。

**为什么身份逐请求解析**：每个 HTTP 请求各带各的 ``Authorization: Bearer``，由
``server._auth_for`` 从 ``context.request`` 逐请求解析（见 auth 的 ``resolve_http_auth``）。
无令牌的请求连 initialize 都不给，HTTP 服务始终要求 Principal/Admin Bearer Token。
"""

from __future__ import annotations

import json
import logging

from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)

from .auth import _bearer_from_headers
from .server import build_server

logger = logging.getLogger(__name__)

MCP_HTTP_PATH = "/mcp"

_session_manager: StreamableHTTPSessionManager | None = None


def get_session_manager() -> StreamableHTTPSessionManager:
    """进程级单例。``run()`` 只能调一次，故 session manager 也只建一次。"""
    global _session_manager
    if _session_manager is None:
        _session_manager = StreamableHTTPSessionManager(
            app=build_server(),
            json_response=True,
            stateless=True,
        )
    return _session_manager


async def _send_401(send) -> None:
    body = json.dumps(
        {"detail": "MCP 远程访问需要令牌：Authorization: Bearer <令牌>"},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"www-authenticate", b'Bearer realm="ontometa-mcp"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _AnonymousGuardASGI:
    """强制鉴权层：无令牌的 HTTP 请求在此 401。

    有令牌（哪怕无效）就放行到 session manager，由 handler 精细判定角色——无效令牌会在
    授权闸门被 denied 并审计。这一层只快速挡掉「完全不带令牌」的公网匿名访问。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        token = _bearer_from_headers(scope.get("headers") or [])
        if not token:
            return await _send_401(send)
        return await self.app(scope, receive, send)


def build_mcp_asgi():
    """构建挂到 ``/mcp`` 的 ASGI 应用（含匿名拦截）。"""
    return _AnonymousGuardASGI(StreamableHTTPASGIApp(get_session_manager()))
