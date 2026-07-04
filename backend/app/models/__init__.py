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


class OntologyStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EntityStatus(str, enum.Enum):
    SUGGESTED = "suggested"
    EDITED = "edited"
    APPROVED = "approved"
    PRE_PUBLISHED = "pre_published"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class ConfirmationStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


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


class Ontology(Base):
    __tablename__ = "ontologies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(50), default=OntologyStatus.DRAFT.value, index=True
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    domain_context: Mapped["DomainContext"] = relationship(back_populates="ontologies")
    object_types: Mapped[list["ObjectType"]] = relationship(back_populates="ontology")
    relation_types: Mapped[list["RelationType"]] = relationship(back_populates="ontology")
    business_logics: Mapped[list["BusinessLogic"]] = relationship(back_populates="ontology")
    draft_evidences: Mapped[list["DraftEvidence"]] = relationship(back_populates="ontology")
    change_confirmations: Mapped[list["ChangeConfirmation"]] = relationship(
        back_populates="ontology"
    )


class ObjectType(Base):
    __tablename__ = "object_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_term_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontology: Mapped["Ontology"] = relationship(back_populates="object_types")
    properties: Mapped[list["Property"]] = relationship(back_populates="object_type")
    outgoing_relations: Mapped[list["RelationType"]] = relationship(
        back_populates="source_object_type",
        foreign_keys="RelationType.source_object_type_id",
    )
    incoming_relations: Mapped[list["RelationType"]] = relationship(
        back_populates="target_object_type",
        foreign_keys="RelationType.target_object_type_id",
    )


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    object_type_id: Mapped[str] = mapped_column(ForeignKey("object_types.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_field_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    semantic_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    object_type: Mapped["ObjectType"] = relationship(back_populates="properties")


class RelationType(Base):
    __tablename__ = "relation_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_object_type_id: Mapped[str] = mapped_column(
        ForeignKey("object_types.id"), index=True
    )
    target_object_type_id: Mapped[str] = mapped_column(
        ForeignKey("object_types.id"), index=True
    )
    cardinality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    structure_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mapping_object_type_id: Mapped[str | None] = mapped_column(
        ForeignKey("object_types.id"), nullable=True, index=True
    )
    source_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontology: Mapped["Ontology"] = relationship(back_populates="relation_types")
    source_object_type: Mapped["ObjectType"] = relationship(
        back_populates="outgoing_relations",
        foreign_keys=[source_object_type_id],
    )
    target_object_type: Mapped["ObjectType"] = relationship(
        back_populates="incoming_relations",
        foreign_keys=[target_object_type_id],
    )
    mapping_object_type: Mapped["ObjectType | None"] = relationship(
        foreign_keys=[mapping_object_type_id],
    )


class BusinessLogic(Base):
    __tablename__ = "business_logics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
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


class DraftEvidence(Base):
    __tablename__ = "draft_evidences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(100), default="datahub")
    source_ref: Mapped[str] = mapped_column(String(512), index=True)
    payload_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    ontology: Mapped["Ontology"] = relationship(back_populates="draft_evidences")


class ChangeConfirmation(Base):
    __tablename__ = "change_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(100))
    confirmation_status: Mapped[str] = mapped_column(
        String(50), default=ConfirmationStatus.PENDING.value, index=True
    )
    operator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    ontology: Mapped["Ontology"] = relationship(back_populates="change_confirmations")


class VersionRecord(Base):
    __tablename__ = "version_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class EntityChangeLog(Base):
    __tablename__ = "entity_change_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(100))
    operator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DraftGenerationTask(Base):
    __tablename__ = "draft_generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    ontology_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontologies.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class LlmServiceConfig(Base):
    __tablename__ = "llm_service_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(50), default="deepseek")
    api_base_url: Mapped[str] = mapped_column(String(512), default="https://api.deepseek.com")
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model: Mapped[str] = mapped_column(String(100))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    use_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DatahubSetting(Base):
    __tablename__ = "datahub_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    gms_url: Mapped[str] = mapped_column(String(512))
    frontend_url: Mapped[str] = mapped_column(String(512))
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    use_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
