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
    # JSON 文本的引擎列表，如 ["hive","doris"]。Hive 为权威写入路径，其余由其派生。
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
