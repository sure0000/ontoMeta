"""依赖组件统一部署管理的请求/响应模型（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DependencySchemaOut(BaseModel):
    """组件目录 + 连接/部署 schema 自描述，供前端表单生成。"""

    components: list[dict[str, Any]]
    connection_schemas: dict[str, list[dict[str, Any]]]
    # 连接分组：一个组件可能握着几条互不相干的连接（Airflow = 调度 API + DAG 投递），
    # 前端据此分节渲染并逐条拨测。同样必须显式声明，否则被 response_model 剔除。
    connection_groups: dict[str, list[dict[str, Any]]]
    deploy_modes: list[str]
    deploy_spec_schemas: dict[str, list[dict[str, Any]]]
    # 未声明的字段会被 response_model 从响应里剔除，导致前端拿到 undefined 后
    # 在 `schema.bare_metal_params[key]` 处抛 TypeError 白屏。必须显式声明。
    bare_metal_params: dict[str, list[dict[str, Any]]]
    docker_params: dict[str, list[dict[str, Any]]]
    # 每组件允许的部署方式白名单（前端据此收窄模式选择器）。同样必须显式声明，
    # 否则被 response_model 剔除，前端拿到 undefined 又会 TypeError 白屏。
    component_deploy_modes: dict[str, list[str]]
    deploy_statuses: list[str]


class DependencyComponentOut(BaseModel):
    id: str
    key: str
    name: str
    deploy_mode: str
    deploy_spec: dict[str, Any] = Field(default_factory=dict)
    deploy_status: str
    deploy_error: str | None = None
    deploy_log: str | None = None
    connection: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DependencyComponentCreate(BaseModel):
    key: str
    name: str | None = None
    deploy_mode: str = "external"
    deploy_spec: dict[str, Any] = Field(default_factory=dict)
    connection: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False


class DependencyComponentUpdate(BaseModel):
    name: str | None = None
    deploy_mode: str | None = None
    deploy_spec: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class ProbeResultOut(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
    # 逐条连接的拨测明细（Airflow = 调度 API + DAG 投递）。单连接组件也回一条。
    parts: list[dict] = Field(default_factory=list)


class DeployResultOut(BaseModel):
    status: str
    ok: bool = False
    message: str | None = None
