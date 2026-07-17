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
