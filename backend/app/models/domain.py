import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class DomainContext(Base):
    __tablename__ = "domain_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    datahub_domain_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontologies: Mapped[list["Ontology"]] = relationship(back_populates="domain_context")


class DraftGenerationTask(Base):
    __tablename__ = "draft_generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    ontology_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontologies.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DraftChunkCheckpoint(Base):
    """草稿分块生成的按块检查点。

    分块 Map-Reduce 生成时，每个子块成功后把其结果落库；执行中途失败后重试
    时可复用已完成子块、跳过重复的 LLM 调用以节省 token。按数据域 + 块内容
    哈希(chunk_key)寻址，新建生成会先清空该域检查点，成功后同样清空。
    """

    __tablename__ = "draft_chunk_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "domain_context_id", "chunk_key", name="uq_draft_chunk_checkpoint"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id", ondelete="CASCADE"), index=True
    )
    chunk_key: Mapped[str] = mapped_column(String(64), index=True)
    output_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
