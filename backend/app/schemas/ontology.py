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
    # 机器给出的复核建议（仅在对象**新建**时落库，再生成不回写人工确认）。
    needs_review: bool = False
    # 分类证据快照：score / needs_review / signals，供复核界面展示「判定依据」。
    role_signals: dict | None = None
    # DataHub profiling：导入时沉淀，供落库到 ObjectType 表。
    row_count: int | None = None


class PropertyEvidencePack(BaseModel):
    object_candidate_name: str
    field_name: str
    display_name: str
    description: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    sample_values: list[str] = Field(default_factory=list)
    unique_count: int | None = None
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
    # 机器给出的复核建议（仅在对象**新建**时落库，再生成不回写人工确认）。
    needs_review: bool = False
    # DataHub profiling：导入时沉淀，供落库到 ObjectType 表。
    row_count: int | None = None
    role_signals: dict | None = None
    is_hub: bool = False


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
    # DataHub profiling：导入时沉淀，供落库到 Property 表。
    sample_values: list[str] = Field(default_factory=list)
    unique_count: int | None = None


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


class DraftSegment(BaseModel):
    """业务板块草稿：本体对象按关系紧密度自动聚类的业务子域。"""
    name: str
    display_name: str
    #: business = 聚类得出的业务模块（名字来自 LLM）；其余为兜底板块，名字固定。
    #: 取值见 services/segment_kinds。划分是全覆盖分区，每个对象恰好属于一个板块。
    kind: str = "business"
    description: str | None = None
    # 锚点：度数最高的 K 个成员的 source_ref（JSON 序列化后的列表）
    anchor_refs: list[str] = Field(default_factory=list)
    member_count: int
    # 机械名（度数最高成员的 display_name），用于重算时对齐
    machine_baseline: str | None = None
    # 成员对象名列表（仅生成时使用，不落库）
    members: list[str] = Field(default_factory=list)


class OntologyDraftOutput(BaseModel):
    object_types: list[DraftObjectType] = Field(default_factory=list)
    properties: list[DraftProperty] = Field(default_factory=list)
    relation_types: list[DraftRelationType] = Field(default_factory=list)
    business_logics: list[DraftBusinessLogic] = Field(default_factory=list)
    business_logic_object_bindings: list[DraftBusinessLogicObjectBinding] = Field(default_factory=list)
    business_logic_property_bindings: list[DraftBusinessLogicPropertyBinding] = Field(default_factory=list)
    segments: list[DraftSegment] = Field(default_factory=list)
    hub_nodes: list[str] = Field(default_factory=list)  # 枢纽节点名列表
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


class ObjectLandingOut(BaseModel):
    """对象的物理落点（见 ``services/object_landing``）。

    ``state`` 由后端汇总，前端直接渲染——判定放到前端就会出现第二份口径。
    """

    state: str
    ods_table: str | None = None
    ods_status: str | None = None
    ods_mode: str | None = None
    serving_table: str | None = None
    serving_layer: str | None = None
    serving_status: str | None = None
    schema_status: str | None = None
    queryable: bool = False
    last_success_at: datetime | None = None
    materialization_artifact_id: str | None = None

    model_config = {"from_attributes": True}


class LogicLandingOut(BaseModel):
    """业务口径的 ADS 落点。指标任务产出的表挂在口径上，不是业务对象。"""

    state: str
    serving_table: str | None = None
    status: str | None = None
    queryable: bool = False
    last_success_at: datetime | None = None

    model_config = {"from_attributes": True}


class DatasetOut(BaseModel):
    """数仓里一张已被本体认领的物理表（见 ``services/dataset_catalog``）。

    ``ref`` 是稳定句柄，任务 Spec / 前端表单存的都是它；``physical`` 只用于展示与复制，
    **不要**拿它回填进 Spec——表名会随契约变，引用不会。
    """

    ref: str
    entity_kind: str
    entity_id: str
    entity_name: str
    entity_display_name: str
    slot: str
    layer: str
    physical: str
    state: str
    queryable: bool
    source_ready: bool
    mode: str | None = None
    last_success_at: datetime | None = None

    model_config = {"from_attributes": True}


