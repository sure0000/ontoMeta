"""建模工单 Pydantic schemas。

强类型校验，extra 字段策略明确。每个 Spec 类型对应独立的 Pydantic model。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# 需求规格 (Requirement)
# ============================================================================


class RequirementSpec(BaseModel):
    """需求规格。
    
    进入 requirement_confirmed 前必须明确：
    - 业务目标
    - 至少一个业务过程或主题
    - 预期交付物
    - 时间范围或明确"不限"
    - 验收标准
    - 所有阻断型 open_questions 已清空
    """
    
    business_goal: str = Field(..., min_length=1, max_length=500)
    business_processes: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    time_scope: dict[str, Any] | None = None
    grain_expectation: str | None = None
    metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    refresh_sla: str | None = None
    delivery: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 上下文规格 (Context)
# ============================================================================


class OntologyReference(BaseModel):
    """本体引用。"""
    ontology_id: str
    version: int
    domain_id: str | None = None
    content_hash: str | None = None
    
    model_config = {"extra": "forbid"}


class DataSourceReference(BaseModel):
    """数据源引用。只保存引用、版本与哈希，不保存 DSN 或凭据。"""
    data_source_id: str
    catalog_name: str | None = None
    database: str | None = None
    mapping_hash: str | None = None
    connection_status: str | None = None
    
    model_config = {"extra": "forbid"}


class ModelingContextSpec(BaseModel):
    """上下文规格。确认"这次究竟基于什么本体和数据"。"""
    
    ontologies: list[OntologyReference] = Field(default_factory=list)
    selected_objects: list[str] = Field(default_factory=list)
    selected_relations: list[str] = Field(default_factory=list)
    selected_logics: list[str] = Field(default_factory=list)
    data_sources: list[DataSourceReference] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 维度模型规格 (DimensionalModel) - 简化第一版
# ============================================================================


class DimensionalModelSpec(BaseModel):
    """维度模型规格。
    
    第一版包含核心字段，P3 会扩展完整的事实、维度、粒度、SCD 等。
    这里先建立基础结构，让 P1-P2 可以保存和版本化。
    """
    
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    business_process: str | None = None
    ontology_refs: list[dict[str, Any]] = Field(default_factory=list)
    
    # P3 会扩展的字段（预留）
    facts: list[dict[str, Any]] = Field(default_factory=list)
    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    bridges: list[dict[str, Any]] = Field(default_factory=list)
    role_playing_dimensions: list[dict[str, Any]] = Field(default_factory=list)
    
    target: dict[str, Any] | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 逻辑包规格 (LogicBundle)
# ============================================================================


class LogicBundleItemSpec(BaseModel):
    """逻辑包中的单个项目。"""
    
    kind: Literal["metric", "tag", "rule"]
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)
    target_fact: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    subject_dimension: str | None = None
    
    model_config = {"extra": "forbid"}


class LogicBundleSpec(BaseModel):
    """逻辑包规格。批量指标/标签/规则。"""
    
    dimensional_model_revision: int | None = None
    items: list[LogicBundleItemSpec] = Field(default_factory=list)
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 交付计划规格 (Delivery)
# ============================================================================


class DeliveryPlanStepSpec(BaseModel):
    """交付计划中的单个步骤。"""
    
    id: str
    kind: str  # materialize/sync/transform/metric
    depends_on: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    
    model_config = {"extra": "forbid"}


class DeliveryPlanSpec(BaseModel):
    """交付计划规格。"""
    
    steps: list[DeliveryPlanStepSpec] = Field(default_factory=list)
    schedule_cron: str | None = None
    rollback: str | None = None
    acceptance_checks: list[str] = Field(default_factory=list)
    data_app: dict[str, Any] | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 验收规格 (Acceptance)
# ============================================================================


class AcceptanceSpec(BaseModel):
    """验收规格。"""
    
    checks: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    passed: bool = False
    notes: str | None = None
    
    model_config = {"extra": "forbid"}


# ============================================================================
# 统一规格包装
# ============================================================================


SPEC_KIND_TO_MODEL: dict[str, type[BaseModel]] = {
    "requirement": RequirementSpec,
    "context": ModelingContextSpec,
    "dimensional_model": DimensionalModelSpec,
    "logic_bundle": LogicBundleSpec,
    "delivery": DeliveryPlanSpec,
    "acceptance": AcceptanceSpec,
}


def validate_spec_payload(kind: str, payload: dict[str, Any]) -> BaseModel:
    """根据 kind 选择对应的 Pydantic model 进行校验。"""
    model_class = SPEC_KIND_TO_MODEL.get(kind)
    if model_class is None:
        raise ValueError(f"未知的 spec kind: {kind}")
    return model_class.model_validate(payload)


# ============================================================================
# API 请求/响应 schemas
# ============================================================================


class ModelingCaseCreate(BaseModel):
    """创建建模工单请求。"""
    
    title: str = Field(..., min_length=1, max_length=200)
    conversation_id: str | None = None
    primary_domain_id: str | None = None
    domain_ids: list[str] = Field(default_factory=list)
    owner_subject_id: str | None = None
    
    model_config = {"extra": "forbid"}


class ModelingCaseUpdate(BaseModel):
    """更新建模工单请求（非确认字段）。"""
    
    title: str | None = Field(None, min_length=1, max_length=200)
    owner_subject_id: str | None = None
    blocked_reason: str | None = None
    
    model_config = {"extra": "forbid"}


class ModelingCaseSpecSave(BaseModel):
    """保存新 draft revision 请求。"""
    
    payload: dict[str, Any]
    proposed_by: str | None = None
    
    model_config = {"extra": "forbid"}


class ModelingCaseSpecConfirm(BaseModel):
    """确认规格请求。"""
    
    confirmed_by: str
    content_hash: str  # 乐观锁
    
    model_config = {"extra": "forbid"}


class ModelingCaseSpecReject(BaseModel):
    """拒绝规格请求。"""
    
    reason: str = Field(..., min_length=1)
    rejected_by: str
    
    model_config = {"extra": "forbid"}


class ModelingCaseOut(BaseModel):
    """建模工单响应。"""
    
    id: str
    title: str
    conversation_id: str | None
    primary_domain_id: str | None
    domain_ids: list[str]
    stage: str
    current_revision: int
    owner_subject_id: str | None
    blocked_reason: str | None
    created_at: datetime
    updated_at: datetime
    
    # 关联信息（聚合）
    has_stale_specs: bool = False
    blocking_issues: list[str] = Field(default_factory=list)
    
    model_config = {"from_attributes": True}


class ModelingCaseSpecOut(BaseModel):
    """规格响应。"""
    
    id: str
    case_id: str
    kind: str
    revision: int
    status: str
    payload: dict[str, Any]
    content_hash: str
    based_on: list[dict[str, Any]] = Field(default_factory=list)
    validation_report: dict[str, Any] | None = None
    proposed_by: str | None
    confirmed_by: str | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    
    model_config = {"from_attributes": True}


class ModelingCaseLinkOut(BaseModel):
    """引用响应。"""
    
    id: str
    case_id: str
    ref_kind: str
    ref_id: str
    role: str
    spec_revision: int | None
    metadata: dict[str, Any] | None
    created_at: datetime
    
    model_config = {"from_attributes": True}


__all__ = [
    "RequirementSpec",
    "ModelingContextSpec",
    "DimensionalModelSpec",
    "LogicBundleSpec",
    "DeliveryPlanSpec",
    "AcceptanceSpec",
    "OntologyReference",
    "DataSourceReference",
    "LogicBundleItemSpec",
    "DeliveryPlanStepSpec",
    "SPEC_KIND_TO_MODEL",
    "validate_spec_payload",
    "ModelingCaseCreate",
    "ModelingCaseUpdate",
    "ModelingCaseSpecSave",
    "ModelingCaseSpecConfirm",
    "ModelingCaseSpecReject",
    "ModelingCaseOut",
    "ModelingCaseSpecOut",
    "ModelingCaseLinkOut",
]
