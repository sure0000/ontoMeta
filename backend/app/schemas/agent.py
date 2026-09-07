from datetime import datetime
from typing import Any, Literal

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


class ArtifactAgentApprovalRequest(BaseModel):
    """逐条给出/收回「允许外部 agent 代执行这条任务」。

    与角色正交：角色说的是"这个身份能不能做这类事"，这里说的是"这一条现在可以自动跑"。
    只有这条 REST（人在界面上点）能写，MCP 工具一律只读。
    """

    approved: bool
    operator: str | None = None


class ArtifactResultRequest(BaseModel):
    """人对执行结果的判断：符不符合预期。

    **不含 operator**：署名由服务端从已认证主体取。这条记录的全部价值就在于"是谁看过之后
    认的"，让客户端自报等于没记。
    """

    outcome: Literal["accepted", "rejected"]
    note: str | None = Field(
        default=None,
        description="不符合预期时写清哪里不对；符合时可留空",
        max_length=2000,
    )


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
    # 代执行授权（与角色正交的第二道闸，只作用于 MCP 的 confirm/execute）。
    agent_execution_approved: bool = False
    agent_execution_approved_by: str | None = None
    agent_execution_approved_at: datetime | None = None
    origin: str
    # 人对结果的判断。与 status/receipt **分列**：那边说系统这侧发生了什么，
    # 这边说人看过之后认不认。没人表态就是 None，绝不拿 status 顶上。
    result_outcome: str | None = None
    result_note: str | None = None
    result_confirmed_by: str | None = None
    result_confirmed_at: datetime | None = None
    result_via: str | None = None
    # 「机器提了什么、人改了什么、谁建的」——制品是这三件事的唯一记录，故一并出到读模型。
    # pinned_fields 由 ProvenanceMixin 从 overridden_fields 解析（最终 spec 相对
    # machine_baseline 差在哪几个顶层键）。
    pinned_fields: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_via: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentKindsOut(BaseModel):
    all_kinds: list[str]
    registered: list[str]
    high_risk: list[str]


class TaskFormRequest(BaseModel):
    """按任务类型现取一张任务表单（供非对话入口取同一份字段骨架与真实候选）。"""

    kind: str
    ontology_id: str
    title: str = ""
    intent: str = ""
    #: 已知取值：核对候选后填成默认值，核不上的丢弃。
    prefill: dict[str, Any] = Field(default_factory=dict)
