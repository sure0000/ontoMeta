"""管理端主体（Principal）与四层角色。

Token 存储方案：SHA-256(pepper + 明文)，库内只存哈希与前缀，明文仅在
创建/轮换时返回一次。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Role(str, enum.Enum):
    """四层角色，权限自低到高。

    reader   读
    editor   编辑（改本体、跑草稿生成）
    reviewer 确认/复核（二次确认、冲突裁决）
    publisher 发布/删除/执行（发布本体、删数据、执行 SQL、改设置、管主体）
    """

    READER = "reader"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    PUBLISHER = "publisher"


_RANK: dict[str, int] = {
    Role.READER.value: 0,
    Role.EDITOR.value: 1,
    Role.REVIEWER.value: 2,
    Role.PUBLISHER.value: 3,
}


def role_rank(role: str | Role | None) -> int:
    value = role.value if isinstance(role, Role) else (role or "")
    return _RANK.get(value, -1)


def role_satisfies(actual: str | Role | None, minimum: str | Role) -> bool:
    return role_rank(actual) >= role_rank(minimum)


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(30), default=Role.READER.value, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    token_prefix: Mapped[str] = mapped_column(String(32), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