class DerivedJoinCondition(BaseModel):
    left: str
    right: str


class DerivedJoinInput(BaseModel):
    """把 ``right_ref`` 接到已在图里的 ``left_ref`` 上。``on`` 为空即笛卡尔积，服务端拒。"""

    left_ref: str
    right_ref: str
    on: list[DerivedJoinCondition]
    how: str = "inner"


class DerivedFieldInput(BaseModel):
    """派生对象的一个属性来自哪个上游的哪一列。"""

    property: str
    from_ref: str
    from_column: str
    display_name: str | None = None


class DerivedObjectCreate(BaseModel):
    """由数仓里的若干数据集派生一个新粒度的业务对象。

    ``grain`` 必填：它就是「该不该建新对象」的判据——粒度没变的加工只是既有对象的另一个
    落点，不该在本体里多出一个实体。
    """

    name: str
    display_name: str
    grain: str
    upstream_refs: list[str]
    fields: list[DerivedFieldInput]
    description: str | None = None
    joins: list[DerivedJoinInput] = []
    layer: str = "dwd"
    notes: str | None = None


class DerivedObjectCreated(BaseModel):
    object_type_id: str
    name: str
    display_name: str
    ontology_id: str
    layer: str
    upstream_refs: list[str]


class DerivedDefinitionOut(BaseModel):
    """派生定义 + 上游此刻的落点状态。"""

    object_type_id: str
    grain: str
    layer: str | None = None
    upstreams: list[DatasetOut] = []
    # 已解析不到的上游引用（上游被删/被降级）。**要显示**：少一个上游的定义看起来
    # 仍然成立，跑起来才发现少了一张表。
    dangling_refs: list[str] = []
    joins: list[dict] = []
    field_mapping: list[dict] = []
    notes: str | None = None


class UnclaimedTableOut(BaseModel):
    """数仓里存在、本体里没人认领的一张表（见 ``services/unclaimed_tables``）。"""

    database: str
    table: str
    physical: str
    # 由库名推断的分层；推不出就是 None（不猜——层会写进落点登记）。
    layer: str | None = None

    model_config = {"from_attributes": True}


class UnclaimedTablesOut(BaseModel):
    items: list[UnclaimedTableOut] = []
    # 实际扫过的库。少扫了哪个（库不存在/连不上）要看得见，否则「没有无主表」既可能是
    # 真的干净，也可能是压根没扫到。
    scanned_databases: list[str] = []


class ClaimTableRequest(BaseModel):
    """把一张无主表登记为某个已有对象的落点。**不新建对象**。"""

    object_type_id: str
    database: str
    table: str
    datasource_id: str | None = None


