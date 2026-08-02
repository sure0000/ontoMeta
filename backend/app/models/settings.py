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
    # DataHub 环境标（PROD/DEV/…）：构造目标表 dataset URN 时用（M11 血缘）。
    # 源表 URN 自带 fabric（来自 source_ref），这里只决定物化目标侧。
    fabric: Mapped[str] = mapped_column(String(20), default="PROD", server_default="PROD")
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


class AirflowSetting(Base):
    """Airflow 编排连接配置：单例配置行，在设置页管理。

    物化改由 Airflow 编排后（M10），落库一律由 Airflow 执行（不再有直连落库模式）。这里只存
    **调度器怎么连**（endpoint / 鉴权 / API 版本）。其余信息不在此：
    - 搬运工具（seatunnel/datax/flink）与同步策略由物化弹窗逐次选；镜像由工具 Adapter 定。
    - 目标仓的 Airflow Connection id 由目标数据源推导（弹窗选的 target）。
    - DAG/作业投递目录属部署基础设施，由 ``config.airflow_dags_dir/jobs_dir`` 给默认。
    目标库与源库的凭据都是 Airflow 侧的 Connection，产物里只出现 conn_id。
    """

    __tablename__ = "airflow_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    endpoint: Mapped[str] = mapped_column(String(512), default="http://localhost:8081")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(512), nullable=True)
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Airflow 2.x=v1，3.x=v2。起栈后以 /openapi.json 实测为准，不照抄文档。
    api_version: Mapped[str] = mapped_column(String(10), default="v1")
    # 关掉即物化不可用（需启用才能编排）。未配 endpoint 时也视为不可用。
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CubeSetting(Base):
    """Cube 语义层外挂配置：单例配置行，在设置页管理，无需环境变量/配置文件。"""

    __tablename__ = "cube_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    api_url: Mapped[str] = mapped_column(String(512), default="http://localhost:4000")
    api_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preagg_refresh: Mapped[str] = mapped_column(String(50), default="1 hour")
    tenant_dimension: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
