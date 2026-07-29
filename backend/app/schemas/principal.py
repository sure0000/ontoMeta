from datetime import datetime

from pydantic import BaseModel, Field


class PrincipalOut(BaseModel):
    id: str
    name: str
    role: str
    token_prefix: str
    active: bool
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PrincipalCreated(PrincipalOut):
    """创建/轮换时返回，``token`` 明文此后不可再取。"""

    token: str = Field(description="明文 Token，仅此一次返回，请立即保存")


class PrincipalCreate(BaseModel):
    name: str
    role: str = "reader"


class PrincipalUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None


class RolePolicyItem(BaseModel):
    method: str
    path_pattern: str
    minimum_role: str


class RolePolicyOut(BaseModel):
    roles: list[str]
    method_defaults: dict[str, str]
    overrides: list[RolePolicyItem]
