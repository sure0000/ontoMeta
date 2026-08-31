import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


from app.models._provenance import ProvenanceMixin


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
    """数据域的**唯一**本体行：既是草稿工作台，也是发布载体。

    一域一本体（``uq_ontology_domain_context``）是这套治理闭环的地基：发布只是把这一行
    的实体提升 + 打版本快照，工作台不会被抽走，再生成继续合并进同一行，人工修订的
    三方合并基线因此跨发布边界连续。写侧一律经
    ``services.ontology_workspace.get_or_create_working_ontology`` 取行。
    """

    __tablename__ = "ontologies"
    __table_args__ = (
        # SQLite reflects a unique index separately from a UNIQUE constraint.
        # Keep metadata aligned with the cross-dialect migration representation.
        Index("uq_ontology_domain_context", "domain_context_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)
    # 草稿演进计数：每次生成运行/合并递增，独立于 publish 时才 +1 的 version。
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    segments: Mapped[list["OntologySegment"]] = relationship(back_populates="ontology")


class OntologySegment(Base, ProvenanceMixin):
    """业务板块：本体对象按关系紧密度自动聚类的业务子域。

    板块是对象分组的单位，提供业务地图视图的骨架。使用锚点成员进行身份匹配，
    支持重新生成时的增量更新。
    """
    __tablename__ = "ontology_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 锚点：度数最高的 K 个成员的 source_ref（JSON 数组），重算时的对齐键
    anchor_refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer)
    # ProvenanceMixin 字段
    origin: Mapped[str] = mapped_column(String(30), default="machine", server_default="machine")
    machine_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_created: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    deleted_by_user: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    upstream_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontology: Mapped["Ontology"] = relationship(back_populates="segments")
    members: Mapped[list["ObjectType"]] = relationship(back_populates="segment")


class ObjectType(Base, ProvenanceMixin):
    __tablename__ = "object_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    segment_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_segments.id"), nullable=True, index=True
    )
    is_hub: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_term_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    # DataHub profiling：导入时沉淀的行数，供阶梯式加载/接地判断直接读取，减少源库查询。
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 字段级溯源与三方合并元数据（见 ONTOLOGY_VERSIONING_PLAN.md）。
    origin: Mapped[str] = mapped_column(String(30), default="machine", server_default="machine")
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_created: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    deleted_by_user: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    upstream_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已发布内容被人工直接改动、尚未固化成新版本。A 案下人工编辑不再把已发布实体
    # 退回 edited（改动立即对外生效），这个标记就是「待固化」在库里的唯一凭据——
    # 靠 updated_at 与 published_at 比时间戳做不到：SQLite 的 CURRENT_TIMESTAMP 只有
    # 秒级精度，同秒内的发布与编辑分辨不出来。publish() 提升实体时清零。
    has_unpublished_change: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    # 对象角色标注（不依赖表名，预生成时由结构/内容/拓扑信号判定）：
    # business_object / data_table / bridge / technical。role_reason 可追溯，供人工在工作区确认。
    table_role: Mapped[str] = mapped_column(
        String(50), default="business_object", index=True
    )
    role_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    role_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 复核状态：与实体生命周期状态、与 role_reason 文本都**正交**的独立开关，
    # 也是部分发布的唯一门闸（True 的业务对象不随本体发布）。
    #
    # 历史上它是 role_reason 的 "[待复核]" 前缀，而 role_reason 又是可合并字段 →
    # 人工确认会把该字段永久钉住、机器换个措辞就产生一条「角色依据」冲突，点
    # 「采纳上游」还会把前缀写回、静默把对象重新打成待复核并踢出下次发布集。
    # 拆成独立列后：role_reason 回归纯描述（机器可持续刷新），复核状态只由人改。
    # 机器只在**新建**对象时给初值，再生成不回写——人的确认不会被机器推翻。
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    # 分类证据快照（JSON 文本）：score / needs_review / signals（主键、外键入度、
    # 字段占比、tech_score、连通性等），供复核界面展示「判定依据」。机器每次生成
    # 重算并直接覆盖，非用户可编辑，不参与三方合并（比照 role_confidence）。
    role_signals: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    ontology: Mapped["Ontology"] = relationship(back_populates="object_types")
    segment: Mapped["OntologySegment | None"] = relationship(back_populates="members")
    properties: Mapped[list["Property"]] = relationship(back_populates="object_type")
    outgoing_relations: Mapped[list["RelationType"]] = relationship(
        back_populates="source_object_type",
        foreign_keys="RelationType.source_object_type_id",
    )
    incoming_relations: Mapped[list["RelationType"]] = relationship(
        back_populates="target_object_type",
        foreign_keys="RelationType.target_object_type_id",
    )

    @property
    def source_provenance(self) -> str:
        """对象来源：``datahub``（有物理源表，可同步）/ ``manual`` / ``none``。

        以派生属性提供而非落列：它完全由 ``source_ref`` 的形态决定，存一份就会与
        ``source_ref`` 分叉。所有 ``from_attributes`` 的读模型声明同名字段即可自动带上，
        前端因而不必再自己解析 URN 语法。
        """
        from app.services.source_ref import provenance_of

        return provenance_of(self.source_ref)

    # 对象标识名在本体内唯一。``name`` 是 Agent 写 SQL 用的标识符，也是投影
    # （ontology_projection）按归一小写建索引的键——重名会让 ``object_of`` 静默
    # 只解析到其中一个，SQL 打到哪张表全看入库顺序。此前只在发布期由
    # ``validate_ontology`` 判「对象标识重复」，从入库到发布之间的窗口无人把关。
    #
    # 注：软删除（``deleted_by_user=True``）的行仍占用名字。这是刻意的——被人工
    # 删除的对象不会被机器复活（见 ontology_merge），名字留着可避免同名对象在
    # 「删了又生成」之间反复横跳。
    __table_args__ = (
        UniqueConstraint("ontology_id", "name", name="uq_object_type_ontology_name"),
    )


