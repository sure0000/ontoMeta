from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

import uuid

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ExternalApp(Base):
    """外部系统接入应用，通过 API Key 调用对外只读接口。

    明文 Key 仅在创建/轮换响应中返回一次；库内只存 hash 与 prefix。
    遗留列 api_key 在启动迁移后清空，仅为兼容旧库结构保留。
    scopes：JSON 数组字符串，如 ["domains:read","objects:read"]。
    """

    __tablename__ = "external_apps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # 遗留明文列：迁移后置空；新写入不再使用
    api_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    # JSON 数组：权限 scope；空/NULL 视为全部默认 scope（兼容旧行）
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每分钟请求上限；NULL 用全局配置
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExternalApiCallLog(Base):
    """外部 API / MCP 调用日志（控制台可查）。"""

    __tablename__ = "external_api_call_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("external_apps.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
