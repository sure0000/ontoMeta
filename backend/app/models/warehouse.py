"""物化契约（Materialization Contract）。

本体是一级源数据、物理表是二级投影；但「投影到哪一层、怎么增量、按什么分区、
是否留历史」这些信息本体本身不承载——物化契约就是补齐这部分的配置层。

它挂在本体实体（ObjectType / RelationType / BusinessLogic）上，随本体一起版本化，
并携带完整溯源字段参与三方合并：机器每次重新推导默认值时，**不得覆盖人工钉住的字段**
（见 ``services/materialization_contract.py``）。
"""

import enum
import json
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models._provenance import ProvenanceMixin


def _uuid() -> str:
    return str(uuid.uuid4())


class TargetKind(str, enum.Enum):
    """契约挂载的本体实体类型。"""

    OBJECT_TYPE = "object_type"
    RELATION_TYPE = "relation_type"
    BUSINESS_LOGIC = "business_logic"


class MaterializationLayer(str, enum.Enum):
    """目标分层。

    注意：分层在本架构中**不是建模范式**，只是物化契约的一个属性——
    本体的对象/关系图才是模型主轴。
    """

    DIM = "dim"
    DWD = "dwd"
    DWS = "dws"
    ADS = "ads"


class LoadStrategy(str, enum.Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    CDC = "cdc"


class ScdType(str, enum.Enum):
    NONE = "none"
    SCD1 = "scd1"
    SCD2 = "scd2"


class OntologyWarehouseDeployment(Base):
    """Auditable deployment of one published ontology version to Doris."""

    __tablename__ = "ontology_warehouse_deployments"
    __table_args__ = (
        UniqueConstraint("ontology_id", "ontology_version", name="uq_ontology_warehouse_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"))
    ontology_version: Mapped[int] = mapped_column()
    doris_datasource_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    materialization_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)


class WarehouseObjectProjection(Base):
    """Object-level logical-to-physical mapping for a deployment version."""

    __tablename__ = "warehouse_object_projections"
    __table_args__ = (
        UniqueConstraint("deployment_id", "object_type_id", name="uq_warehouse_projection_object"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("ontology_warehouse_deployments.id"))
    object_type_id: Mapped[str] = mapped_column(ForeignKey("object_types.id"))
    ods_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ods_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serving_layer: Mapped[str | None] = mapped_column(String(20), nullable=True)
    serving_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serving_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    column_mapping_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    sync_status: Mapped[str] = mapped_column(String(20), default="empty", server_default="empty")
    transform_status: Mapped[str] = mapped_column(String(20), default="not_required", server_default="not_required")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    queryable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)


class WarehouseLogicProjection(Base):
    """BusinessLogic metric/tag/rule projection in Doris ADS."""

    __tablename__ = "warehouse_logic_projections"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "business_logic_id",
            name="uq_warehouse_logic_projection",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_warehouse_deployments.id"), index=True
    )
    business_logic_id: Mapped[str] = mapped_column(
        ForeignKey("business_logics.id"), index=True
    )
    serving_database: Mapped[str] = mapped_column(String(255))
    serving_table: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    queryable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )


class IngestionContract(Base):
    """Source table → default Doris ODS ingestion contract."""

    __tablename__ = "ingestion_contracts"
    __table_args__ = (
        UniqueConstraint(
            "ontology_id", "ontology_version", "object_type_id",
            name="uq_ingestion_contract_object_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    ontology_version: Mapped[int] = mapped_column()
    object_type_id: Mapped[str] = mapped_column(ForeignKey("object_types.id"), index=True)
    source_datasource_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    source_physical_table: Mapped[str] = mapped_column(String(512))
    source_mapping_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    doris_datasource_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    target_ods_database: Mapped[str] = mapped_column(String(255))
    target_ods_table: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(20), default="full", server_default="full")
    primary_keys_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    incremental_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    initial_watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    late_arrival_policy: Mapped[str] = mapped_column(
        String(30), default="strict", server_default="strict"
    )
    idempotency_strategy: Mapped[str] = mapped_column(
        String(50), default="primary_key_upsert", server_default="primary_key_upsert"
    )
    delete_policy: Mapped[str] = mapped_column(String(30), default="ignore", server_default="ignore")
    refresh_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flink_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    flink_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    savepoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )


class WarehouseMigrationBatch(Base):
    """Auditable Phase 6 production migration/cut-over batch.

    The batch is a control-plane record only: write-side execution remains in
    GovernanceArtifact/Airflow/Flink.  It stores approvals and evidence without
    rewriting historical artifacts or receipts.
    """

    __tablename__ = "warehouse_migration_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    ontology_version: Mapped[int] = mapped_column()
    doris_datasource_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(30), default="preparing", server_default="preparing", index=True
    )
    current_step: Mapped[int] = mapped_column(default=0, server_default="0")
    approver: Mapped[str] = mapped_column(String(255))
    rollback_owner: Mapped[str] = mapped_column(String(255))
    observation_window_minutes: Mapped[int] = mapped_column()
    legacy_dag_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_dag_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observation_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    legacy_stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rollback_drill_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    compatibility_items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )


