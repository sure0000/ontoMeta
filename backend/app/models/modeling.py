"""建模工单（Modeling Case）数据模型。

建模工单是对话式本体驱动建模的端到端权威载体，管理从需求到交付的完整生命周期、
版本、确认与失效关系。

**与决策账本的关系**：
- ModelingCase 是流程权威，决定当前阶段、有效版本和是否可进入下一阶段；
- ChatBiDecisionRecord 是审计观察层，记录谁接受、修改或拒绝了什么；
- 账本写失败不能让主链失败，也绝不能越权改变工单状态。

**与现有制品的关系**：
- ModelingCase 不替代 GovernanceArtifact、BusinessLogic、DataApp 的执行权威；
- 通过 ModelingCaseLink 关联这些制品，追踪交付结果；
- 制品仍各自走现有状态机，工单只聚合展示。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ModelingCaseStage(str, enum.Enum):
    """建模工单阶段。
    
    阶段是权威流程事实，因此可以落库。这与任务链"由制品聚合推导状态"不冲突——
    任务链状态仍不重复保存，工单阶段是更上层的交付里程碑。
    """
    
    COLLECTING_REQUIREMENT = "collecting_requirement"
    REQUIREMENT_CONFIRMED = "requirement_confirmed"
    CONTEXT_CONFIRMED = "context_confirmed"
    MODEL_CONFIRMED = "model_confirmed"
    PLAN_CONFIRMED = "plan_confirmed"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ModelingCase(Base):
    """建模工单主表。
    
    承载从需求到结果的权威状态、当前版本和阻断原因。
    各类规格（需求/上下文/模型/计划/验收）落在 ModelingCaseSpec 表中。
    """
    
    __tablename__ = "modeling_cases"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200))
    
    # 对话入口：可空，允许从专属页面创建
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    
    # 主域与关联域
    primary_domain_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    domain_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # 权威流程阶段
    stage: Mapped[str] = mapped_column(
        String(50), 
        default=ModelingCaseStage.COLLECTING_REQUIREMENT.value,
        index=True
    )
    
    # 当前版本号（每次确认递增）
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    
    # 负责人主体
    owner_subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    
    # 阻断原因（上游变化、校验失败等）
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    
    # 关系
    specs: Mapped[list["ModelingCaseSpec"]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="ModelingCaseSpec.revision.desc()"
    )
    
    links: Mapped[list["ModelingCaseLink"]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan"
    )
    
    dimensional_models: Mapped[list["DimensionalModel"]] = relationship(
        back_populates="modeling_case",
        cascade="all, delete-orphan"
    )


class ModelingCaseSpecKind(str, enum.Enum):
    """规格类型。
    
    每类规格对应一个设计阶段，有独立的 revision 和确认状态。
    """
    
    REQUIREMENT = "requirement"
    CONTEXT = "context"
    DIMENSIONAL_MODEL = "dimensional_model"
    LOGIC_BUNDLE = "logic_bundle"
    DELIVERY = "delivery"
    ACCEPTANCE = "acceptance"


class ModelingCaseSpecStatus(str, enum.Enum):
    """规格状态。"""
    
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    STALE = "stale"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ModelingCaseSpec(Base):
    """版本化规格表。
    
    不要把所有 JSON 永久塞在 ModelingCase 一行中。使用通用版本表：
    - DB 不因 Spec 字段增加而频繁迁移；
    - 业务层仍由 Pydantic 严格校验；
    - 可统一做版本、确认、失效、diff 和审计。
    """
    
    __tablename__ = "modeling_case_specs"
    __table_args__ = (
        # 一个工单的某类规格只能有一个相同 revision
        {"sqlite_autoincrement": True},  # SQLite 需要显式声明
    )
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), 
        ForeignKey("modeling_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), 
        default=ModelingCaseSpecStatus.DRAFT.value,
        index=True
    )
    
    # 规格内容（强类型 Pydantic schema 序列化后的 JSON）
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    
    # 内容哈希（用于检测变化与幂等）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    
    # 基于哪些上游版本（用于失效判断）
    # 格式：[{"kind": "requirement", "revision": 3, "hash": "..."}]
    based_on_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # 校验报告
    validation_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # 提出者与确认者
    proposed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    
    # 关系
    case: Mapped["ModelingCase"] = relationship(back_populates="specs")


class ModelingCaseLink(Base):
    """工单引用表。
    
    用于追踪最终交付物，不复制其权威状态。
    """
    
    __tablename__ = "modeling_case_links"
    __table_args__ = (
        # 同一个外部实体在同一个工单中只能有一个角色
        {"sqlite_autoincrement": True},
    )
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("modeling_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    # 引用类型与 ID
    ref_kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ref_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    
    # 角色：input/output/evidence/plan/result
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    
    # 关联的规格版本
    spec_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # 扩展元数据
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    
    # 关系
    case: Mapped["ModelingCase"] = relationship(back_populates="links")


__all__ = [
    "ModelingCase",
    "ModelingCaseStage",
    "ModelingCaseSpec",
    "ModelingCaseSpecKind",
    "ModelingCaseSpecStatus",
    "ModelingCaseLink",
]
