from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

import uuid

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LlmServiceConfig(Base):
    __tablename__ = "llm_service_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(50), default="deepseek")
    api_base_url: Mapped[str] = mapped_column(String(512), default="https://api.deepseek.com")
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model: Mapped[str] = mapped_column(String(100))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    use_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DatahubSetting(Base):
    __tablename__ = "datahub_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    gms_url: Mapped[str] = mapped_column(String(512))
    frontend_url: Mapped[str] = mapped_column(String(512))
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    use_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DraftGenerationSetting(Base):
    """草稿生成分块并发度：单例配置行，可在设置页动态调整，无需改环境变量重启。"""

    __tablename__ = "draft_generation_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    object_chunk_concurrency: Mapped[int] = mapped_column(Integer, default=4)
    relation_chunk_concurrency: Mapped[int] = mapped_column(Integer, default=4)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
