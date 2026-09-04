"""Server-delivered MCP skills.

The checked-in skill pack is the default source of truth.  This table stores
only deployment-local overrides and the enabled flag; the built-in body is
never copied into the database unless an operator explicitly edits it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class McpSkill(Base):
    __tablename__ = "mcp_skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    source: Mapped[str] = mapped_column(String(20), default="builtin", server_default="builtin")
    builtin_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class McpSkillVersion(Base):
    """Append-only snapshots of every Skill change."""

    __tablename__ = "mcp_skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_name", "version", name="uq_mcp_skill_versions_skill_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_name: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    body_md: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(20))  # override / restore
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    builtin_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