class WarehouseMigrationEvidence(Base):
    """Immutable evidence attempt for one ordered Phase 6 step."""

    __tablename__ = "warehouse_migration_evidence"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "step", "attempt", name="uq_warehouse_migration_step_attempt"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("warehouse_migration_batches.id"), index=True
    )
    step: Mapped[int] = mapped_column()
    attempt: Mapped[int] = mapped_column(default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(20))  # pass / fail
    report_json: Mapped[str] = mapped_column(Text)
    artifact_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(255))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )


class DerivedDefinition(Base):
    """派生对象的定义：这个业务对象是由数仓里哪几张表、按什么粒度算出来的。

    **为什么派生对象要有实体身份，而 ODS/DWD 的 1:1 落点不要**：判据是粒度。源表搬进
    ODS、ODS 清洗成 DWD，一行代表的东西没变，那是同一个实体的另一个落点；而多表 join
    出的宽表、汇总表、快照表，一行代表的东西变了（从「一张订单」变成「订单×商品×日」），
    那就是一个新的业务概念——它必须在本体里有名字，否则下游没有任何东西可引用。

    但它**仍在同一个本体里**：一域一本体，再造一个「数仓本体」会让关系图、字段权威与
    发布门闸各自分叉（见 ONTOLOGY_LIFECYCLE_REDESIGN.md）。

    **派生必须是声明，不是推断。** 上游、粒度、连接条件都由人显式写下来，不靠扫描 Doris
    反推——反推出来的「新对象」正是重复对象的来源（见 services/unmodeled_tables）。

    血缘存在这里而**不是** RelationType：业务关系有「两端都必须是 business_object」的
    角色门闸，把加工血缘混进去会在下一次全量合并时被当成不合规关系剥掉。
    """

    __tablename__ = "derived_definitions"
    __table_args__ = (
        UniqueConstraint("object_type_id", name="uq_derived_definition_object"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    object_type_id: Mapped[str] = mapped_column(
        ForeignKey("object_types.id"), index=True
    )
    # 粒度声明：一行代表什么。**必填**——它就是「该不该是新实体」的判据本身，
    # 允许留空等于允许人把一个 1:1 落点包装成新对象，重复对象由此重新长出来。
    grain: Mapped[str] = mapped_column(Text)
    # 上游数据集引用（``dataset_catalog`` 的 ref），JSON 数组，**第一个是主表**。
    # 存引用而不是物理表名：表名会随契约变，引用指存储槽位不变。
    upstream_refs_json: Mapped[str] = mapped_column(Text)
    # 连接条件：[{"left_ref":…,"right_ref":…,"how":"inner|left","on":[{"left":…,"right":…}]}]
    joins_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 字段来源：[{"property":…,"from_ref":…,"from_column":…}]，供 P3 生成 SELECT 列表。
    field_mapping_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )


class MaterializationContract(Base, ProvenanceMixin):
    __tablename__ = "materialization_contracts"
    __table_args__ = (
        UniqueConstraint(
            "ontology_id",
            "target_kind",
            "target_id",
            name="uq_materialization_contract_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    # 指向本体实体；不设 FK，因为跨三张表（object_types/relation_types/business_logics）。
    target_kind: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[str] = mapped_column(String(36), index=True)

    target_layer: Mapped[str] = mapped_column(
        String(20), default=MaterializationLayer.DIM.value
    )
    # JSON 文本的历史字段；新契约固定为 ["doris"]。旧值只供审计，不参与新执行路由。
    target_engines: Mapped[str | None] = mapped_column(Text, nullable=True)
    load_strategy: Mapped[str] = mapped_column(
        String(20), default=LoadStrategy.FULL.value
    )
    partition_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scd_type: Mapped[str] = mapped_column(String(20), default=ScdType.NONE.value)
    refresh_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # False = 该实体不落物理表（如 technical 表、foreign_key 型关系）。
    materialized: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    # 机器推导时记录的判定依据，供人工复核；不参与三方合并。
    derivation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 字段级溯源与三方合并元数据（比照 ObjectType / Property）。
    origin: Mapped[str] = mapped_column(
        String(30), default="machine", server_default="machine"
    )
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_created: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    deleted_by_user: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    upstream_removed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    last_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def engines(self) -> list[str]:
        """``target_engines`` 的结构化视图，供 Pydantic ``from_attributes`` 直接映射。

        比照 ``ProvenanceMixin.pinned_fields`` 的做法，避免在每个序列化点重复解析。
        """
        if not self.target_engines:
            return []
        try:
            data = json.loads(self.target_engines)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(x) for x in data] if isinstance(data, list) else []