class ObjectTypeSummary(_ProvenanceReadMixin):
    id: str
    name: str
    display_name: str
    description: str | None = None
    # 源表定位（DataHub urn）。列表里带上它，是因为「能不能建同步任务」要在前端就判出来，
    # 不给的话只能让人选完提交才在 drafter 里被拒。
    source_ref: str | None = None
    # 判「能不能同步」用这个，不要用 `source_ref` 是否为空：人工建模对象的 source_ref
    # 是 `manual:<源>:<标识>`，非空却没有任何物理表可搬。取值见 services/source_ref。
    source_provenance: str = "none"
    status: str
    property_count: int = 0
    relation_count: int = 0
    business_logic_count: int = 0
    bound_logic_count: int = 0
    source_confidence: float | None = None
    table_role: str = "business_object"
    role_confidence: float | None = None
    role_reason: str | None = None
    needs_review: bool = False
    # 分类证据快照（score / signals{pk_columns, fk_in_degree, technical_ratio…}）。
    # 审核界面的判据就是它——放进摘要，复核者才能在列表里横向比较、按信号排序，
    # 而不必逐个点进详情页的第 3 个 Tab。数据库里一直有这列，只是此前没往外发。
    # Agent 的工具结果里它是噪声，由 tool_result_compaction 的 _VERBOSE_KEYS 先行丢弃。
    role_signals: dict | None = None
    # DataHub profiling 沉淀的行数：判「这是不是日志/流水表」最直接的一列。
    row_count: int | None = None
    # 板块归属
    segment_id: str | None = None
    segment_name: str | None = None
    # Top N 邻居对象（用于卡片脚注展示关系）
    top_neighbors: list[dict[str, Any]] = Field(default_factory=list)
    domain_context_id: str | None = None
    domain_name: str | None = None
    # 物理落点：这个对象落到哪张表了。``None`` = 还没有任何落点登记（未物化/未同步），
    # 与「登记了但状态未就绪」是两回事，故不给默认对象。见 services/object_landing。
    landing: ObjectLandingOut | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class SegmentSummary(_ProvenanceReadMixin):
    """业务板块摘要（列表视图）。"""
    id: str
    name: str
    display_name: str
    #: business / shared / pending / technical / system，见 services/segment_kinds
    kind: str = "business"
    description: str | None = None
    member_count: int
    ontology_id: str
    # 所属数据域：板块页的「返回工作区」要用它。此前前端拿 ontology_id 当 domainId
    # 拼 /workspace/:domainId，是一条断链。
    domain_context_id: str | None = None
    needs_review: bool = False
    updated_at: datetime

    model_config = {"from_attributes": True}


class SegmentNeighbor(BaseModel):
    """板块外部、但与板块成员有关系的对象。

    板块视图的默认画面只画板块内部；邻居用来回答「这块业务往外连到谁」，
    按连接条数降序、由调用方截断，避免把 100+ 跨板块关系一次泼进画布。
    """

    id: str
    label: str
    display_name: str
    status: str
    is_hub: bool = False
    segment_id: str | None = None
    segment_name: str | None = None
    #: 该外部对象与本板块成员之间的关系条数
    link_count: int = 0


class SegmentDetail(SegmentSummary):
    """业务板块详情（包含成员列表）。"""
    # 成员对象列表（ObjectTypeSummary）
    members: list[ObjectTypeSummary] = Field(default_factory=list)
    # 板块内关系数量
    internal_relation_count: int = 0
    # 板块内的边数据。恒返回——板块视图的主画面就是这张图，早期只在成员 > 40 时
    # 才给，结果最大的板块（32 成员 / 51 条内部关系）也拿不到边，图从未渲染过。
    edges: list["GraphEdge"] | None = None
    cross_relation_count: int = 0
    relation_sentences: list[str] = Field(default_factory=list)
    #: 跨板块邻居（按 link_count 降序，已截断到 _SEGMENT_NEIGHBOR_CAP）
    neighbors: list[SegmentNeighbor] = Field(default_factory=list)
    #: 连向上述邻居的跨板块边；端点一头在 members 里、一头在 neighbors 里
    cross_edges: list["GraphEdge"] = Field(default_factory=list)


class ReviewGroupOut(BaseModel):
    """一组同类待复核对象——审核工作台的最小工作单元。

    同板块 + 同命名族 + 同判定强度的表几乎总是同一个判定，所以一组给一次裁决，
    例外靠反选。分组与排序规则见 ``services/review_queue``。
    """

    key: str
    segment_id: str | None = None
    segment_name: str
    #: 板块种类（business / shared / system，见 services/segment_kinds）。
    segment_kind: str = "business"
    #: 这一组是「归错了地方」：业务对象/关系表却压在系统表里，机器按邻居与命名族都
    #: 归不进任何业务模块。判定不因此被拒，但审核台要把「移动到板块」摆到最显眼处。
    #: 由服务端算：判定规则只在 segment_placement 写一次，前端不重算一遍。
    stranded_in_system: bool = False
    table_role: str
    name_family: str
    score_band: str
    score_band_label: str
    size: int
    # 这一组里**已经判过**的个数（按同一套分组规则、按当前角色归组）。
    # 「本组已判 11 个」本身就是判据：同族同板块的表前面都确认了，后面多半一样。
    reviewed_in_group: int = 0
    # 成员超过单组上限时截断（size 仍是真实总数），判完这批下次进来会补上。
    truncated: bool = False
    members: list[ObjectTypeSummary] = Field(default_factory=list)
    # kind=relation 时装这里（关系没有对象摘要那套字段，塞不进 members）
    relation_members: list["RelationTypeOut"] = Field(default_factory=list)


