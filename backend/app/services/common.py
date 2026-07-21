"""服务层共享工具。"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.models import EntityChangeLog


def make_http_client() -> httpx.Client:
    """Create an httpx sync client that ignores system proxy env vars.

    The OpenAI SDK internally uses httpx and defaults to trust_env=True,
    which picks up HTTP_PROXY / ALL_PROXY / socks5 proxy settings from the
    environment. When a SOCKS proxy is configured, httpx requires the
    ``socksio`` extra to be installed.  By disabling trust_env we avoid
    that dependency and keep the SDK talking directly to the LLM endpoint.
    """
    return httpx.Client(trust_env=False)


def make_async_http_client() -> httpx.AsyncClient:
    """Async variant of :func:`make_http_client` for use with AsyncOpenAI."""
    return httpx.AsyncClient(trust_env=False)


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
