from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    """统一分页响应：limit 为 None 表示未截断（返回全部）。"""

    items: list[T]
    total: int
    limit: int | None = None
    offset: int = 0

class ObjectTypeEvidencePack(BaseModel):
    candidate_name: str
    display_name: str
    description: str | None = None
    source_dataset_urn: str
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)
    # 对象角色标注（不依赖表名，由结构/内容/拓扑信号判定）。
    table_role: str = "business_object"
    role_confidence: float = 0.5
    role_reason: str | None = None


class PropertyEvidencePack(BaseModel):
    object_candidate_name: str
    field_name: str
    display_name: str
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    sample_values: list[str] = Field(default_factory=list)
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


class DraftObjectType(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    source_ref: str | None = None
    confidence: float = 0.5
    # 对象角色标注：business_object / data_table / bridge。
    table_role: str = "business_object"
    role_confidence: float = 0.5
    role_reason: str | None = None


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
    table_role: str = "business_object"
    role_confidence: float | None = None
    role_reason: str | None = None
    domain_context_id: str | None = None
    domain_name: str | None = None
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


class VersionRecordOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    version: int
    diff_summary: str | None = None
    operator: str | None = None
    created_at: datetime
    has_diff: bool = False
    has_snapshot: bool = False

    model_config = {"from_attributes": True}


class VersionDiffSection(BaseModel):
    added: list[dict] = Field(default_factory=list)
    removed: list[dict] = Field(default_factory=list)
    modified: list[dict] = Field(default_factory=list)


class VersionDiffOut(BaseModel):
    ontology_id: str
    version: int
    previous_version: int | None = None
    diff_summary: str | None = None
    operator: str | None = None
    created_at: datetime | None = None
    object_types: VersionDiffSection = Field(default_factory=VersionDiffSection)
    properties: VersionDiffSection = Field(default_factory=VersionDiffSection)
    relation_types: VersionDiffSection = Field(default_factory=VersionDiffSection)
    business_logics: VersionDiffSection = Field(default_factory=VersionDiffSection)


class VersionSnapshotOut(BaseModel):
    ontology_id: str
    version: int
    diff_summary: str | None = None
    created_at: datetime | None = None
    object_types: list[dict] = Field(default_factory=list)
    properties: list[dict] = Field(default_factory=list)
    relation_types: list[dict] = Field(default_factory=list)
    business_logics: list[dict] = Field(default_factory=list)


class ValidationIssueOut(BaseModel):
    code: str
    message: str
    entity_type: str
    entity_id: str | None = None
    entity_name: str | None = None


class OntologyValidationResult(BaseModel):
    ontology_id: str
    ok: bool
    issues: list[ValidationIssueOut] = Field(default_factory=list)


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
    center_id: str | None = None
    depth: int = 1
    truncated: bool = False
    total_object_count: int = 0
    total_relation_count: int = 0


class GraphPoint(BaseModel):
    """宏观布局中的稳定二维坐标（近邻间距约 1 个单位，前端按固定像素间距放大）。"""

    x: float
    y: float


class ClusterNode(BaseModel):
    """聚类内的单个 ObjectType 节点。"""

    id: str
    label: str
    display_name: str
    status: str


class GraphCluster(BaseModel):
    """一个业务子域聚类。"""

    id: str
    name: str
    nodes: list[ClusterNode] = Field(default_factory=list)
    node_count: int = 0
    truncated: bool = False
    layout: GraphPoint | None = None


class HubNode(BaseModel):
    """枢纽节点（公司、文档类型等几乎处处被引用的公共维度表）。

    它们不参与常规聚类，而是作为宏观图的"主干骨架"独立展示——各业务版块挂在其上，
    直观体现"万物如何连起来"。
    """

    id: str
    label: str
    display_name: str
    status: str
    degree: int = 0
    layout: GraphPoint | None = None


class GroupedGraphEdge(BaseModel):
    """宏观节点之间的聚合边：weight 为底层被合并的关系条数。

    source/target 既可能是聚类 id，也可能是枢纽节点 id（枢纽以自身对象 id 作为宏观节点）。
    """

    id: str
    source_cluster_id: str
    target_cluster_id: str
    weight: int = 1
    relation_ids: list[str] = Field(default_factory=list)


class OntologyGroupedGraph(BaseModel):
    clusters: list[GraphCluster] = Field(default_factory=list)
    hub_nodes: list[HubNode] = Field(default_factory=list)
    edges: list[GroupedGraphEdge] = Field(default_factory=list)
    isolated_nodes: list[ClusterNode] = Field(default_factory=list)
    total_object_count: int = 0
    total_relation_count: int = 0