class ReviewQueueOut(BaseModel):
    """审核队列的一页。

    ``next_cursor`` 是下一页首组的 key：分组排序不含任何随判定变化的字段，
    因此判掉一批后重放同一游标不会跳过任何组。
    """

    # object=对象队列，relation=关系队列。两者共用分组与排序，只是成员类型不同。
    kind: str = "object"
    #: pending=还没判的，reviewed=已经判过的。分组在「待判+已判」的完整人口上做一次，
    #: 两种视图因此是同一批组、同一套 key——判完的板块回头看，位置和当初一模一样。
    status: str = "pending"
    groups: list[ReviewGroupOut] = Field(default_factory=list)
    group_total: int = 0
    # 本页首组在整条队列里的位置（0 基），用于「第 N / M 组」这类进度显示
    group_offset: int = 0
    # 队列里还剩多少个待判**个体**（不是组数）
    pending_total: int = 0
    # 已判过的个体数（同一筛选范围内）：「已判」这个 tab 的角标
    reviewed_total: int = 0
    # 按角色拆分的待判数：非业务对象也要能被判，否则它们永远不可见
    pending_by_role: dict[str, int] = Field(default_factory=dict)
    next_cursor: str | None = None


class SegmentUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    operator: str | None = None


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
    needs_review: bool = False

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

    一个 display_name（如「属于」）通常对应成百上千条 (源,目标) 外键，
    这里把它折叠成一行，聚合展示类型/基数/置信度/复核状态。具体外键由
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
    needs_review_count: int = 0
    target_groups: list[dict[str, Any]] = Field(default_factory=list)


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


class FormalIssueOut(BaseModel):
    code: str
    message: str
    severity: str  # error | warning
    entity_type: str
    entity_id: str | None = None
    entity_name: str | None = None


class FormalValidationResult(BaseModel):
    ontology_id: str
    ok: bool  # 无 error 级不变式违反（warning 不影响 ok）
    enforcement: str  # off | warn | error
    error_count: int = 0
    warning_count: int = 0
    issues: list[FormalIssueOut] = Field(default_factory=list)


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
    segments: dict = Field(default_factory=dict)


class ObjectTypeUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    # 人工改判对象角色（业务对象/数据表/关系表/技术表）。
    table_role: str | None = None
    # 板块归属（人工修改板块分配）
    segment_id: str | None = None
    # 复核状态开关：True=标记为待复核；False=标记为已确认（清除 [待复核]）。
    needs_review: bool | None = None
    operator: str | None = None


class ObjectTypeBatchUpdate(BaseModel):
    # 批量改判对象角色与复核状态。ids 为空或全无效则不产生任何变更。
    ids: list[str] = Field(default_factory=list)
    table_role: str | None = None
    #: 成组归类：把这批对象一次挪进某个业务板块（空串＝移出板块）。
    #: 与 needs_review=False 同时给出时先挪后判，「归类并确认」因此是一次调用。
    segment_id: str | None = None
    needs_review: bool | None = None
    operator: str | None = None


class ObjectTypeBatchUpdateResult(BaseModel):
    updated: int
    #: 其中判成业务对象/关系表、却被机器归不进任何业务模块而留在系统表里的个数。
    #: 只报 updated 会让人以为这一组归好位了。
    stranded_in_system: int = 0
    items: list[ObjectTypeSummary] = Field(default_factory=list)


