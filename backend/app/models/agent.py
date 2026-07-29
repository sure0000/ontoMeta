"""治理制品（Governance Artifact）。

ontoMeta 现有的本体生产方式是「LLM 草稿 → 校验 → 人工确认 → 版本化发布 → 溯源」。
本表把这套机制从「本体」一种制品泛化到五种——集群拓扑、同步作业、ETL 任务、
指标任务共用同一条流水线，不新建框架。

**LLM 只产声明式 Spec，不产命令**；执行由确定性 Executor 完成，可测试、可回滚、可审计。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models._provenance import ProvenanceMixin


def _uuid() -> str:
    return str(uuid.uuid4())


class ArtifactKind(str, enum.Enum):
    CLUSTER = "cluster"  # 集群拓扑 → Bigtop Manager
    SYNC = "sync"  # 同步作业 → SeaTunnel
    TRANSFORM = "transform"  # ETL 任务 → Spark SQL
    METRIC = "metric"  # 指标任务 → 聚合 SQL


# 高危制品：执行不可逆或直接改动生产集群，必须展示 dry-run 差异后才可确认。
HIGH_RISK_KINDS = frozenset({ArtifactKind.CLUSTER.value})


class ArtifactStatus(str, enum.Enum):
    DRAFTED = "drafted"
    VALIDATED = "validated"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GovernanceArtifact(Base, ProvenanceMixin):
    __tablename__ = "governance_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    # 可选的本体归属：cluster 制品无本体，故不设外键约束。
    ontology_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), default=ArtifactStatus.DRAFTED.value, index=True
    )
    # 校验报告（含 dry-run 差异），确认前必须呈现给人。
    validation_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 溯源（比照 ObjectType / MaterializationContract）
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
    def is_high_risk(self) -> bool:
        return self.kind in HIGH_RISK_KINDS
