from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# 对外应用可用的全部 scope（单一枚举）
ALL_EXTERNAL_SCOPES: list[str] = [
    "domains:read",
    "objects:read",
    "relations:read",
    "logics:read",
]


class ExternalAppCreate(BaseModel):
    name: str
    description: str | None = None
    scopes: list[str] | None = None
    rate_limit_per_minute: int | None = None


class ExternalAppUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    scopes: list[str] | None = None
    rate_limit_per_minute: int | None = None


class ExternalAppOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    app_key: str
    api_key_hint: str | None = None
    api_key: str | None = None
    scopes: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None

    model_config = {"from_attributes": True}


class ExternalAppCreated(ExternalAppOut):
    """创建或重置密钥后返回，包含明文 api_key。"""

    api_key: str


class ExternalApiCatalogItem(BaseModel):
    """MCP Tool / REST 目录项（控制台文档 / 试用页）；与 MCP tools/list 同源。"""

    id: str
    name: str
    tool_name: str
    category: str
    description: str
    auth_required: bool = True
    required_scope: str
    rest_method: str | None = None
    rest_path: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_fields: list[dict[str, Any]] = Field(default_factory=list)
    example_result: Any = None
    mcp_endpoint: str = "/api/mcp"


class McpToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpToolCallResult(BaseModel):
    content: list[dict[str, Any]] = Field(default_factory=list)
    structuredContent: Any = None
    isError: bool = False

    model_config = {"populate_by_name": True}


class ExternalApiCallLogOut(BaseModel):
    id: str
    app_id: str
    tool_name: str | None = None
    path: str | None = None
    status_code: int
    duration_ms: int | None = None
    error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
