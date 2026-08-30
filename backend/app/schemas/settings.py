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


class AirflowSettingsOut(BaseModel):
    """Airflow 编排配置。凭据只回传「是否已设 + 掩码」，不回明文。

    编排的全部可调项都在这里回传：不需要改任何配置文件，设置页即唯一入口。
    """

    # ---- 连接一：调度 API ----
    # 没有 token / api_version：前者 Airflow REST 用的是 basic auth，从没有部署路径
    # 产出过 bearer token；后者由客户端 404 时自协商（见 connectors/airflow.py）。
    endpoint: str
    username: str | None = None
    password_set: bool = False
    password_hint: str | None = None
    enabled: bool
    # 启用且 endpoint / SSH 主机已填才算真的可用；否则物化无法执行（不再有直连回退）。
    available: bool
    # ---- 投递目录（**Airflow 主机上的路径**）----
    dags_dir: str = ""
    # ---- SSH 投递（唯一通道：产物 rsync 到 Airflow 主机后原子切换）----
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    # 没有私钥路径：那是 ontoMeta 主机上的文件，归该机 ~/.ssh/config 管，
    # 在 Web 表单里填别处的路径只会得到一个测不出真假的配置。密码留空即走默认身份。
    ssh_password_set: bool = False
    ssh_password_hint: str | None = None
    # ---- DAG 形状与时序 ----
    max_tasks_per_dag: int = 50
    max_active_tasks_per_dag: int = 16
    dag_parse_timeout: float = 60.0
    staging_swap: bool = True
    updated_at: datetime

    model_config = {"from_attributes": True}


class AirflowSettingsUpdate(BaseModel):
    endpoint: str
    username: str | None = None
    password: str | None = None  # 不传 = 保留原值
    enabled: bool = False
    dags_dir: str = ""
    # SSH 投递：ontoMeta / Airflow / Flink 常分处三台机器，产物经 rsync 推到 Airflow
    # 主机。填了 ssh_password 就用密码（需 sshpass），留空则用本机默认 SSH 身份/agent。
    ssh_host: str = ""
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = ""
    ssh_password: str | None = None  # 不传 = 保留原值
    max_tasks_per_dag: int = Field(default=50, ge=1, le=1000)
    max_active_tasks_per_dag: int = Field(default=16, ge=1, le=256)
    # 要大于 Airflow 的 dag_dir_list_interval，否则首次提交必报「尚未解析到」。
    dag_parse_timeout: float = Field(default=60.0, ge=0, le=3600)
    staging_swap: bool = True