class Property(Base, ProvenanceMixin):
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
    # DataHub profiling：导入时沉淀，供阶梯式加载直接读取，减少源库查询。
    # sample_values_json: JSON 文本，反序列化为 list[str]（最多 5 个样例值）。
    sample_values_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unique_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    # 字段级溯源与三方合并元数据。
    origin: Mapped[str] = mapped_column(String(30), default="machine", server_default="machine")
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_created: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    deleted_by_user: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    upstream_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已发布内容被人工直接改动、尚未固化成新版本。A 案下人工编辑不再把已发布实体
    # 退回 edited（改动立即对外生效），这个标记就是「待固化」在库里的唯一凭据——
    # 靠 updated_at 与 published_at 比时间戳做不到：SQLite 的 CURRENT_TIMESTAMP 只有
    # 秒级精度，同秒内的发布与编辑分辨不出来。publish() 提升实体时清零。
    has_unpublished_change: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    object_type: Mapped["ObjectType"] = relationship(back_populates="properties")


class RelationType(Base, ProvenanceMixin):
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
    # 稳定身份键：urn(src)|urn(tgt)|structure_type，合并匹配用，不随可变的 name 变化。
    source_signature: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    # 复核状态：与 object_types 同理，关系也需要人工复核（特别是「修边」是修板块的主入口）
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    status: Mapped[str] = mapped_column(String(50), default=EntityStatus.SUGGESTED.value, index=True)
    # 字段级溯源与三方合并元数据。
    origin: Mapped[str] = mapped_column(String(30), default="machine", server_default="machine")
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_created: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    deleted_by_user: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    upstream_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已发布内容被人工直接改动、尚未固化成新版本。A 案下人工编辑不再把已发布实体
    # 退回 edited（改动立即对外生效），这个标记就是「待固化」在库里的唯一凭据——
    # 靠 updated_at 与 published_at 比时间戳做不到：SQLite 的 CURRENT_TIMESTAMP 只有
    # 秒级精度，同秒内的发布与编辑分辨不出来。publish() 提升实体时清零。
    has_unpublished_change: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
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
