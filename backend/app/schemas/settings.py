from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

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
    use_mock: bool
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
    use_mock: bool = False


class LlmServiceConfigUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None
    use_mock: bool | None = None


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
    use_mock: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class DatahubSettingsUpdate(BaseModel):
    gms_url: str
    frontend_url: str
    token: str | None = None
    use_mock: bool = False


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
    use_mock: bool
    preagg_refresh: str
    tenant_dimension: str | None = None
    timeout_seconds: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class AirflowSettingsOut(BaseModel):
    """Airflow 编排配置。凭据只回传「是否已设 + 掩码」，不回明文。"""

    endpoint: str
    username: str | None = None
    password_set: bool = False
    password_hint: str | None = None
    token_set: bool = False
    api_version: str
    dags_dir: str
    jobs_dir: str
    warehouse_conn_id: str
    seatunnel_image: str
    enabled: bool
    # 启用且投递目录齐全才算真的可用；否则物化回落到 direct 开发模式。
    available: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class AirflowSettingsUpdate(BaseModel):
    endpoint: str
    username: str | None = None
    password: str | None = None  # 不传 = 保留原值
    token: str | None = None
    api_version: str = "v1"
    dags_dir: str = ""
    jobs_dir: str = ""
    warehouse_conn_id: str = "warehouse_default"
    seatunnel_image: str = "apache/seatunnel:2.3.11"
    enabled: bool = False


class CubeSettingsUpdate(BaseModel):
    api_url: str
    api_secret: str | None = None
    use_mock: bool = True
    preagg_refresh: str = "1 hour"
    tenant_dimension: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=600)
