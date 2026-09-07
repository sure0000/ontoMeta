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


#: 已经有结果、可以谈"结果符不符合预期"的状态。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {ArtifactStatus.SUCCEEDED.value, ArtifactStatus.FAILED.value}
)

#: 人对结果的取态。只有两个值——"跑完了没有"已经由 status 回答，这里回答的是"对不对"。
RESULT_OUTCOMES: frozenset[str] = frozenset({"accepted", "rejected"})


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

    # 谁、从哪个入口建的这条任务。
    #
    # **制品是「机器提了什么、人定了什么」的唯一记录**（此前另有一张按会话组织的决策
    # 账本随旧对话入口一起退场）。既然唯一，创建时刻的身份就不能是空的——
    # ``origin`` 只分得出 machine/user，分不出是谁、是 Web 还是外部 agent 经 MCP 建的。
    #
    # ``created_via`` 取 ``AuthContext.client_type`` 的同一套词：frontend / mcp_local /
    # mcp_remote / api。两个入口写同一个字段，才谈得上"这条是哪个 agent 做的"。
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_via: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    # 人对执行结果的判断：**跑完了**和**跑对了**是两件事。
    #
    # ``status`` / ``execution_receipt_json`` 说的是系统这一侧发生了什么（DAG 提交成功、
    # Airflow 终态、写了多少行）；这几列说的是人看过之后认不认。系统答不了后者——回执自陈
    # 成功而数据其实没搬对，是这套东西真实出过的事。故两者分列，**任何时候都不许拿 status
    # 自动填 outcome**：没人表态就是没人表态，如实空着。
    #
    # 表态从哪儿来：人在任务详情里点，或通用 agent 按 skill 问出来后经 confirm_task_result
    # 回写。``result_via`` 记的就是这个区别（词汇与 ``created_via`` 同一套）——经 agent 转述
    # 的答复和人自己点的，可信度不一样，读的人有权知道。
    #
    # 只在终态（succeeded/failed）可写：还没有结果时谈"结果符不符合预期"没有意义。
    # 可改判（覆盖写），与 confirmed_by 一样只答"最近一次"。
    result_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    result_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_via: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 代执行授权：**与角色正交的第二道闸**，只挡 MCP（外部 agent）那条路。
    #
    # 角色是「这个身份能不能做这类事」，一发就长期有效；而「这一条任务现在可以让
    # agent 自己确认并推到远端」是另一个决定，得逐条给。此前只有角色一道闸：一个
    # publisher 令牌加一句话就能把任务推到远端 Airflow 真跑起来。
    #
    # 只能从 REST（人在界面上点）写，任何 MCP 工具都不得设置它——否则 agent 就能
    # 自己给自己发许可，闸门等于不存在。
    agent_execution_approved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    agent_execution_approved_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    agent_execution_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # 溯源（比照 ObjectType / MaterializationContract）
    origin: Mapped[str] = mapped_column(
        String(30), default="machine", server_default="machine"
    )
    # 最终 spec 相对 ``machine_baseline`` 差在哪几个顶层键（JSON 字符串数组，与
    # ObjectType 那套同形）。粒度取顶层键是刻意的：spec 的顶层键就是人在表单上看到的
    # 那几格（source / target / mode / primary_keys / refresh_cron…），再往下钻只会
    # 把 drafter 派生出的内部结构当成"人改的"。
    #
    # 由 ``agent_pipeline.edit()`` 落，``confirm()`` 据此定 origin：空 = 原样接受机器
    # 提案，非空 = 接受但改过参数。**这是"用户的选择"在库里唯一的落点**，别让它再变回
    # 声明了没人写的死字段。
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
