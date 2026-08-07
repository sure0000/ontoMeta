"""依赖组件统一部署管理的请求/响应模型（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DependencySchemaOut(BaseModel):
    """组件目录 + 连接/部署 schema 自描述，供前端表单生成。"""

    components: list[dict[str, Any]]
    connection_schemas: dict[str, list[dict[str, Any]]]
    deploy_modes: list[str]
    deploy_spec_schemas: dict[str, list[dict[str, Any]]]
    deploy_statuses: list[str]


class DependencyComponentOut(BaseModel):
    id: str
    key: str
    name: str
    deploy_mode: str
    deploy_spec: dict[str, Any] = Field(default_factory=dict)
    deploy_status: str
    deploy_error: str | None = None
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


class DeployResultOut(BaseModel):
    status: str
    ok: bool = False
    message: str | None = None
