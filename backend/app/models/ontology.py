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
    diff_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
