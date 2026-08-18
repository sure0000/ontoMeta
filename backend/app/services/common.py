"""服务层共享工具。"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EntityChangeLog


def _http_timeout() -> httpx.Timeout:
    """LLM 端点分档超时：连接快失败，读写按大模型生成留足预算。

    裸 ``httpx`` 客户端只有一个总 timeout，连接握手慢会挤占生成读预算；分档后连接
    10s 内失败即重试，读超时才等满生成时长。
    """
    return httpx.Timeout(
        settings.llm_timeout_seconds,
        connect=min(settings.llm_connect_timeout_seconds, settings.llm_timeout_seconds),
    )


def _http_limits() -> httpx.Limits:
    """连接池上限 + 短 keepalive 存活期：主动在服务器关闭空闲连接前丢弃，避免复用
    死连接触发 ReadError/RemoteProtocolError（大扇出预生成的主要失败源）。"""
    return httpx.Limits(
        max_keepalive_connections=settings.llm_http_max_keepalive,
        keepalive_expiry=settings.llm_http_keepalive_expiry_seconds,
    )


def make_http_client() -> httpx.Client:
    """Create an httpx sync client that ignores system proxy env vars.

    The OpenAI SDK internally uses httpx and defaults to trust_env=True,
    which picks up HTTP_PROXY / ALL_PROXY / socks5 proxy settings from the
    environment. When a SOCKS proxy is configured, httpx requires the
    ``socksio`` extra to be installed.  By disabling trust_env we avoid
    that dependency and keep the SDK talking directly to the LLM endpoint.
    """
    return httpx.Client(
        trust_env=False, timeout=_http_timeout(), limits=_http_limits()
    )


def make_async_http_client() -> httpx.AsyncClient:
    """Async variant of :func:`make_http_client` for use with AsyncOpenAI."""
    return httpx.AsyncClient(
        trust_env=False, timeout=_http_timeout(), limits=_http_limits()
    )


def log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    """写入一条实体变更审计日志。仅 db.add，调用方负责 commit。"""
    db.add(
        EntityChangeLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            operator=operator,
            change_summary=summary,
        )
    )
