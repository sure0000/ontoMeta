from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

class LlmModelOption(BaseModel):
    id: str
    label: str
    description: str
    deprecated: bool = False


class LlmServiceConfigOut(BaseModel):
    id: str
    name: str
    provider: str
    api_base_url: str
    model: str
    is_default: bool
    enabled: bool
    api_key_set: bool
    api_key_hint: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LlmServiceConfigDetail(LlmServiceConfigOut):
    api_key: str | None = None


class LlmServiceConfigCreate(BaseModel):
    name: str
    provider: str = "deepseek"
    api_base_url: str = "https://api.deepseek.com"
    api_key: str | None = None
    model: str
    is_default: bool = False
    enabled: bool = True


class LlmServiceConfigUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class LlmConnectionTestRequest(BaseModel):
    """连接测试入参：可测试未保存的表单配置。编辑态留空 api_key 时用 service_id 取回已存密钥。"""

    api_base_url: str
    model: str
    provider: str = "deepseek"
    api_key: str | None = None
    service_id: str | None = None


class LlmConnectionTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
    model: str | None = None


class DatahubSettingsOut(BaseModel):
    gms_url: str
    frontend_url: str
    token_set: bool
    token_hint: str | None = None
    fabric: str = "PROD"
    updated_at: datetime

    model_config = {"from_attributes": True}


class DatahubSettingsUpdate(BaseModel):
    gms_url: str
    frontend_url: str
    token: str | None = None
    fabric: str = "PROD"


class DraftGenerationSettingsOut(BaseModel):
    object_chunk_concurrency: int
    relation_chunk_concurrency: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class DraftGenerationSettingsUpdate(BaseModel):
    object_chunk_concurrency: int = Field(ge=1, le=32)
    relation_chunk_concurrency: int = Field(ge=1, le=32)


class CubeSettingsOut(BaseModel):
    api_url: str
    secret_set: bool
    secret_hint: str | None = None
    preagg_refresh: str
    tenant_dimension: str | None = None
    timeout_seconds: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class AirflowSettingsOut(BaseModel):
    """Airflow 编排配置。凭据只回传「是否已设 + 掩码」，不回明文。

    编排的全部可调项都在这里回传：不需要改任何配置文件，设置页即唯一入口。
    """

    endpoint: str
    username: str | None = None
    password_set: bool = False
    password_hint: str | None = None
    token_set: bool = False
    api_version: str
    enabled: bool
    # 启用且 endpoint 已填才算真的可用；否则物化无法执行（不再有直连回退）。
    available: bool
    # ---- 投递目录 ----
    dags_dir: str = ""
    jobs_dir: str = ""
    # ---- 执行通道 ----
    sync_channel: str = "runner"
    sync_runner_endpoint: str = ""
    sync_runner_token_set: bool = False
    docker_network: str = "bridge"
    drivers_dir: str = ""
    sync_tool_images: str = ""
    # 空 = 自动选搬运工具（物化弹窗不再逐次选，见 services/sync_tool_resolver）。
    sync_tool: str = ""
    # ---- DAG 形状与时序 ----
    max_tasks_per_dag: int = 50
    max_active_tasks_per_dag: int = 16
    dag_parse_timeout: float = 60.0
    preflight_sentinel_timeout: float = 20.0
    staging_swap: bool = True
    updated_at: datetime

    model_config = {"from_attributes": True}


class AirflowSettingsUpdate(BaseModel):
    endpoint: str
    username: str | None = None
    password: str | None = None  # 不传 = 保留原值
    token: str | None = None
    api_version: str = "v1"
    enabled: bool = False
    dags_dir: str = ""
    jobs_dir: str = ""
    # runner：Airflow 任务向常驻 sync-runner 发 HTTP；docker：经 docker.sock 起搬运容器。
    sync_channel: str = Field(default="runner", pattern="^(runner|docker)$")
    sync_runner_endpoint: str = ""
    sync_runner_token: str | None = None  # 不传 = 保留原值
    docker_network: str = "bridge"
    drivers_dir: str = ""
    sync_tool_images: str = ""
    # 强制指定搬运工具；空 = 自动。取值必须是注册表里真有的工具，写错一个名字会让物化
    # 在提交时才报「未知搬运工具」，故在这里就挡掉。
    sync_tool: str = ""
    max_tasks_per_dag: int = Field(default=50, ge=1, le=1000)
    max_active_tasks_per_dag: int = Field(default=16, ge=1, le=256)
    # 要大于 Airflow 的 dag_dir_list_interval，否则首次提交必报「尚未解析到」。
    dag_parse_timeout: float = Field(default=60.0, ge=0, le=3600)
    preflight_sentinel_timeout: float = Field(default=20.0, ge=0, le=600)
    staging_swap: bool = True

    @field_validator("sync_tool")
    @classmethod
    def _known_tool(cls, v: str) -> str:
        # 校验依据取自注册表本身：新增工具时这里自动跟上，不必再改一处枚举。
        from app.warehouse.jobs import list_sync_tools

        name = (v or "").strip().lower()
        known = list_sync_tools()
        if name and name not in known:
            # PydanticCustomError 而非裸 ValueError：后者会把异常对象塞进 422 的 ctx，
            # FastAPI 序列化响应时直接炸成 500，报错反而看不见了。
            raise PydanticCustomError(
                "unknown_sync_tool",
                "未知搬运工具 {value}，可选：{options}（留空 = 自动）",
                {"value": v, "options": ", ".join(known)},
            )
        return name


class CubeSettingsUpdate(BaseModel):
    api_url: str
    api_secret: str | None = None
    preagg_refresh: str = "1 hour"
    tenant_dimension: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class SyncRunnerSecretOut(BaseModel):
    """runner 侧一个别名的配置概览。**不含任何机密明文**——机密键只回「已设置」。"""

    alias: str
    # store：由设置页写入 runner 自己的存储，可改；env：部署时钉死的环境变量，只读。
    source: str
    values: dict[str, str] = Field(default_factory=dict)


class SyncRunnerSecretUpdate(BaseModel):
    """写一个别名的连接配置。

    值**穿透到 runner 就没了**：ontoMeta 不落库、不缓存——设置页只是代填的输入框，
    不是凭据库（凭据只有一个归属地，见 MATERIALIZE_SYNC_STABILITY.md §3.1）。
    传空串表示清掉该项。
    """

    values: dict[str, str] = Field(
        description="如 {'url': 'mysql+pymysql://u:p@h:3306/db', 'metastore_uri': 'thrift://h:9083'}"
    )
