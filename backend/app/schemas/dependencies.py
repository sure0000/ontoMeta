"""依赖组件登记的请求/响应模型。

组件固定（LLM / DataHub / Airflow），只连已有服务：没有创建、没有删除、没有部署。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DependencySchemaOut(BaseModel):
    """组件目录 + 连接 schema 自描述，供前端表单生成。

    未声明的字段会被 response_model 从响应里剔除，前端拿到 undefined 后在
    ``schema.connection_schemas[key]`` 处抛 TypeError 白屏——加字段务必同步声明。
    """

    components: list[dict[str, Any]]
    connection_schemas: dict[str, list[dict[str, Any]]]
    # 连接分组：一个组件可能握着几条互不相干的连接（Airflow = 调度 API + DAG 投递），
    # 前端据此分节渲染并逐条拨测。
    connection_groups: dict[str, list[dict[str, Any]]]
    connection_statuses: list[str]


class DependencyComponentOut(BaseModel):
    id: str
    key: str
    name: str
    # 组件附加配置：Airflow 编排参数 extra、逐条拨测记账 _probe
    settings: dict[str, Any] = Field(default_factory=dict)
    connection_status: str
    connection_error: str | None = None
    connection: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DependencyComponentUpdate(BaseModel):
    name: str | None = None
    settings: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    enabled: bool | None = None


class ProbeResultOut(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
    # 逐条连接的拨测明细（Airflow = 调度 API + DAG 投递）。单连接组件也回一条。
    parts: list[dict] = Field(default_factory=list)
