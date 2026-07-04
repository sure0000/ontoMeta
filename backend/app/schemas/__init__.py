from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --- DataHub Input Models ---


class DomainInput(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner: str | None = None


class FieldInput(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_target: str | None = None


class DatasetInput(BaseModel):
    urn: str
    name: str
    display_name: str | None = None
    description: str | None = None
    platform: str | None = None
    container: str | None = None
    fields: list[FieldInput] = Field(default_factory=list)


class LineageInput(BaseModel):
    source_urn: str
    target_urn: str
    lineage_type: str = "table"


class LogicEvidenceInput(BaseModel):
    source_type: str
    source_ref: str
    name: str
    expression: str | None = None
    description: str | None = None


class DataHubDomainBundle(BaseModel):
    domain: DomainInput
    datasets: list[DatasetInput] = Field(default_factory=list)
    lineages: list[LineageInput] = Field(default_factory=list)
    logic_evidences: list[LogicEvidenceInput] = Field(default_factory=list)


class DataHubDatasetOption(BaseModel):
    """DataHub dataset 搜索结果（含已映射的 ObjectType 信息）。"""
    urn: str
    name: str
    display_name: str | None = None
    description: str | None = None
    platform: str | None = None
    container: str | None = None
    object_type_id: str | None = None
    object_type_display_name: str | None = None
    datahub_url: str | None = None


class EnsureObjectTypeRequest(BaseModel):
    ontology_id: str
    dataset_urn: str
    operator: str | None = None


# --- Evidence Pack Models ---


class ObjectTypeEvidencePack(BaseModel):
    candidate_name: str
    display_name: str
    description: str | None = None
    source_dataset_urn: str
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)


class PropertyEvidencePack(BaseModel):
    object_candidate_name: str
    field_name: str
    display_name: str
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)


class RelationEvidencePack(BaseModel):
    name: str
    display_name: str
    source_object: str
    target_object: str
    cardinality: str | None = None
    structure_type: str | None = None
    description: str | None = None
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)


class LogicEvidencePack(BaseModel):
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    object_types: list[ObjectTypeEvidencePack] = Field(default_factory=list)
    properties: list[PropertyEvidencePack] = Field(default_factory=list)
    relations: list[RelationEvidencePack] = Field(default_factory=list)
    business_logics: list[LogicEvidencePack] = Field(default_factory=list)


# --- LLM Draft Output ---


