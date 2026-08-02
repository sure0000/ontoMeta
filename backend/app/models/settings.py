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


class AirflowSetting(Base):
    """Airflow 编排配置：单例配置行，在设置页管理。

    物化改由 Airflow 编排后（M10），落库不再由 ontoMeta 直连执行。这里存的是
    **调度器怎么连**，以及 DAG/作业配置往哪投递；目标库与源库的凭据不在这里——
    那些是 Airflow 侧的 Connection，产物里只出现 conn_id。
    """

    __tablename__ = "airflow_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    endpoint: Mapped[str] = mapped_column(String(512), default="http://localhost:8081")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(512), nullable=True)
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Airflow 2.x=v1，3.x=v2。起栈后以 /openapi.json 实测为准，不照抄文档。
    api_version: Mapped[str] = mapped_column(String(10), default="v1")
    # DAG 文件与 SeaTunnel 作业配置的投递目录（本地为挂载卷，生产可为 git-sync 工作区）
    dags_dir: Mapped[str] = mapped_column(String(512), default="")
    jobs_dir: Mapped[str] = mapped_column(String(512), default="")
    # DAG 里建表任务用的 Airflow Connection id
    warehouse_conn_id: Mapped[str] = mapped_column(String(255), default="warehouse_default")
    seatunnel_image: Mapped[str] = mapped_column(
        String(255), default="apache/seatunnel:2.3.11"
    )
    # 关掉即回到 direct 直连落库（开发模式）。未配置 dags_dir 时也视为不可用。
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
    use_mock: Mapped[bool] = mapped_column(Boolean, default=True)
    preagg_refresh: Mapped[str] = mapped_column(String(50), default="1 hour")
    tenant_dimension: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
