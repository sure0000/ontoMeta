from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

import uuid

from app.database import Base
from app.models.ontology import EntityStatus


def _uuid() -> str:
    return str(uuid.uuid4())


class BusinessLogicCategory(Base):
    __tablename__ = "business_logic_categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    business_logics: Mapped[list["BusinessLogic"]] = relationship(back_populates="category")


class BusinessLogic(Base):
    __tablename__ = "business_logics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_logic_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    logic_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expression_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    expression_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    expression_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontology: Mapped["Ontology"] = relationship(back_populates="business_logics")
    category: Mapped["BusinessLogicCategory | None"] = relationship(back_populates="business_logics")
    object_bindings: Mapped[list["BusinessLogicObjectBinding"]] = relationship(
        back_populates="business_logic", cascade="all, delete-orphan"
    )
    property_bindings: Mapped[list["BusinessLogicPropertyBinding"]] = relationship(
        back_populates="business_logic", cascade="all, delete-orphan"
    )


class BusinessLogicObjectBinding(Base):
    """业务逻辑到 ObjectType（表/对象）的显式绑定。

    role: subject=主对象 / dimension=维度对象 / output=产出对象
    source: inferred=LLM 或规则推断 / manual=人工绑定
    """

    __tablename__ = "business_logic_object_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_logic_id: Mapped[str] = mapped_column(
        ForeignKey("business_logics.id"), index=True
    )
    object_type_id: Mapped[str] = mapped_column(
        ForeignKey("object_types.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(50), default="subject")
    source: Mapped[str] = mapped_column(String(50), default="inferred")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    business_logic: Mapped["BusinessLogic"] = relationship(
        back_populates="object_bindings"
    )
    object_type: Mapped["ObjectType"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "business_logic_id",
            "object_type_id",
            "role",
            name="uq_logic_object_role",
        ),
    )


class BusinessLogicPropertyBinding(Base):
    """业务逻辑到 Property（字段）的显式绑定。

    role: input=口径输入 / output=结果输出 / filter=过滤条件 / group=分组维度
    source: inferred=LLM 或规则推断 / manual=人工绑定
    """

    __tablename__ = "business_logic_property_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_logic_id: Mapped[str] = mapped_column(
        ForeignKey("business_logics.id"), index=True
    )
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    role: Mapped[str] = mapped_column(String(50), default="input")
    source: Mapped[str] = mapped_column(String(50), default="inferred")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    business_logic: Mapped["BusinessLogic"] = relationship(
        back_populates="property_bindings"
    )
    property: Mapped["Property"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "business_logic_id",
            "property_id",
            "role",
            name="uq_logic_property_role",
        ),
    )
