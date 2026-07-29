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