class DraftObjectType(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    source_ref: str | None = None
    confidence: float = 0.5


class DraftProperty(BaseModel):
    object_type_name: str
    name: str
    display_name: str
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    source_field_ref: str | None = None
    required: bool = False
    confidence: float = 0.5


class DraftRelationType(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    source_object_type_name: str
    target_object_type_name: str
    cardinality: str | None = None
    structure_type: str | None = None
    source_evidence: str | None = None
    confidence: float = 0.5


class DraftBusinessLogic(BaseModel):
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    confidence: float = 0.5


class DraftBusinessLogicObjectBinding(BaseModel):
    logic_name: str
    object_type_name: str
    role: str = "subject"
    confidence: float = 0.5


class DraftBusinessLogicPropertyBinding(BaseModel):
    logic_name: str
    object_type_name: str
    field_name: str
    role: str = "input"
    confidence: float = 0.5


class OntologyDraftOutput(BaseModel):
    object_types: list[DraftObjectType] = Field(default_factory=list)
    properties: list[DraftProperty] = Field(default_factory=list)
    relation_types: list[DraftRelationType] = Field(default_factory=list)
    business_logics: list[DraftBusinessLogic] = Field(default_factory=list)
    business_logic_object_bindings: list[DraftBusinessLogicObjectBinding] = Field(default_factory=list)
    business_logic_property_bindings: list[DraftBusinessLogicPropertyBinding] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


# --- API Response Schemas ---


class DomainContextSummary(BaseModel):
    id: str
    datahub_domain_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    status: str
    draft_count: int = 0
    published_count: int = 0
    latest_draft_at: datetime | None = None
    latest_published_at: datetime | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class DomainContextDetail(DomainContextSummary):
    datahub_url: str | None = None
    latest_ontology_id: str | None = None
    latest_ontology_status: str | None = None
    published_ontology_id: str | None = None
    published_ontology_version: int | None = None


class OntologySummary(BaseModel):
    id: str
    domain_context_id: str
    version: int
    status: str
    generated_at: datetime | None = None
    published_at: datetime | None = None
    object_type_count: int = 0
    relation_type_count: int = 0
    business_logic_count: int = 0

    model_config = {"from_attributes": True}


class PropertyOut(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    source_field_ref: str | None = None
    required: bool
    source_confidence: float | None = None
    status: str

    model_config = {"from_attributes": True}


class ObjectTypeSummary(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None = None
    status: str
    property_count: int = 0
    relation_count: int = 0
    business_logic_count: int = 0
    bound_logic_count: int = 0
    source_confidence: float | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ObjectTypeLogicBindingOut(BaseModel):
    """对象视角下：这个对象作为什么角色参与了哪条业务逻辑。"""

    binding_id: str
    role: str
    source: str
    confidence: float | None = None
    logic_id: str
    logic_name: str
    logic_display_name: str
    logic_type: str
    logic_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ObjectTypeDetail(ObjectTypeSummary):
    ontology_id: str | None = None
    domain_context_id: str | None = None
    domain_name: str | None = None
    source_ref: str | None = None
    datahub_url: str | None = None
    properties: list[PropertyOut] = Field(default_factory=list)
    outgoing_relations: list["RelationTypeOut"] = Field(default_factory=list)
    incoming_relations: list["RelationTypeOut"] = Field(default_factory=list)
    business_logics: list["BusinessLogicOut"] = Field(default_factory=list)
    business_logic_bindings: list[ObjectTypeLogicBindingOut] = Field(default_factory=list)
    version_records: list["VersionRecordOut"] = Field(default_factory=list)


class RelationTypeOut(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None = None
    source_object_type_id: str
    target_object_type_id: str
    source_object_name: str | None = None
    target_object_name: str | None = None
    cardinality: str | None = None
    structure_type: str | None = None
    mapping_object_type_id: str | None = None
    mapping_object_name: str | None = None
    source_evidence: str | None = None
    status: str
    source_confidence: float | None = None

    model_config = {"from_attributes": True}


class RelationObjectRef(BaseModel):
    id: str
    name: str
    display_name: str
    source_ref: str | None = None
    datahub_url: str | None = None


class RelationTypeDetail(RelationTypeOut):
    ontology_id: str
    source_object: RelationObjectRef | None = None
    target_object: RelationObjectRef | None = None
    mapping_object: RelationObjectRef | None = None


class BusinessLogicObjectBindingOut(BaseModel):
    id: str
    business_logic_id: str
    object_type_id: str
    object_type_name: str | None = None
    object_type_display_name: str | None = None
    role: str
    source: str
    confidence: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicPropertyBindingOut(BaseModel):
    id: str
    business_logic_id: str
    property_id: str
    property_name: str | None = None
    property_display_name: str | None = None
    object_type_id: str | None = None
    object_type_name: str | None = None
    role: str
    source: str
    confidence: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicObjectBindingCreate(BaseModel):
    business_logic_id: str
    object_type_id: str
    role: str = "subject"
    operator: str | None = None


class BusinessLogicPropertyBindingCreate(BaseModel):
    business_logic_id: str
    property_id: str
    role: str = "input"
    operator: str | None = None


class BusinessLogicPropertyOption(BaseModel):
    """业务逻辑编辑器中可挑选的已发布字段候选(含所属对象信息)。"""

    property_id: str
    property_name: str
    property_display_name: str | None = None
    object_type_id: str
    object_type_name: str
    object_type_display_name: str | None = None

    model_config = {"from_attributes": True}


class BusinessLogicCreate(BaseModel):
    domain_id: str
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    operator: str | None = None


class BusinessLogicUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    logic_type: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    operator: str | None = None


class BusinessLogicImportRequest(BaseModel):
    domain_id: str
    code: str
    source_type: str = "sql"
    operator: str | None = None


class ExpressionFormatRequest(BaseModel):
    domain_id: str
    expression_draft: dict
    logic_type: str | None = None
    description: str | None = None


class ExpressionFormatResponse(BaseModel):
    expression_json: dict
    expression_summary: str


class BusinessLogicOut(BaseModel):
    id: str
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    source_type: str | None = None
    source_ref: str | None = None
    status: str
    source_confidence: float | None = None
    domain_context_id: str | None = None
    domain_name: str | None = None
    bound_object_count: int = 0
    bound_property_count: int = 0
    updated_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicRef(BaseModel):
    """关联对象上绑定的业务逻辑简要引用。"""

    id: str
    name: str
    display_name: str
    logic_type: str
    status: str

    model_config = {"from_attributes": True}


class BusinessLogicDetail(BusinessLogicOut):
    related_object_types: list[ObjectTypeSummary] = Field(default_factory=list)
    related_object_logics: dict[str, list[BusinessLogicRef]] = Field(default_factory=dict)
    related_properties: list[PropertyOut] = Field(default_factory=list)
    object_bindings: list[BusinessLogicObjectBindingOut] = Field(default_factory=list)
    property_bindings: list[BusinessLogicPropertyBindingOut] = Field(default_factory=list)
    version_records: list["VersionRecordOut"] = Field(default_factory=list)
    ontology_id: str | None = None
    available_object_types: list[ObjectTypeSummary] = Field(default_factory=list)
    available_properties: list[BusinessLogicPropertyOption] = Field(default_factory=list)


class VersionRecordOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    version: int
    diff_summary: str | None = None
    operator: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DraftProgressOut(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str | None = None
    ontology_id: str | None = None


class TaskRecordOut(BaseModel):
    id: str
    status: str
    progress: int
    message: str | None = None
    ontology_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChangeLogOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    action: str
    operator: str | None = None
    change_summary: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReviewUpdate(BaseModel):
    status: str
    operator: str | None = None


class ObjectTypeUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    operator: str | None = None


class PropertyUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    operator: str | None = None


class RelationTypeCreate(BaseModel):
    ontology_id: str
    display_name: str
    source_object_type_id: str
    target_object_type_id: str
    name: str | None = None
    description: str | None = None
    cardinality: str | None = None
    structure_type: str | None = None
    mapping_object_type_id: str | None = None
    operator: str | None = None


class RelationTypeUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    cardinality: str | None = None
    structure_type: str | None = None
    mapping_object_type_id: str | None = None
    source_object_type_id: str | None = None
    target_object_type_id: str | None = None
    operator: str | None = None


class ConfirmationCreate(BaseModel):
    ontology_id: str
    target_type: str
    target_id: str | None = None
    action_type: str
    operator: str | None = None
    reason: str | None = None
    payload: dict[str, Any] | None = None


class ConfirmationOut(BaseModel):
    id: str
    ontology_id: str
    target_type: str
    target_id: str | None = None
    action_type: str
    confirmation_status: str
    operator: str | None = None
    reason: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class GraphNode(BaseModel):
    id: str
    label: str
    display_name: str
    status: str


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str
    cardinality: str | None = None
    relation_id: str | None = None


class OntologyGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


# --- Settings ---


class LlmModelOption(BaseModel):
    id: str
    label: str
    description: str
    deprecated: bool = False


class LlmServiceConfigOut(BaseModel):
    id: str
    name: str
    provider: str
    api_base_url: str
    model: str
    is_default: bool
    enabled: bool
    use_mock: bool
    api_key_set: bool
    api_key_hint: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LlmServiceConfigDetail(LlmServiceConfigOut):
    api_key: str | None = None


class LlmServiceConfigCreate(BaseModel):
    name: str
    provider: str = "deepseek"
    api_base_url: str = "https://api.deepseek.com"
    api_key: str | None = None
    model: str
    is_default: bool = False
    enabled: bool = True
    use_mock: bool = False


class LlmServiceConfigUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None
    use_mock: bool | None = None


class DatahubSettingsOut(BaseModel):
    gms_url: str
    frontend_url: str
    token_set: bool
    token_hint: str | None = None
    use_mock: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class DatahubSettingsUpdate(BaseModel):
    gms_url: str
    frontend_url: str
    token: str | None = None
    use_mock: bool = False

