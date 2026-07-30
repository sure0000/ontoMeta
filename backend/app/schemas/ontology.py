from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")


class _ProvenanceReadMixin(BaseModel):
    """读模型的字段级溯源字段，对瞬态对象的 None 做容错。"""

    origin: str = "machine"
    upstream_removed: bool = False
    has_conflict: bool = False
    pinned_fields: list[str] = Field(default_factory=list)
    conflicts: dict = Field(default_factory=dict)

    @field_validator("origin", mode="before")
    @classmethod
    def _default_origin(cls, v):
        return v or "machine"

    @field_validator("upstream_removed", "has_conflict", mode="before")
    @classmethod
    def _default_bool(cls, v):
        return bool(v)

    @field_validator("pinned_fields", mode="before")
    @classmethod
    def _default_list(cls, v):
        return v or []

    @field_validator("conflicts", mode="before")
    @classmethod
    def _default_dict(cls, v):
        return v or {}


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
    # 分类证据快照：score / needs_review / signals，供复核界面展示「判定依据」。
    role_signals: dict | None = None


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
    # 桥表塌缩：这条关系由某张关系表(bridge)承载时，填其 candidate_name 作实现表。
    mapping_object: str | None = None


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
    # 对象角色标注：business_object / data_table / bridge / technical。
    table_role: str = "business_object"
    role_confidence: float = 0.5
    role_reason: str | None = None
    role_signals: dict | None = None


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
    # 桥表塌缩：承载该关系的关系表(bridge) candidate_name，落库时回链为 mapping_object_type_id。
    mapping_object_type_name: str | None = None


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


class PropertyOut(_ProvenanceReadMixin):
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


class ObjectTypeSummary(_ProvenanceReadMixin):
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
    # 分类证据快照：score / needs_review / signals（主键、外键入度、字段占比、
    # tech_score、连通性等）。仅详情返回，供「判定依据」面板展示。
    role_signals: dict | None = None
    properties: list[PropertyOut] = Field(default_factory=list)
    outgoing_relations: list["RelationTypeOut"] = Field(default_factory=list)
    incoming_relations: list["RelationTypeOut"] = Field(default_factory=list)
    # 本对象作为关系表(bridge)时，它实现(mapping_object)的业务关系。桥表本身不是
    # 关系端点，故 outgoing/incoming 为空；这条列表让其详情图谱能显示所连的业务对象。
    implemented_relations: list["RelationTypeOut"] = Field(default_factory=list)
    business_logics: list["BusinessLogicOut"] = Field(default_factory=list)
    business_logic_bindings: list[ObjectTypeLogicBindingOut] = Field(default_factory=list)
    version_records: list["VersionRecordOut"] = Field(default_factory=list)


class RelationTypeOut(_ProvenanceReadMixin):
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


class RelationGroupOut(BaseModel):
    """按 display_name 去重后的关系分组（列表用）。

    一个 display_name（如「属于」）通常对应成百上千条 (源,目标) 三元组，
    这里把它折叠成一行，聚合展示类型/基数/置信度/复核状态。具体三元组由
    关系详情页按 display_name 精确过滤 list_relation_types 拉取。
    """

    display_name: str
    count: int
    description: str | None = None
    structure_types: list[str] = []
    cardinalities: list[str] = []
    confidence_min: float | None = None
    confidence_max: float | None = None
    statuses: list[str] = []


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


class ConflictItemOut(BaseModel):
    """单个字段级待复核冲突。"""

    entity_type: str  # object_type / property / relation_type / business_logic
    entity_id: str
    name: str
    display_name: str
    field: str
    base: Any = None
    ours: Any = None
    theirs: Any = None


class OntologyConflictsOut(BaseModel):
    ontology_id: str
    items: list[ConflictItemOut] = Field(default_factory=list)
    total: int = 0


class ConflictResolveRequest(BaseModel):
    entity_type: str
    entity_id: str
    field: str
    resolution: str  # accept_theirs | keep_ours
    operator: str | None = None


class FieldPinRequest(BaseModel):
    entity_type: str
    entity_id: str
    field: str
    pinned: bool = True
    operator: str | None = None


class MergeReportOut(BaseModel):
    task_id: str
    scope: str | None = None
    summary: dict = Field(default_factory=dict)
    object_types: dict = Field(default_factory=dict)
    properties: dict = Field(default_factory=dict)
    relation_types: dict = Field(default_factory=dict)
    business_logics: dict = Field(default_factory=dict)


class ObjectTypeUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    # 人工改判对象角色（业务对象/数据表/关系表/技术表）。
    table_role: str | None = None
    # 复核状态开关：True=标记为待复核；False=标记为已确认（清除 [待复核]）。
    needs_review: bool | None = None
    operator: str | None = None


class ObjectTypeBatchUpdate(BaseModel):
    # 批量改判对象角色与复核状态。ids 为空或全无效则不产生任何变更。
    ids: list[str] = Field(default_factory=list)
    table_role: str | None = None
    needs_review: bool | None = None
    operator: str | None = None


class ObjectTypeBatchUpdateResult(BaseModel):
    updated: int
    items: list[ObjectTypeSummary] = Field(default_factory=list)


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


class ObjectToRelationConvertIn(BaseModel):
    """把被误判为业务对象的事实/明细/动作表转成一条业务关系。

    这类表（维修/清算/交易…）每行是一次业务事实而非一个实体：真正的业务对象是它
    引用的键。转换以原表为「实现表」在两端点间建关系，原对象降级为 bridge 离开业务
    对象集（可逆）。端点必须是业务对象——非业务对象端点会被自动提升（rule1）。
    """

    source_object_type_id: str
    target_object_type_id: str
    # 关系谓词（读成「源 谓词 目标」），如「维修」「清算」；默认取原对象展示名。
    display_name: str
    description: str | None = None
    cardinality: str | None = None
    structure_type: str | None = "fact_table"
    operator: str | None = None


class ObjectToRelationConvertResult(BaseModel):
    relation: RelationTypeOut
    retired_object: ObjectTypeSummary
    # 因作为端点而被自动提升为业务对象的对象展示名（rule1），供前端提示。
    promoted_endpoints: list[str] = Field(default_factory=list)


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
    table_role: str | None = None
    needs_review: bool = False


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str
    cardinality: str | None = None
    relation_id: str | None = None
    structure_type: str | None = None


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


class ClusterDetail(BaseModel):
    """单个聚类的下钻详情：全量成员 + 簇内关系，供前端邻接矩阵视图使用。

    与 GraphCluster 不同，这里不截断成员，且携带成员之间的真实关系边
    （grouped-graph 为宏观视图刻意丢弃了簇内边）。
    """

    id: str
    name: str
    node_count: int = 0
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
