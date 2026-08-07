from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
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
    """Airflow 编排配置：单例配置行，**全部在设置页管理，不需要配置文件**。

    物化改由 Airflow 编排后（M10），落库一律由 Airflow 执行（不再有直连落库模式）。
    调度器怎么连、DAG 往哪投、走哪条执行通道、分批与超时怎么定，全在这一行里。

    **环境变量只用于首次播种**（见 ``settings_service.ensure_defaults``）：新库建这一行时
    从 ``config.Settings`` 取一次初值，此后以本行为准。这样既能让已有部署平滑过渡，
    又不会出现「改了 .env 却不生效」这种两个事实源打架的情况。

    仍**不在**这里的：搬运工具与同步策略由物化弹窗逐次选；目标仓的 Airflow Connection id
    由目标数据源推导；源库/目标库的凭据分别归 Airflow Connection（docker 通道）与
    sync-runner 的 secrets（runner 通道）——凭据只有一个归属地，产物里只出现别名。
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

    # ---- 投递：DAG 与作业配置落到哪个目录 ----
    # 必须是 Airflow 真正挂进容器的那个目录，否则 DAG 落了盘也没人解析
    # （preflight 的「DAG 目录双向可见」专治这一项）。
    dags_dir: Mapped[str] = mapped_column(String(512), default="")
    jobs_dir: Mapped[str] = mapped_column(String(512), default="")

    # ---- 执行通道 ----
    # runner：Airflow 任务向常驻 sync-runner 发 HTTP（默认）；
    # docker：Airflow 经 docker.sock 起搬运容器（旧通道，保留作回退）。
    sync_channel: Mapped[str] = mapped_column(String(16), default="runner")
    sync_runner_endpoint: Mapped[str] = mapped_column(String(512), default="")
    # ontoMeta 调 runner 的 Bearer token。runner 侧设了 SYNC_RUNNER_TOKEN 才需要；
    # **写连接配置必须有它**（runner 没配 token 时写接口直接 403，不敞着）。
    sync_runner_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 强制指定搬运工具。空 = 自动（见 services/sync_tool_resolver）：物化弹窗不再让人
    # 逐次选工具——工具是部署事实，且 runner 通道下它压根不参与执行。
    sync_tool: Mapped[str] = mapped_column(String(32), default="")
    # 下面三项只服务 docker 通道，runner 通道下不生效。
    docker_network: Mapped[str] = mapped_column(String(128), default="bridge")
    drivers_dir: Mapped[str] = mapped_column(String(512), default="")
    # ``工具名=镜像`` 逗号分隔。无官方镜像的工具（DataX）只有在这里配了才可选。
    sync_tool_images: Mapped[str] = mapped_column(String(1024), default="")

    # ---- DAG 形状与时序 ----
    max_tasks_per_dag: Mapped[int] = mapped_column(Integer, default=50)
    max_active_tasks_per_dag: Mapped[int] = mapped_column(Integer, default=16)
    # 落盘后等 Airflow 解析到 DAG 的超时。要大于 Airflow 的 dag_dir_list_interval，
    # 否则首次提交必然报「尚未解析到」（该值默认 300s，按你的部署实测调）。
    dag_parse_timeout: Mapped[float] = mapped_column(Float, default=60.0)
    preflight_sentinel_timeout: Mapped[float] = mapped_column(Float, default=20.0)
    # 全量装载是否走 staging + 原子切换（搬到一半失败时正式表原封不动）。
    staging_swap: Mapped[bool] = mapped_column(Boolean, default=True)

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


class DependencyComponent(Base):
    """依赖组件统一注册表（DEPENDENCY_DEPLOYMENT_REDESIGN §3）。

    除 ontoMeta 自身前后端外，所有依赖组件（LLM / DataHub / Airflow / SeaTunnel /
    目标数仓 / sync-runner / Cube / PG / Bigtop）在此统一管理部署方式与连接信息。

    - ``deploy_mode`` 决定「怎么来的」：external(已有)/docker/k8s/bare_metal。
    - ``connection`` 记「怎么连」：部署成功自动回写，或 external 时手填。
    - ``deploy_spec`` / ``connection`` 以 Text(json) 存储，与项目既有范式一致（SQLite/PG 通用）。

    Phase 0：本表与既有五张设置表并行存在，不接读取侧；Phase 1 起读取侧改为从本表投影。
    ERPNext 等外部源库不在此纳管——它们是外部数据源，走 ``DataSource``。
    """

    __tablename__ = "dependency_components"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 组件类型：llm/datahub/airflow/seatunnel/warehouse/sync_runner/cube/postgres/bigtop
    key: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # 部署：external | docker | k8s | bare_metal
    deploy_mode: Mapped[str] = mapped_column(String(16), default="external")
    deploy_spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # not_deployed|deploying|deployed|failed|connected
    deploy_status: Mapped[str] = mapped_column(String(16), default="not_deployed")
    deploy_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 连接信息（JSON），结构由 key 决定（见 DependencyComponentService.CONNECTION_SCHEMAS）
    connection_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