class RelationTypeBatchUpdate(BaseModel):
    """批量置关系复核状态。关系没有角色可改判，判定就是「这条关系成不成立」。"""

    ids: list[str] = Field(default_factory=list)
    needs_review: bool | None = None
    operator: str | None = None


class RelationTypeBatchUpdateResult(BaseModel):
    updated: int
    items: list["RelationTypeOut"] = Field(default_factory=list)


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
    needs_review: bool | None = None
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
    #: business = 真业务模块；shared/pending/technical/system = 兜底板块。
    #: 前端据此把兜底板块固定排到目录末尾，并且不画进宏观概览图。
    kind: str = "business"
    #: 簇内关系条数——板块目录靠它排序：能读出关系的板块排在前面
    internal_relation_count: int = 0
    #: 该簇与簇外对象之间的关系条数
    cross_relation_count: int = 0
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
    ontology_id: str | None = None
    published_only: bool = False


class SegmentReviewProgress(BaseModel):
    """板块的审核进度统计。"""

    segment_id: str
    segment_name: str
    #: 板块种类，见 services/segment_kinds。pending 那一行是「待归类业务对象」，
    #: 它的成员必须先归位才算判完，界面据此单独提示。
    kind: str = "business"
    #: 对象口径
    total_count: int
    needs_review_count: int
    reviewed_count: int
    progress_ratio: float = Field(description="已审核比例 (0.0-1.0)")
    #: 关系口径：按**源端对象所属板块**归集，与关系队列的 segment_id 筛选同口径。
    #: 关系队列此前只显示板块名不给数字，正是因为拿对象进度顶替会读成假数字。
    relation_total: int = 0
    relation_needs_review: int = 0
    relation_reviewed: int = 0
    relation_progress_ratio: float = 1.0
    #: 板块内按角色拆分的对象计数。关系表（bridge）在关系页单独审，侧栏要显示的是
    #: 「这个板块还有几张关系表待判」，不是板块的对象总数。
    role_total: dict[str, int] = Field(default_factory=dict)
    role_pending: dict[str, int] = Field(default_factory=dict)


class ReviewModeStats(BaseModel):
    """审核模式的全局统计。

    口径：``total_objects`` / ``needs_review_count`` 覆盖**全部角色**，与审核队列
    一致。此前只统计 business_object，于是「桥表未能塌缩→重判为数据表/技术表」的那批
    对象（全部 needs_review=True）既不在分母里也不在队列里，事实上不可见。
    发布门禁只卡业务对象，那个数字单独由 ``business_object_pending`` 给出。
    """

    total_objects: int
    needs_review_count: int
    reviewed_count: int
    progress_ratio: float
    # 待复核按角色拆分（business_object / data_table / bridge / technical）
    pending_by_role: dict[str, int] = Field(default_factory=dict)
    # 全量按角色拆分：对象页要排除 bridge，分母也得跟着排除
    total_by_role: dict[str, int] = Field(default_factory=dict)
    # 其中会卡住发布的部分：待复核的业务对象不随本体发布（见 publish 的部分发布门禁）
    business_object_pending: int = 0
    total_relations: int = 0
    relation_needs_review_count: int = 0
    reviewed_relation_count: int = 0
    # 未接入板块的对象：它们不在任何 segment_progress 行里，但同样要判。
    # 队列侧用 segment_id="-" 指代这一桶。
    unsegmented_total: int = 0
    unsegmented_pending: int = 0
    #: 源端对象未接入板块的关系（关系队列 segment_id="-" 那一桶）
    unsegmented_relation_total: int = 0
    unsegmented_relation_pending: int = 0
    #: 判成业务对象/关系表却仍压在系统表里的对象数（归错了地方，等着被移出去），
    #: 以及其中已被标成已确认的那部分——后者不在待判队列里，只能靠这个数字捞回来。
    stranded_total: int = 0
    stranded_reviewed: int = 0

    segment_progress: list[SegmentReviewProgress] = Field(default_factory=list)
