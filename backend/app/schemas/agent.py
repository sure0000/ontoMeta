from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ArtifactDraftRequest(BaseModel):
    kind: str = Field(description="sync / transform / metric / materialize")
    # 意图驱动路径必填；给了 spec 的手动结构化路径可空。
    intent: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    ontology_id: str | None = None
    # 手动结构化起草：直接给一份声明式 spec，跳过 drafter，仍进校验闸门兜底。
    spec: dict[str, Any] | None = None
    # 手填表单可直接给名；不给则由 spec 派生（name_from_spec）。
    name: str | None = None
    # 表单起草走 context+drafter 派生路径但仍是**用户发起**：置 True 让溯源标 user，
    # 而非默认的 machine（对话/机器起草）。spec 直填路径恒为 user，不看此标。
    user_created: bool = False


class ConfirmedArtifactDraftRequest(BaseModel):
    """Data Agent 三步确认表单 → 草稿 + dry-run，不再经过第二轮 LLM。"""

    conversation_id: str
    confirmation_id: str
    kind: str
    intent: str
    context: dict[str, Any] = Field(default_factory=dict)
    ontology_id: str
    message_id: str | None = None
    block_id: str | None = None


class ArtifactEditRequest(BaseModel):
    """编辑草稿/已校验/失败态的制品。给 spec 走直填覆盖，给 intent/context 走
    drafter 重新派生——与 draft() 的两条路径语义一致，不做字段级 patch（制品的
    spec 是整体派生/直填的产物，混合新旧字段会产生不一致态）。"""

    name: str | None = None
    intent: str | None = None
    context: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    ontology_id: str | None = None


class ArtifactConfirmRequest(BaseModel):
    operator: str | None = None


class ArtifactExecuteRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)


class GovernanceArtifactOut(BaseModel):
    id: str
    kind: str
    name: str
    ontology_id: str | None = None
    intent: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    status: str
    is_high_risk: bool = False
    validation_report: dict[str, Any] | None = None
    execution_receipt: dict[str, Any] | None = None
    # Airflow DagRun 实时态（best-effort 回读）。制品 status 在提交 DAG 后即 succeeded，
    # 但 DAG 在 Airflow 里可能还在跑——实时权威在 Airflow。读不到即为 None，退回 status。
    live_state: dict[str, Any] | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    origin: str
    created_at: datetime
    updated_at: datetime


class AgentKindsOut(BaseModel):
    all_kinds: list[str]
    registered: list[str]
    high_risk: list[str]


class PipelineStepInput(BaseModel):
    """建链时的一步：**只描述打算做什么**，制品要等轮到它才起草。"""

    kind: str = Field(description="materialize / sync / transform / metric")
    intent: str
    context: dict[str, Any] = Field(default_factory=dict)
    # C2：血缘依赖（上游步序列表）。agent 从血缘/意图推导；空 = 线性默认（依赖上一步）。
    depends_on: list[int] = Field(default_factory=list)


class TaskPipelineCreateRequest(BaseModel):
    name: str = ""
    intent: str | None = None
    ontology_id: str | None = None
    steps: list[PipelineStepInput] = Field(default_factory=list)


class PipelineStepOut(BaseModel):
    id: str
    step_index: int
    kind: str
    intent: str
    context: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = None
    #: 还没起草的步骤没有制品，状态如实为 null——不拿 "drafted" 冒充。
    artifact_status: str | None = None
    artifact_name: str | None = None
    # C2：血缘依赖（上游步序列表）。
    depends_on: list[int] = Field(default_factory=list)


class TaskPipelineOut(BaseModel):
    id: str
    name: str
    intent: str | None = None
    ontology_id: str | None = None
    #: 由各步制品聚合推导：drafted / running / succeeded / failed。
    status: str
    steps: list[PipelineStepOut] = Field(default_factory=list)
    #: 下一个待起草的步序；全起草完为 null。
    next_step_index: int | None = None
    #: 下一步为什么还不能起草（上游没跑成功）；能起草为 null。
    next_blocked_reason: str | None = None
    # P2: 编译成周期 DAG 的状态
    schedule_cron: str | None = None
    compiled_dag_id: str | None = None
    compiled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PipelineAdvanceConfirmedRequest(BaseModel):
    """任务链的**某一步**走完前三环确认后才起草它。

    链不替谁确认：每一步都是一条独立的数据任务，与单发任务同样要人分别确认
    需求 / 本体 / 数据，再在制品抽屉里确认执行方案 / 执行 / 结果。
    """

    conversation_id: str
    confirmation_id: str
    #: 人在向导里定下的参数；合并进该步的 context（显式的优先于链的继承值）。
    context: dict[str, Any] = Field(default_factory=dict)
    #: 人在「确认任务需求」那一环改定的需求，作为该步 intent。留空则沿用链上原意图。
    intent: str | None = None


class TaskFormRequest(BaseModel):
    """按任务类型现取一张六环确认表单（任务链逐步确认、非对话入口共用）。"""

    kind: str
    ontology_id: str
    title: str = ""
    intent: str = ""
    #: 已知取值（如链上游继承来的落点）：核对候选后填成默认值，核不上的丢弃。
    prefill: dict[str, Any] = Field(default_factory=dict)


class TaskPipelineAdvanceOut(BaseModel):
    """推进一步的结果：新起草的制品 + 推进后的链态（前端一次拿全，不用再查一遍）。"""

    pipeline: TaskPipelineOut
    artifact: GovernanceArtifactOut


class TaskPipelineDraftAllOut(BaseModel):
    """C2：一键起草全部步骤的结果：新起草的制品列表 + 链态。"""

    pipeline: TaskPipelineOut
    artifacts: list[GovernanceArtifactOut]


class PipelineScheduleRequest(BaseModel):
    """给链设置周期调度 cron。"""

    schedule_cron: str = Field(description="cron 表达式，如 '0 2 * * *'（每天凌晨 2 点）")


class PipelineCompileOut(BaseModel):
    """编译结果：链 → 一条周期 DAG。"""

    pipeline_id: str
    compiled_dag_id: str
    schedule_cron: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    dag_path: str
    spec_path: str
