from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ArtifactDraftRequest(BaseModel):
    kind: str = Field(description="cluster / sync / transform / metric")
    intent: str
    context: dict[str, Any] = Field(default_factory=dict)
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

    kind: str = Field(description="materialize / sync / transform / metric / cluster")
    intent: str
    context: dict[str, Any] = Field(default_factory=dict)


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
    created_at: datetime
    updated_at: datetime


class TaskPipelineAdvanceOut(BaseModel):
    """推进一步的结果：新起草的制品 + 推进后的链态（前端一次拿全，不用再查一遍）。"""

    pipeline: TaskPipelineOut
    artifact: GovernanceArtifactOut
