"""治理制品（Governance Artifact）。

ontoMeta 现有的本体生产方式是「LLM 草稿 → 校验 → 人工确认 → 版本化发布 → 溯源」。
本表把这套机制从「本体」一种制品泛化到五种——集群拓扑、同步作业、ETL 任务、
指标任务共用同一条流水线，不新建框架。

**LLM 只产声明式 Spec，不产命令**；执行由确定性 Executor 完成，可测试、可回滚、可审计。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._provenance import ProvenanceMixin


def _uuid() -> str:
    return str(uuid.uuid4())


class ArtifactKind(str, enum.Enum):
    # 物化与同步是本体两种来源的两种去处，不要混为一谈：
    #   物化 = 把本体建成物理表（只出 DDL）。人工建模的本体只有元数据，必须先物化出表给业务用。
    #   同步 = 确保目标表并把源库已有的数据搬进来。同步成功后该表直接成为本体 serving 表。
    # 二者都编译成 Flink SQL / DDL 交 Airflow 执行（统一执行架构），本进程不落库。
    SYNC = "sync"  # 数据同步 → 幂等建目标表 + 搬数据
    TRANSFORM = "transform"  # ETL 任务 → Flink SQL
    METRIC = "metric"  # 指标任务 → 聚合 Flink SQL
    MATERIALIZE = "materialize"  # 本体物化 → 只出建表 DDL，不搬数据、不触碰已有数据


# 高危制品：执行不可逆的制品必须展示 dry-run 差异后才可确认。
# 目前为空——原唯一高危项 cluster（Bigtop Manager 部署）已移除。保留常量与
# is_high_risk/流水线闸门接线不动：将来若要把 materialize 等列为高危，只需在此加回。
HIGH_RISK_KINDS: frozenset[str] = frozenset()


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
    # 可选的本体归属：部分制品（如手填 sync）可能无本体，故不设外键约束。
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


class PipelineStatus(str, enum.Enum):
    """任务链的整体状态。**由各步制品聚合推导，不独立维护**——两处状态迟早分叉。"""

    DRAFTED = "drafted"  # 已建链，还没起草任何一步
    RUNNING = "running"  # 有步骤在走（已起草但未全部成功）
    SUCCEEDED = "succeeded"  # 每一步都成功
    FAILED = "failed"  # 某一步执行失败，链停在那里


class GovernanceTaskPipeline(Base):
    """任务链：把「物化 → 清洗 → 聚合」这种前后相继的多个任务串成一条可编排的东西。

    **链只管顺序与上下文传递，不碰治理门槛**：每一步仍是一条独立的 GovernanceArtifact，
    照旧各自走「校验 → dry-run → 人工确认 → 执行」。链做的是两件此前只能靠人肉完成的事——
    ①记住下一步是什么；②把上游已经定下的选项（目标数据源/库/引擎）接到下游，不必逐步重问。

    「未确认不得执行」因此**逐制品仍然成立**：链不会替谁确认，也不会跳过任何一步的 dry-run。

    形态是**线性链**，不是 DAG。用户要的是「物化完清洗、清洗完聚合」这种前后相继；扇出/汇聚
    的真正去处是把整条链编译成一条 Airflow DAG（下一步），在这里做半个调度器只会两头不到岸。
    """

    __tablename__ = "governance_task_pipelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), default="")
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ontology_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # P2：编译成周期 DAG 后的状态。链态仍由各步制品聚合推导（不落第二份），但「已挂成
    # 周期任务」是链级事实、无处可聚合，故在此落：schedule_cron 是挂的 cron，compiled_dag_id
    # 是编译出的 DAG，compiled_at 是编译时间。任一步 spec 确认后变动，compiled_dag_id 失效、需重编。
    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    compiled_dag_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    steps: Mapped[list["GovernanceTaskPipelineStep"]] = relationship(
        back_populates="pipeline",
        order_by="GovernanceTaskPipelineStep.step_index",
        cascade="all, delete-orphan",
    )


class GovernanceTaskPipelineStep(Base):
    """链上的一步：**先是一份待起草的意图，起草后才有制品**。

    下游不能在建链时就起草：它的 context 要等上游真的跑完才配得齐（上游落到哪个库、建了哪张
    表）。故这里存「打算做什么」，``artifact_id`` 在轮到它时才由 advance() 填上——那一刻上游
    的 spec 与回执都已存在，继承来的取值才是事实而不是预测。
    """

    __tablename__ = "governance_task_pipeline_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pipeline_id: Mapped[str] = mapped_column(
        ForeignKey("governance_task_pipelines.id"), index=True
    )
    # 从 0 起的执行序。线性链：第 n 步等第 n-1 步成功。
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30))
    intent: Mapped[str] = mapped_column(Text, default="")
    # P3-2：显式依赖的上游步序列表（JSON 数组）。空/None = 沿用线性默认（依赖上一步）。
    # 给了则支持扇出/汇聚：一个上游分叉到多个下游（扇出）、多个上游汇到一个下游（汇聚）。
    # 提案层仍以线性为主（易读易起草），分叉只在编译成 DAG 时生效。
    depends_on_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本步显式给定的 context；起草时与上游继承来的合并，**显式的优先**。
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 软引用（不设 FK）：制品的权威在 agent 流水线，这里只记「这一步落成了哪条制品」。
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    pipeline: Mapped["GovernanceTaskPipeline"] = relationship(back_populates="steps")
