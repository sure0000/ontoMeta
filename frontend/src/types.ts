export interface DomainContext {
  id: string;
  datahub_domain_id: string;
  name: string;
  description?: string;
  owner?: string;
  status: string;
  draft_count: number;
  published_count: number;
  object_type_count: number;
  relation_type_count: number;
  published_object_type_count: number;
  latest_draft_at?: string;
  latest_published_at?: string;
  updated_at: string;
}

export interface ExpressionTextSegment {
  type: "text";
  value: string;
}

export interface ExpressionRefSegment {
  type: "ref";
  ref_id: string;
  object_type_id: string;
  object_name: string;
  object_display_name: string;
  property_id?: string;
  property_name?: string;
  property_display_name?: string;
}

export type ExpressionSegment = ExpressionTextSegment | ExpressionRefSegment;

export interface ExpressionDraft {
  segments: ExpressionSegment[];
}

export type ExpressionJson = Record<string, unknown>;

export interface DomainContextDetail extends DomainContext {
  datahub_url?: string;
  /** 一域一本体：该域唯一那行本体，既是草稿工作台也是发布载体。
   *  旧字段 latest_ontology_id 取「按 updated_at 最新的那行」，会在 draft/published
   *  两行之间来回跳，页面主体因此不稳定；现在没有第二行可跳。 */
  working_ontology_id?: string;
  working_ontology_status?: string;
  published_ontology_id?: string;
  published_ontology_version?: number;
  /** 已发布内容被改动但未固化成新版本的实体数。 */
  unpublished_change_count?: number;
  /** 本次发布会新提升的实体数。 */
  pending_publish_count?: number;
  /** 待复核业务对象数（发布会跳过它们）。 */
  needs_review_count?: number;
  /** 未解决的字段级冲突数。 */
  unresolved_conflict_count?: number;
}

export interface IsolatedObject {
  object_id: string;
  object_name: string;
  reason: "no_relations" | "all_neighbors_unpublished";
  unpublished_neighbor_count: number;
}

export interface PublishPreflight {
  ontology_id: string;
  current_version: number;
  next_version: number;
  object_count: number;
  property_count: number;
  relation_count: number;
  skipped_needs_review: number;
  skipped_non_business: number;
  skipped_relation_endpoint: number;
  unresolved_conflicts: number;
  isolated_objects: IsolatedObject[];
}

export type DraftGenerationScope = "full" | "objects" | "relations";

export interface DraftProgress {
  task_id: string;
  status: string;
  progress: number;
  message?: string;
  ontology_id?: string;
  scope: DraftGenerationScope;
  /** 本次按表裁剪的数量；0 = 全域生成。 */
  scoped_table_count?: number;
}

/** 数据域里还没进本体的表（`GET /domains/{id}/unmodeled-tables`）。 */
export interface UnmodeledTable {
  urn: string;
  name: string;
  display_name?: string;
  description?: string;
  platform?: string;
  field_count: number;
  row_count?: number;
}

export interface UnmodeledTables {
  items: UnmodeledTable[];
  /** 域内表总数，用于说明「N 张里有 M 张没建模」。 */
  domain_table_count: number;
}

export interface TaskRecord {
  id: string;
  status: string;
  progress: number;
  message?: string;
  error_summary?: string;
  ontology_id?: string;
  evidence_count?: number;
  scope: DraftGenerationScope;
  created_at: string;
  updated_at: string;
}

export interface ChangeLog {
  id: string;
  entity_type: string;
  entity_id: string;
  action: string;
  operator?: string;
  change_summary?: string;
  created_at: string;
}

export interface FieldProvenance {
  origin?: string; // machine | manual | machine_edited
  upstream_removed?: boolean;
  has_conflict?: boolean;
  pinned_fields?: string[];
  conflicts?: Record<string, { base?: unknown; ours?: unknown; theirs?: unknown }>;
}

/**
 * 对象的**物理落点**：这个业务对象落到哪张物理表了。
 *
 * 任务产出的表不会变成新的业务对象——它们是既有对象的物理投影，由后端从
 * 接入契约 / 仓库 Projection 聚合而来（见 backend `services/object_landing`）。
 * `state` 由后端汇总，前端只负责渲染；在这里再算一遍就会出现第二份口径。
 */
export interface ObjectLanding {
  /**
   * `not_landed` 无任何登记 · `registered` 落点已登记未搬数 · `schema_ready` 表已建未搬数
   * · `syncing` 在跑 · `landed` 已落地 · `stale` 陈旧 · `failed` 失败
   */
  state: "not_landed" | "registered" | "schema_ready" | "syncing" | "landed" | "stale" | "failed";
  /** ODS 落点（库.表），恒为 `ods.ods_{数据域}_{原表}` */
  ods_table?: string;
  ods_status?: string;
  /** full / incremental / cdc */
  ods_mode?: string;
  /** 服务层落点（库.表） */
  serving_table?: string;
  serving_layer?: string;
  serving_status?: string;
  schema_status?: string;
  queryable?: boolean;
  last_success_at?: string;
  materialization_artifact_id?: string;
}

/**
 * 业务口径的 ADS 落点：指标/标签/规则任务把这条口径物化成的表。
 * 口径的物化归口径——它不会变成一个业务对象。
 */
export interface LogicLanding {
  state: ObjectLanding["state"];
  serving_table?: string;
  status?: string;
  queryable?: boolean;
  last_success_at?: string;
}

/**
 * 数仓里一张**已被本体认领**的物理表（`GET /ontologies/{id}/datasets`）。
 *
 * 同步/加工不会产生新本体——ODS、DWD 表都是既有实体的物理投影。目录给每个落点一个
 * 稳定引用 `ref`，它才是要存进任务配置的东西；`physical` 只用于展示与复制，表名会随
 * 契约变、引用不会。见 backend `services/dataset_catalog`。
 */
export interface DatasetEntry {
  /** `obj:<id>@ods` | `obj:<id>@serving` | `logic:<id>@ads` */
  ref: string;
  entity_kind: "object_type" | "business_logic";
  entity_id: string;
  entity_name: string;
  entity_display_name: string;
  /** 存储槽位：ods / serving / ads。引用指槽位不指层。 */
  slot: "ods" | "serving" | "ads";
  /** 展示用的分层：ods / dim / dwd / dws / ads */
  layer: string;
  /** 库.表 */
  physical: string;
  state: ObjectLanding["state"];
  queryable: boolean;
  /** 能否作为下游作业的源表。后端判定，前端不要再算一遍。 */
  source_ready: boolean;
  /** 仅 ODS：full / incremental / cdc */
  mode?: string;
  last_success_at?: string;
}

/**
 * 派生对象：由数仓里的若干数据集按**新粒度**算出来的业务对象。
 *
 * 判据只有一条：换了粒度才换实体，换了层只换落点。1:1 的搬运与清洗不产生新对象，
 * 它们只是既有对象的另一个落点（见 ObjectLanding / DatasetEntry）。
 */
export interface DerivedJoinCondition {
  left: string;
  right: string;
}

export interface DerivedJoin {
  left_ref: string;
  right_ref: string;
  on: DerivedJoinCondition[];
  how?: "inner" | "left";
}

export interface DerivedFieldInput {
  property: string;
  from_ref: string;
  from_column: string;
  display_name?: string;
}

export interface DerivedObjectCreate {
  name: string;
  display_name: string;
  /** 一行代表什么。**必填**——它就是「该不该建新对象」的判据。 */
  grain: string;
  upstream_refs: string[];
  fields: DerivedFieldInput[];
  description?: string;
  joins?: DerivedJoin[];
  layer?: string;
  notes?: string;
}

export interface DerivedObjectCreated {
  object_type_id: string;
  name: string;
  display_name: string;
  ontology_id: string;
  layer: string;
  upstream_refs: string[];
}

export interface DerivedDefinition {
  object_type_id: string;
  grain: string;
  layer?: string;
  upstreams: DatasetEntry[];
  /** 已解析不到的上游（被删/被降级）。要显示——少列一个会让定义看起来仍然成立。 */
  dangling_refs: string[];
  joins: DerivedJoin[];
  field_mapping: DerivedFieldInput[];
  notes?: string;
}

/**
 * 数仓里存在、本体里没人认领的一张表（`GET /ontologies/{id}/unclaimed-tables`）。
 *
 * 对这类表只给两个动作：认领为已有实体的落点，或者不管它。**没有「照着表建对象」**——
 * 照物理表反推出来的对象正是重复对象的来源。
 */
export interface UnclaimedTable {
  database: string;
  table: string;
  physical: string;
  /** 由库名推断的分层；推不出就没有（不猜——层会写进落点登记）。 */
  layer?: string;
}

export interface UnclaimedTables {
  items: UnclaimedTable[];
  /** 实际扫过的库。少扫了哪个要看得见，否则「没有无主表」既可能是真干净也可能是没扫到。 */
  scanned_databases: string[];
}

export interface ObjectTypeSummary extends FieldProvenance {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  /** 源表定位（DataHub urn / `manual:<源>:<标识>` / `derived:<本体 id>:<标识>`）。 */
  source_ref?: string;
  /**
   * 对象来源。判「能不能建同步任务」用这个，**不要**用 `source_ref` 是否为空——
   * 人工建模与派生对象的 source_ref 都非空，却都没有可搬的源库表。
   * - `datahub` 由数据源采集而来，有真实源表 → 可同步
   * - `manual`  人工建模，只有元数据 → 只能物化建表
   * - `derived` 派生对象，上游是数仓里的数据集 → 物化建表 + 清洗落数，不可同步
   * - `none`    无 source_ref 或无法解析
   */
  source_provenance?: "datahub" | "manual" | "derived" | "none";
  status: string;
  property_count: number;
  relation_count: number;
  business_logic_count: number;
  bound_logic_count?: number;
  source_confidence?: number;
  table_role?: string;
  role_confidence?: number;
  role_reason?: string;
  needs_review?: boolean;
  /** 分类证据快照：审核界面的判据。列表接口现在也带它，不必再跳详情页看。 */
  role_signals?: RoleSignals;
  /** DataHub profiling 沉淀的行数：判「是不是日志/流水表」最直接的一列。 */
  row_count?: number;
  segment_id?: string;
  segment_name?: string;
  top_neighbors?: Array<{
    id: string;
    name: string;
    display_name: string;
    relation_name: string;
    direction: "outbound" | "inbound";
  }>;
  domain_context_id?: string;
  domain_name?: string;
  /**
   * 物理落点。`undefined` = 还没有任何落点登记（既没物化也没同步），与
   * 「登记了但未就绪」是两回事，故后端不给默认值。
   */
  landing?: ObjectLanding;
  updated_at: string;
}

export interface SegmentSummary extends FieldProvenance {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  member_count: number;
  ontology_id: string;
  /** 所属数据域：板块页返回工作区要用它（ontology_id 不是 domainId）。 */
  domain_context_id?: string;
  needs_review: boolean;
  updated_at: string;
}

export interface SegmentDetail extends SegmentSummary {
  members: ObjectTypeSummary[];
  internal_relation_count: number;
  edges?: GraphEdge[];
  cross_relation_count?: number;
  relation_sentences?: string[];
}

export interface SegmentReviewProgress {
  segment_id: string;
  segment_name: string;
  total_count: number;
  needs_review_count: number;
  reviewed_count: number;
  progress_ratio: number;
}

export interface ReviewModeStats {
  /** 覆盖全部角色（与审核队列同口径），不再只数业务对象。 */
  total_objects: number;
  needs_review_count: number;
  reviewed_count: number;
  progress_ratio: number;
  /** 待复核按角色拆分 */
  pending_by_role?: Record<string, number>;
  /** 其中会卡住发布的部分（待复核的业务对象不随本体发布） */
  business_object_pending?: number;
  total_relations: number;
  relation_needs_review_count: number;
  reviewed_relation_count: number;
  /** 未接入板块的对象：不在任何 segment_progress 行里，队列侧用 segment_id="-" 指代 */
  unsegmented_total?: number;
  unsegmented_pending?: number;
  segment_progress: SegmentReviewProgress[];
}

/** 一组同类待复核对象——审核工作台的最小工作单元。 */
export interface ReviewGroup {
  key: string;
  segment_id?: string | null;
  segment_name: string;
  table_role: string;
  name_family: string;
  score_band: "strong" | "near" | "weak" | "unknown";
  score_band_label: string;
  size: number;
  /** 这一组里已经判过的个数（判定自我加强：同族前面都确认了，后面多半一样）。 */
  reviewed_in_group: number;
  truncated: boolean;
  members: ObjectTypeSummary[];
  /** kind=relation 时成员装在这里（关系没有对象摘要那套字段）。 */
  relation_members: RelationType[];
}

export interface ReviewQueue {
  kind: "object" | "relation";
  groups: ReviewGroup[];
  group_total: number;
  /** 本页首组在整条队列里的位置（0 基） */
  group_offset: number;
  pending_total: number;
  pending_by_role: Record<string, number>;
  next_cursor?: string | null;
}

export interface VerbSuggestion {
  relation_id: string;
  current_verb: string;
  suggested_verb: string;
  method: string;
  confidence: number;
  source_object_name: string;
  target_object_name: string;
}

export interface VerbRefinementBatch {
  suggestions: VerbSuggestion[];
  total: number;
  rule_count: number;
  llm_count: number;
  fallback_count: number;
}

export interface Property extends FieldProvenance {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  data_type?: string;
  semantic_type?: string;
  source_field_ref?: string;
  required: boolean;
  source_confidence?: number;
  status: string;
}

export interface RelationType extends FieldProvenance {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  source_object_type_id: string;
  target_object_type_id: string;
  source_object_name?: string;
  target_object_name?: string;
  cardinality?: string;
  structure_type?: string;
  mapping_object_type_id?: string | null;
  mapping_object_name?: string | null;
  source_evidence?: string;
  status: string;
  source_confidence?: number;
  needs_review?: boolean;
}

export interface RelationObjectRef {
  id: string;
  name: string;
  display_name: string;
  source_ref?: string;
  datahub_url?: string;
}

export interface DataHubDatasetOption {
  urn: string;
  name: string;
  display_name?: string;
  description?: string;
  platform?: string;
  container?: string;
  object_type_id?: string | null;
  object_type_display_name?: string | null;
  datahub_url?: string;
}

export interface RelationTypeDetail extends RelationType {
  ontology_id: string;
  source_evidence?: string;
  source_object?: RelationObjectRef;
  target_object?: RelationObjectRef;
  mapping_object?: RelationObjectRef | null;
}

/** 按 display_name 去重后的关系分组（关系 Tab 列表用）。 */
export interface RelationGroup {
  display_name: string;
  count: number;
  description?: string | null;
  structure_types: string[];
  cardinalities: string[];
  confidence_min?: number | null;
  confidence_max?: number | null;
  statuses: string[];
  needs_review_count: number;
  target_groups: Array<{ display_name: string; count: number }>;
}

/** 对象角色分类的结构化证据快照（后端 object_classifier 产出，仅详情返回）。 */
export interface RoleSignals {
  score?: number;
  needs_review?: boolean;
  role?: string;
  signals?: Record<string, number | boolean | string | null>;
}

export interface ObjectTypeDetail extends ObjectTypeSummary {
  ontology_id?: string;
  domain_context_id?: string;
  domain_name?: string;
  source_ref?: string;
  datahub_url?: string;
  role_signals?: RoleSignals;
  properties: Property[];
  outgoing_relations: RelationType[];
  incoming_relations: RelationType[];
  /** 本对象作为关系表(bridge)所实现(mapping)的业务关系；桥表本身非端点，故用于其图谱展示。 */
  implemented_relations?: RelationType[];
  business_logics: BusinessLogic[];
  business_logic_bindings?: ObjectTypeLogicBinding[];
  version_records?: VersionRecord[];
}

export interface BusinessLogicCategory {
  id: string;
  name: string;
  description?: string;
  logic_count: number;
  created_at: string;
  updated_at: string;
}

export interface BusinessLogic {
  id: string;
  name: string;
  display_name: string;
  logic_type: string;
  description?: string;
  expression_summary?: string;
  expression_draft?: ExpressionDraft;
  expression_json?: ExpressionJson;
  source_type?: string;
  source_ref?: string;
  status: string;
  source_confidence?: number;
  domain_context_id?: string;
  domain_name?: string;
  category_id?: string | null;
  category_name?: string | null;
  bound_object_count?: number;
  bound_property_count?: number;
  /** ADS 落点。`undefined` = 该口径还没有被指标任务物化。 */
  landing?: LogicLanding;
  updated_at: string;
}

export interface ObjectTypeLogicBinding {
  binding_id: string;
  role: string;
  source: string;
  confidence?: number;
  logic_id: string;
  logic_name: string;
  logic_display_name: string;
  logic_type: string;
  logic_status: string;
  created_at: string;
}

export interface BusinessLogicObjectBinding {
  id: string;
  business_logic_id: string;
  object_type_id: string;
  object_type_name?: string;
  object_type_display_name?: string;
  role: string;
  source: string;
  confidence?: number;
  created_at: string;
}

export interface BusinessLogicPropertyBinding {
  id: string;
  business_logic_id: string;
  property_id: string;
  property_name?: string;
  property_display_name?: string;
  object_type_id?: string;
  object_type_name?: string;
  role: string;
  source: string;
  confidence?: number;
  created_at: string;
}

export interface BusinessLogicPropertyOption {
  property_id: string;
  property_name: string;
  property_display_name?: string;
  object_type_id: string;
  object_type_name: string;
  object_type_display_name?: string;
}

export interface BusinessLogicCreateInput {
  domain_id: string;
  name: string;
  display_name: string;
  logic_type: string;
  description?: string;
  expression_summary?: string;
  expression_draft?: ExpressionDraft;
  expression_json?: ExpressionJson;
  category_id?: string | null;
  operator?: string;
}

export interface BusinessLogicUpdateInput {
  display_name?: string;
  description?: string;
  logic_type?: string;
  expression_summary?: string;
  expression_draft?: ExpressionDraft;
  expression_json?: ExpressionJson;
  category_id?: string | null;
  operator?: string;
}

export interface BusinessLogicImportInput {
  domain_id: string;
  code: string;
  source_type?: string;
  category_id?: string | null;
  operator?: string;
}

export interface BusinessLogicRef {
  id: string;
  name: string;
  display_name: string;
  logic_type: string;
  status: string;
}

export interface BusinessLogicDetail extends BusinessLogic {
  related_object_types: ObjectTypeSummary[];
  related_object_logics?: Record<string, BusinessLogicRef[]>;
  related_properties?: Property[];
  object_bindings?: BusinessLogicObjectBinding[];
  property_bindings?: BusinessLogicPropertyBinding[];
  version_records?: VersionRecord[];
  ontology_id?: string;
  available_object_types: ObjectTypeSummary[];
  available_properties: BusinessLogicPropertyOption[];
}

export interface VersionRecord {
  id: string;
  entity_type: string;
  entity_id: string;
  version: number;
  diff_summary?: string;
  operator?: string;
  created_at: string;
  has_diff?: boolean;
  has_snapshot?: boolean;
}

export interface VersionDiffSection {
  added: Array<{ key?: string; name?: string; display_name?: string }>;
  removed: Array<{ key?: string; name?: string; display_name?: string }>;
  modified: Array<{
    key?: string;
    name?: string;
    display_name?: string;
    changes?: Record<string, { from?: unknown; to?: unknown }>;
  }>;
}

export interface VersionDiff {
  ontology_id: string;
  version: number;
  previous_version?: number | null;
  diff_summary?: string;
  operator?: string;
  created_at?: string;
  object_types: VersionDiffSection;
  properties: VersionDiffSection;
  relation_types: VersionDiffSection;
  business_logics: VersionDiffSection;
}

export interface VersionSnapshot {
  ontology_id: string;
  version: number;
  diff_summary?: string;
  created_at?: string;
  object_types: Record<string, unknown>[];
  properties: Record<string, unknown>[];
  relation_types: Record<string, unknown>[];
  business_logics: Record<string, unknown>[];
}

export interface OntologyValidationResult {
  ontology_id: string;
  ok: boolean;
  issues: Array<{
    code: string;
    message: string;
    entity_type: string;
    entity_id?: string;
    entity_name?: string;
  }>;
}

export interface FormalIssue {
  code: string;
  message: string;
  severity: "error" | "warning";
  entity_type: string;
  entity_id?: string | null;
  entity_name?: string | null;
}

export interface FormalValidationResult {
  ontology_id: string;
  ok: boolean; // 无 error 级不变式违反
  enforcement: "off" | "warn" | "error";
  error_count: number;
  warning_count: number;
  issues: FormalIssue[];
}

export interface OntologySummary {
  id: string;
  domain_context_id: string;
  version: number;
  status: string;
  generated_at?: string;
  published_at?: string;
  object_type_count: number;
  relation_type_count: number;
  business_logic_count: number;
}

export interface GraphNode {
  id: string;
  label: string;
  display_name: string;
  status: string;
  table_role?: string;
  needs_review?: boolean;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  cardinality?: string;
  relationId?: string;
  relation_id?: string;
  /** foreign_key / derivation / bridge_table / fact_table / other，供矩阵单元格按类型着色 */
  structure_type?: string;
}

export interface OntologyGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  center_id?: string | null;
  depth?: number;
  truncated?: boolean;
  total_object_count?: number;
  total_relation_count?: number;
}

export interface GraphPoint {
  x: number;
  y: number;
}

export interface ClusterNode {
  id: string;
  label: string;
  display_name: string;
  status: string;
}

export interface GraphCluster {
  id: string;
  name: string;
  nodes: ClusterNode[];
  node_count: number;
  truncated: boolean;
  /** 宏观图中的稳定坐标（近邻间距约 1 个单位，前端按固定像素间距放大） */
  layout?: GraphPoint | null;
}

export interface HubNode {
  id: string;
  label: string;
  display_name: string;
  status: string;
  degree: number;
  layout?: GraphPoint | null;
}

export interface GroupedGraphEdge {
  id: string;
  /** 可能是聚类 id，也可能是枢纽节点 id */
  source_cluster_id: string;
  target_cluster_id: string;
  weight: number;
  relation_ids: string[];
}

export interface OntologyGroupedGraph {
  clusters: GraphCluster[];
  hub_nodes: HubNode[];
  edges: GroupedGraphEdge[];
  isolated_nodes: ClusterNode[];
  total_object_count: number;
  total_relation_count: number;
}

/** 单簇下钻详情：全量成员 + 簇内关系边，供邻接矩阵视图。 */
export interface ClusterDetail {
  id: string;
  name: string;
  node_count: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface PageResult<T> {
  items: T[];
  total: number;
  limit: number | null;
  offset: number;
}

export interface Confirmation {
  id: string;
  ontology_id: string;
  target_type: string;
  target_id?: string;
  action_type: string;
  confirmation_status: string;
  operator?: string;
  reason?: string;
  confirmed_at?: string;
  created_at: string;
}

export interface LlmModelOption {
  id: string;
  label: string;
  description: string;
  deprecated?: boolean;
}

export interface LlmServiceConfig {
  id: string;
  name: string;
  provider: string;
  api_base_url: string;
  model: string;
  is_default: boolean;
  enabled: boolean;
  api_key_set: boolean;
  api_key_hint?: string;
  api_key?: string;
  created_at: string;
  updated_at: string;
}

export interface LlmConnectionTestResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
  model?: string;
}

export interface DatahubSettings {
  gms_url: string;
  frontend_url: string;
  token_set: boolean;
  token_hint?: string;
  fabric: string;
  updated_at: string;
}

export interface DraftGenerationSettings {
  object_chunk_concurrency: number;
  relation_chunk_concurrency: number;
  updated_at: string;
}

/** Airflow 编排配置。凭据只回「是否已设 + 掩码」，不回明文。 */
export interface AirflowSettings {
  /** 连接一：调度 API。没有 token/api_version——前者 Airflow REST 用的是 basic auth，
   *  后者由客户端 404 时自协商。 */
  endpoint: string;
  username?: string | null;
  password_set: boolean;
  password_hint?: string | null;
  enabled: boolean;
  /** 启用且 endpoint / SSH 主机已填才算真的可用；否则物化报错无法执行。 */
  available: boolean;
  /** DAG 与作业配置的投递目录（**Airflow 主机上的路径**）。 */
  dags_dir: string;
  /** SSH 投递参数：产物 rsync 到 Airflow 主机后原子切换（唯一投递通道）。 */
  ssh_host: string;
  ssh_port: number;
  ssh_user: string;
  /** 填了就用密码认证（需 sshpass）；留空则用 ontoMeta 主机的默认 SSH 身份/agent。 */
  ssh_password_set: boolean;
  ssh_password_hint: string | null;
  max_tasks_per_dag: number;
  max_active_tasks_per_dag: number;
  dag_parse_timeout: number;
  staging_swap: boolean;
  /** Flink 执行引擎参数（搬运/计算经 Airflow BashOperator 提交 flink run）。 */
  flink_sql_runner_jar?: string;
  flink_sql_runner_class?: string;
  flink_bin?: string;
  flink_deploy_target?: string;
  flink_parallelism?: number;
  flink_yarn_queue?: string;
  flink_checkpoint_dir?: string;
  flink_rest_endpoint?: string;
  updated_at: string;
}

// ===== 依赖组件统一部署管理（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0） =====

export interface DependencySchemaField {
  name: string;
  type: "str" | "int" | "bool" | "text";
  secret: boolean;
  required: boolean;
  default?: string | number | boolean | null;
}
export interface DependencyComponentMeta {
  key: string;
  label: string;
  multi: boolean;
}
/** 连接分组：一个组件可能握着几条互不相干的连接（Airflow = 调度 API + DAG 投递）。 */
export interface DependencyConnectionGroup {
  id: string;
  label: string;
  fields: string[];
}
export interface DependencySchema {
  components: DependencyComponentMeta[];
  connection_schemas: Record<string, DependencySchemaField[]>;
  connection_groups: Record<string, DependencyConnectionGroup[]>;
  deploy_modes: string[];
  // 每组件允许的部署方式（未列出=全支持）；前端据此收窄模式选择器。
  component_deploy_modes?: Record<string, string[]>;
  deploy_spec_schemas: Record<string, DependencySchemaField[]>;
  bare_metal_params: Record<string, DependencySchemaField[]>;
  docker_params: Record<string, DependencySchemaField[]>;
  deploy_statuses: string[];
}
export interface DependencyComponent {
  id: string;
  key: string;
  name: string;
  deploy_mode: string;
  deploy_spec: Record<string, unknown>;
  deploy_status: string;
  deploy_error?: string | null;
  deploy_log?: string | null;
  connection: Record<string, unknown>;
  enabled: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}
export interface DependencyProbeResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
  /** 逐条连接的拨测明细；单连接组件也回一条。 */
  parts?: DependencyProbePart[];
}
export interface DependencyProbePart {
  group: string;
  label: string;
  ok: boolean;
  message: string;
  latency_ms?: number | null;
  /** 记账时间（ISO）。只在组件行的 deploy_spec._probe 里有。 */
  at?: string;
}
export interface DependencyDeployResult {
  status: string;
  ok: boolean;
  message?: string;
}

export interface ChatBiConversation {
  id: string;
  domain_ids: string[];
  domain_id?: string | null;
  title: string;
  category?: string | null;
  is_pinned: boolean;
  is_archived: boolean;
  message_count: number;
  last_message_preview?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChatBiCategoryItem {
  name: string;
  conversation_count: number;
}

export interface ChatBiCategoryList {
  categories: ChatBiCategoryItem[];
}

export interface ChatBiMessageItem {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  payload?: Record<string, unknown> | null;
  created_at: string;
}

/** 决策留痕：一次对话里人在某个关键节点拍的板。 */
export interface ChatBiDecision {
  id: string;
  conversation_id: string;
  /** 仅跨会话查询填充——会话内时间线本就在会话上下文里，不必重复。 */
  conversation_title?: string | null;
  message_id?: string | null;
  block_id?: string | null;
  seq: number;
  /** requirement | ontology | data | plan | execute | result | other */
  node: string;
  stage?: string | null;
  trigger?: string | null;
  /** accepted | modified | rejected | skipped */
  outcome: string;
  subject_id?: string | null;
  subject_role?: string | null;
  summary?: string | null;
  /** agent 原样提的 */
  proposed?: unknown;
  /** 人最终定的 */
  chosen?: unknown;
  /** 人相对机器基线改过的顶层键 */
  overridden_fields: string[];
  ref_kind?: string | null;
  ref_id?: string | null;
  created_at?: string | null;
}

export interface ChatBiClosureNode {
  node: string;
  label: string;
  reached: boolean;
  latest_outcome?: string | null;
  latest_at?: string | null;
  summary?: string | null;
  count: number;
}

/**
 * 本会话催生的一条数据任务**及它自己的六环闭环**。
 *
 * 闭环的粒度是任务、不是会话：`nodes` 恒为六项（未到达的标灰而非隐藏），只统计归属
 * 这条任务的决策记录。卡片一条任务一张，并据此给出「重新进入某一环」的入口。
 */
export interface ChatBiClosureTask {
  artifact_id: string;
  name: string;
  kind?: string | null;
  status?: string | null;
  confirmation_id?: string | null;
  nodes: ChatBiClosureNode[];
  reached_count: number;
  total_count: number;
  dangling: string[];
}

/**
 * 一次对话的决策总结。
 *
 * `tasks` 是给人看的闭环——一条任务一组六环。会话级的 `nodes`/`reached_count`/
 * `dangling` 是审计聚合（决策追踪页的时间线表头、跨会话统计），**不是闭环**：
 * 拿它画图的话，一次纯查询点个「认可」也会顶出一张六环卡。
 */
export interface ChatBiDecisionClosure {
  conversation_id: string;
  nodes: ChatBiClosureNode[];
  reached_count: number;
  total_count: number;
  dangling: string[];
  tasks: ChatBiClosureTask[];
  records: ChatBiDecision[];
}

export interface ChatBiReference {
  id?: string | null;
  name?: string | null;
  display_name?: string | null;
}

export type ChatBiCaliberKind = "object_type" | "property" | "relation_type" | "business_logic";

export interface ChatBiCaliberReference {
  kind: ChatBiCaliberKind;
  id?: string | null;
  name?: string | null;
  display_name?: string | null;
}

export interface ChatBiCaliberItem {
  label: string;
  description?: string | null;
  references: ChatBiCaliberReference[];
}

export interface ChatBiOpsRecord {
  family: string;
  subject?: string | null;
  facts?: Array<{ key: string; label: string; value: unknown }>;
  items?: Array<Record<string, unknown>>;
  as_of?: string | null;
  observed_at?: string | null;
  source?: string;
  truncated?: boolean;
  note?: string | null;
}

export interface ChatBiAgentStep {
  index: number;
  /**
   * "tool"（默认）= 工具调用步；"thought" = 工具间的模型自述；
   * "repair" = 可靠性校验未过、正在重写（P4.3）。
   */
  kind?: "tool" | "thought" | "repair";
  tool: string;
  /** kind==="thought" 时的思考文本。 */
  text?: string;
  arguments?: Record<string, unknown>;
  status?: "running" | "succeeded" | "failed";
  summary?: string | null;
}

export interface ChatBiClarification {
  question: string;
  options: string[];
  reason?: string;
}

/**
 * 表单候选项：**显示什么**（label）与**回填什么**（value）分开。
 *
 * 带 id 的候选（数据源、对象）此前只能写成「名称｜id」，那串 id 就直接糊在下拉里给人看。
 * `disabled` 用于「摆出来但选不了」的候选——执行侧不支持的装载方式必须看得见（否则用户
 * 以为系统只会全量），但不能真被选中（与 MaterializeModal 的置灰同口径）。
 */
export interface ChatBiFormOption {
  label: string;
  value: string;
  disabled?: boolean;
}

/** 交互表单字段（P6）：`type` 决定前端用哪种控件渲染。 */
export interface ChatBiFormField {
  name: string;
  label: string;
  type:
    | "text"
    | "textarea"
    | "number"
    | "select"
    | "multiselect"
    | "radio"
    | "boolean"
    | "date"
    /** 带候选建议的文本框：候选是建议不是闭集（如分区键）。 */
    | "autocomplete"
    /** 调度选择器：与业务对象详情里那个「定时策略」同一个 CronPicker。 */
    | "cron";
  /** select/multiselect/radio/autocomplete 的候选项（须来自真实实体）。 */
  options?: ChatBiFormOption[];
  required?: boolean;
  placeholder?: string;
  help?: string;
  default?: string | number | boolean | string[] | null;
  /** 建数确认向导中的所属环节；通用表单留空。 */
  confirmation_node?: "ontology" | "data" | "plan" | string;
  /** 级联候选：监听该字段（同步源数据源监听 object_type）。 */
  depends_on?: string;
  /** 上游字段值 → 本字段候选。 */
  options_by_value?: Record<string, ChatBiFormOption[]>;
  /**
   * 候选实时取而非静态摊开。`object_properties` = 拉 `depends_on` 那个对象的字段清单
   * （几百对象的本体全摊开是几 MB 的消息负载）。
   */
  options_from?: "object_properties" | string;
  /**
   * 条件可见：`{ field: "mode", in: ["incremental", "cdc"] }`。不满足时不渲染、不校验、
   * 也不提交该字段的值——否则改回全量后，先前填的 CDC 参数仍会进 Spec 并真的生效。
   */
  visible_when?: { field: string; in: string[] };
}

/**
 * Agent 动态生成的可填写表单（P6）：一次向用户收集多个结构化参数。
 * 与 clarification 同为终态出口——本轮结束、等用户填完提交带回（结构化回填文本进 history）。
 */
export interface ChatBiFormRequest {
  title: string;
  intent?: string;
  /**
   * 服务端改判/合并了这次请求时给人的一句解释（如「同步自带建表，已省掉物化那一步」）。
   * 改判不能只在后台发生——人得知道自己拿到的为什么是这张表单。
   */
  notice?: string;
  submit_label?: string;
  fields: ChatBiFormField[];
  /** 数据任务表单元数据；存在时提交直接进入草稿+dry-run，不再续问 LLM。 */
  task_kind?: string;
  ontology_id?: string;
  /** 一张任务确认单的隔离 id；防止复用同会话旧确认。 */
  confirmation_id?: string;
  /**
   * 一个数据任务的六环确认之旅：需求 → 本体 → 数据 → 执行方案 → 执行 → 结果。
   * `phase="form"` 的前三环在本表单里逐环确认，`phase="artifact"` 的后三环在任务
   * 详情抽屉里逐环确认——一次给全，人从第一步就看得见一共几环、现在第几环。
   * 没有这个字段则按普通单页表单渲染（非任务表单）。
   */
  confirmation_steps?: Array<{
    node: "requirement" | "ontology" | "data" | "plan" | "execute" | "result" | string;
    title: string;
    description?: string;
    phase?: "form" | "artifact" | string;
  }>;
}

export interface ChatBiDataResult {
  columns: { key?: string; title?: string; [k: string]: unknown }[];
  rows: Record<string, unknown>[];
  truncated?: boolean;
}

/**
 * 渲染块（V3 S0）：Data Agent 回答由一串有类型的块组成，前端按 `type` 查注册表渲染，
 * 替代改造前写死的 JSX 阶梯。后端 `answer_to_blocks` 双写，缺失时 `answerToBlocks` 兜底。
 * 未来 S1 的 chart / lineage / draft_proposal 是新增的块类型，运行时由渲染器 default 跳过。
 */
export type ChatBiBlock =
  | { id: string; type: "steps"; steps: ChatBiAgentStep[] }
  | { id: string; type: "markdown"; content: string }
  | {
      id: string;
      type: "mapping";
      variant: "inline" | "caliber";
      items: ChatBiCaliberItem[];
      /** 「命中本体」：去重的可跳转口径/对象引用，随口径卡一行展示。 */
      references: ChatBiCaliberReference[];
    }
  | { id: string; type: "sql"; sql: string; compiled_from?: string }
  | {
      id: string;
      type: "table";
      columns: ChatBiDataResult["columns"];
      rows: ChatBiDataResult["rows"];
      truncated?: boolean;
    }
  | {
      id: string;
      type: "chart";
      spec: { kind: "bar" | "line" | "area"; x: string; y: string; title?: string };
      columns: ChatBiDataResult["columns"];
      rows: ChatBiDataResult["rows"];
    }
  | {
      id: string;
      /** P5：结果统计画像 + 离群检测（analyze_result 产出）。 */
      type: "insight";
      analysis: {
        row_count: number;
        total_outliers: number;
        total_jumps?: number;
        ordered_by?: string;
        columns: Array<{
          column: string;
          count: number;
          nulls: number;
          min: number;
          max: number;
          mean: number;
          p25?: number;
          median?: number;
          p75?: number;
          std?: number;
          outlier_count?: number;
          outliers?: number[];
          trend?: {
            direction: "up" | "down" | "flat";
            slope: number;
            first: number;
            last: number;
            change: number;
            change_pct?: number | null;
          };
          jumps?: Array<{ at: unknown; from: number; to: number; delta: number }>;
        }>;
      };
    }
  | { id: string; type: "refs"; objects: ChatBiReference[]; logics: ChatBiReference[] }
  | {
      id: string;
      /** P2：多步分析计划（update_plan 产出）。声明式路线图，与实时 steps 轨迹互补。 */
      type: "plan";
      steps: Array<{ title: string; status: "pending" | "active" | "done" }>;
      note?: string;
    }
  | {
      id: string;
      type: "lineage";
      center_id?: string | null;
      nodes: GraphNode[];
      edges: GraphEdge[];
      truncated?: boolean;
    }
  | { id: string; type: "notice"; level: "info" | "warning"; variant: "refused" | "mock" }
  | {
      id: string;
      type: "draft_proposal";
      proposal: {
        kind: string;
        logic_type: string;
        display_name: string;
        name: string;
        description?: string;
        /**
         * propose_expression 产的提案还带**已编译并自证过**的表达式：
         * compiled_sql/caliber_trace 给人看（判断口径对不对，看的是真 SQL 不是承诺），
         * expression_json 是要落库的权威 AST。propose_draft 产的提案没有这几项。
         */
        compiled_sql?: string;
        caliber_trace?: string[];
        expression_json?: Record<string, unknown>;
        /** 当前系统可选的业务逻辑分类目录，供确认前人工调整。 */
        category_options?: Array<{ id: string; name: string }>;
        /** 新建：POST /api/business-logics。 */
        create_payload?: BusinessLogicCreateInput;
        /** 给已有口径补表达式：PATCH /api/business-logics/{logic_id}。 */
        logic_id?: string;
        update_payload?: {
          logic_type?: string;
          expression_summary?: string;
          expression_json?: Record<string, unknown>;
          category_id?: string | null;
        };
      };
    }
  | {
      id: string;
      /** P0：数据任务提案（物化/同步/加工）。agent 只出提案，点按钮才走既有 draft→…→execute。 */
      type: "action_proposal";
      proposal: {
        kind: string;
        intent: string;
        context?: Record<string, unknown>;
        ontology_id?: string | null;
        /** request_form 生成的确认单号；有它时创建必须走 draft-confirmed。 */
        confirmation_id?: string | null;
        /** 「去校验并执行」按钮原样传给 api.draftArtifact 的载荷。 */
        draft_payload: {
          kind: string;
          intent: string;
          context?: Record<string, unknown>;
          ontology_id?: string | null;
        };
      };
    }
  | {
      id: string;
      /**
       * 任务链提案（propose_pipeline 产出）：前后相继的多个任务。
       * 点「创建任务链」只建链、不起草任何制品；每一步仍各自走校验/确认/执行。
       */
      type: "pipeline_proposal";
      proposal: {
        kind: "pipeline";
        name: string;
        intent?: string;
        ontology_id?: string | null;
        steps: { kind: string; intent: string; context?: Record<string, unknown> }[];
        /**
         * 服务端砍掉的步骤（当前只有一种：排在同步前、纯为同步建表的物化）。
         * 同步自己会幂等建出 ODS 表，那一步是多余的；但砍了要说出来，不能让人
         * 以为自己要的步骤凭空消失。
         */
        dropped_steps?: { kind: string; intent: string; reason: string }[];
        /** 「创建任务链」按钮原样传给 api.createPipeline 的载荷。 */
        create_payload: {
          name: string;
          intent?: string;
          ontology_id?: string | null;
          steps: { kind: string; intent: string; context?: Record<string, unknown> }[];
        };
      };
    }
  | {
      id: string;
      /**
       * 数据应用提案（propose_panel / propose_dashboard 产出）：把本轮口径做成面板或新看板。
       * agent 只出提案；点按钮才走既有的 generate-widget / generate-app，口径由本条消息的
       * payload 附上（与动作条同一条路，保证生成的东西与对话里看到的一致）。
       */
      type: "app_proposal";
      proposal: {
        kind: "panel" | "dashboard";
        /** 面板标题；kind=dashboard 时是首个面板的标题。 */
        title: string;
        /** 仅 kind=dashboard：看板名称。 */
        name?: string;
        viz_type: "bar" | "kpi" | "table";
        domain_id: string;
        create_payload: {
          domain_id: string;
          question: string;
          /** kind=panel：面板图型（→ generate-widget 的 widget_type）。 */
          widget_type?: string;
          /** kind=dashboard：固定 "dashboard"（→ generate-app 的 app_type）。 */
          app_type?: string;
          name: string;
        };
      };
    }
  | {
      id: string;
      /**
       * 接数据提案（propose_datasource / propose_ontology_draft 产出）。
       * 建源的凭据**不由 agent 提供**：用户在卡里自己填连接信息后才 POST /api/data-sources。
       */
      type: "onboard_proposal";
      proposal: {
        kind: "datasource" | "ontology_draft";
        /** kind=datasource：数据源显示名。 */
        name?: string;
        /** kind=datasource：数据源类型（mysql/postgres/…）。 */
        datasource_kind?: string;
        catalog_name?: string | null;
        note?: string;
        /** 提案里被丢弃的参数名（模型塞了凭据时如实回显）。 */
        dropped_args?: string[];
        credentials_required?: boolean;
        /** kind=ontology_draft：目标域与生成范围。 */
        domain_id?: string;
        domain_name?: string;
        scope?: "draft" | "objects" | "relations";
        reason?: string;
        has_published_ontology?: boolean;
        create_payload: {
          name?: string;
          kind?: string;
          catalog_name?: string | null;
          domain_id?: string;
          scope?: string;
        };
      };
    }
  | {
      id: string;
      /** P0：任务状态回读（get_task_status 产出）。 */
      type: "task_status";
      status: {
        tasks: Array<{
          id: string;
          kind: string;
          name: string;
          status: string;
          is_high_risk?: boolean;
          executed_at?: string | null;
          receipt_summary?: string | null;
        }>;
        total?: number;
        /** L4 血缘：会话内任务间依赖（谁产出谁消费）。 */
        lineage?: {
          tasks: Array<{
            task_id: string;
            label?: string;
            artifact_id?: string;
            source_urns?: string[];
            target_urn?: string;
          }>;
          dependencies: Array<{ upstream: string; downstream: string }>;
        };
      };
    }
  | { id: string; type: "record"; record: ChatBiOpsRecord }
  | {
      id: string;
      /** P3.1：记忆提案（跨会话约定）。agent 只提案，点「记住」才写入本域约定。 */
      type: "preference_proposal";
      proposal: { kind: string; text: string; domain_id?: string | null };
    }
  | { id: string; type: "clarify"; clarification: ChatBiClarification }
  | { id: string; type: "form"; form: ChatBiFormRequest };

export interface ChatBiAgentRun {
  id: string;
  status: "succeeded" | "refused" | "waiting_input" | "failed" | "cancelled";
  question: string;
  intent?: string | null;
  skill?: string | null;
  grounded: boolean;
  started_at: string;
  finished_at: string;
  error?: string | null;
}

export interface ChatBiAgentArtifact {
  id: string;
  kind: string;
  label: string;
  payload_path: string;
  snapshot?: Record<string, unknown> | null;
  source?: string | null;
  as_of?: unknown;
}

export interface ChatBiAgentRunSummary extends ChatBiAgentRun {
  message_id: string;
  artifact_count: number;
  answer_preview: string;
  created_at: string;
}

export interface ChatBiAgentRunDetail {
  message_id: string;
  run: ChatBiAgentRun;
  artifacts: ChatBiAgentArtifact[];
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ChatBiAnswer {
  domain_ids?: string[];
  domain_names?: string[];
  domain_id?: string | null;
  domain_name?: string;
  ontology_id?: string | null;
  answer: string;
  suggested_sql?: string | null;
  caliber_decomposition?: ChatBiCaliberItem[];
  referenced_objects?: ChatBiReference[];
  referenced_logics?: ChatBiReference[];
  used_mock: boolean;
  grounding_refused?: boolean;
  /**
   * 需要用户澄清的缺口（P4.1）。与 grounding_refused 是**两种不同结局**：
   * 拒答是「答不了」，澄清是「先确认再答」。
   */
  clarification?: ChatBiClarification | null;
  /** P6：需一次补齐多个结构化参数时，Agent 生成的可填写表单（终态出口）。 */
  form_request?: ChatBiFormRequest | null;
  /** V6：物理落点/运行记录读模型。 */
  ops_records?: ChatBiOpsRecord[];
  steps?: ChatBiAgentStep[];
  data_result?: ChatBiDataResult | null;
  /** V3 S0 渲染块（后端双写）；缺失时前端 answerToBlocks 由旧字段兜底。 */
  blocks?: ChatBiBlock[];
  /** P4：持久化 run 信封与本轮结构化制品索引。 */
  agent_run?: ChatBiAgentRun | null;
  agent_artifacts?: ChatBiAgentArtifact[];
  conversation_id?: string | null;
  conversation_title?: string | null;
}

export type ChatBiStreamEvent =
  | { type: "meta"; conversation_id: string; conversation_title?: string | null; run_id?: string }
  | { type: "step_start"; index: number; tool: string; arguments?: Record<string, unknown> }
  | { type: "step_done"; index: number; status: "succeeded" | "failed"; summary?: string | null }
  | { type: "thought"; index: number; text: string }
  /** 答案未过可靠性校验，正在让模型重写一次（P4.3 自愈回环）。 */
  | { type: "repair"; reasons: string[] }
  | { type: "token"; delta: string }
  | { type: "done"; payload: ChatBiAnswer }
  | { type: "error"; message: string; run_id?: string };

export interface ChatBiSuggestions {
  domain_ids?: string[];
  domain_id?: string;
  suggestions: string[];
}

export interface ChatBiHistoryItem {
  role: "user" | "assistant";
  content: string;
}

// ---- 字段级溯源：合并报告与冲突复核 ----

export interface MergeReportSummary {
  added: number;
  updated: number;
  kept: number;
  conflict: number;
  removed: number;
}

export interface MergeReportItem {
  id: string;
  name: string;
  display_name: string;
  fields?: string[];
  conflicts?: Record<string, { base?: unknown; ours?: unknown; theirs?: unknown }>;
}

export type MergeReportSection = {
  added: MergeReportItem[];
  updated: MergeReportItem[];
  kept: MergeReportItem[];
  conflict: MergeReportItem[];
  removed: MergeReportItem[];
};

export interface MergeReport {
  task_id: string;
  scope?: string;
  summary: MergeReportSummary;
  object_types: MergeReportSection;
  properties: MergeReportSection;
  relation_types: MergeReportSection;
  business_logics: MergeReportSection;
  segments: MergeReportSection;
}

export interface ConflictItem {
  entity_type: string; // object_type | property | relation_type | business_logic
  entity_id: string;
  name: string;
  display_name: string;
  field: string;
  base?: unknown;
  ours?: unknown;
  theirs?: unknown;
}

export interface OntologyConflicts {
  ontology_id: string;
  items: ConflictItem[];
  total: number;
}

// ------------------------------------------------------------ Data App (数据应用)

export interface DataAppBindingRef {
  kind: "object_type" | "property" | "business_logic";
  id?: string | null;
  name?: string | null;
  display_name?: string | null;
}

export interface DataAppMeasure {
  ref: DataAppBindingRef;
  agg: string; // sum / count / avg / max / min
}

export interface DataAppFilter {
  ref: DataAppBindingRef;
  op: string; // eq / ne / gt / lt / ge / le / like
  value?: unknown;
}

export interface DataAppTimeRange {
  ref?: DataAppBindingRef | null;
  window?: string | null; // last_7d / last_30d / today / this_month
}

export interface DataAppBinding {
  primary_object_type_id?: string | null;
  measures: DataAppMeasure[];
  dimensions: DataAppBindingRef[];
  filters: DataAppFilter[];
  time_range?: DataAppTimeRange | null;
  row_limit: number;
}

export interface DataAppDataset {
  id: string;
  app_id: string;
  name: string;
  primary_object_type_id?: string | null;
  binding: DataAppBinding;
  compiled_sql?: string | null;
  data_source_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataAppSummary {
  id: string;
  domain_id: string;
  app_type: "data_table" | "screen" | "dashboard";
  name: string;
  description?: string | null;
  status: string; // draft / published / archived
  source: string; // manual / chat_generated
  current_version: number;
  published_version?: number | null;
  published_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataAppDetail extends DataAppSummary {
  ontology_id?: string | null;
  spec?: Record<string, unknown> | null;
  datasets: DataAppDataset[];
}

export interface DataAppColumn {
  key: string;
  title: string;
}

export interface DataAppPreviewResult {
  dataset_id?: string | null;
  compiled_sql?: string | null;
  columns: DataAppColumn[];
  rows: Record<string, unknown>[];
  used_mock: boolean;
  warnings: string[];
}

export interface DataAppVersion {
  id: string;
  app_id: string;
  version: number;
  diff_summary?: string | null;
  operator?: string | null;
  created_at: string;
}

export interface DataAppDatasetInput {
  id?: string;
  name?: string;
  primary_object_type_id?: string | null;
  binding: DataAppBinding;
  data_source_id?: string | null;
}

export interface DorisWarehouseConfig {
  id: string;
  warehouse_datasource_id: string;
  enabled: boolean;
  query_host?: string | null;
  query_port: number;
  default_catalog: string;
  default_database?: string | null;
  connect_timeout_seconds: number;
  query_timeout_seconds: number;
  ssl_enabled: boolean;
  fenodes: string[];
  benodes?: string[];
  airflow_ddl_conn_id?: string | null;
  airflow_etl_conn_id?: string | null;
  airflow_flink_conn_id?: string | null;
  reader_dsn_set: boolean;
  reader_dsn_hint?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface DorisWarehouseConfigInput {
  warehouse_datasource_id: string;
  enabled?: boolean;
  query_host?: string | null;
  query_port?: number;
  default_catalog?: string;
  default_database?: string | null;
  connect_timeout_seconds?: number;
  query_timeout_seconds?: number;
  ssl_enabled?: boolean;
  fenodes?: string[];
  benodes?: string[];
  airflow_ddl_conn_id?: string | null;
  airflow_etl_conn_id?: string | null;
  airflow_flink_conn_id?: string | null;
  reader_dsn_secret_ref?: string | null;
}

export interface DataSource {
  id: string;
  name: string;
  kind: string; // sqlite / duckdb / postgres / mysql / mock / doris
  purpose?: "business_source" | "warehouse";
  is_default_warehouse?: boolean;
  enabled?: boolean;
  status: string; // untested / ok / error
  mapping?: { tables?: Record<string, string>; columns?: Record<string, string> } | null;
  tested_at?: string | null;
  created_at: string;
  updated_at: string;
  // 连接信息回显：只返回 password_set/password_hint，不返回密码明文。
  dsn_set?: boolean;
  host?: string | null;
  port?: number | null;
  database?: string | null;
  username?: string | null;
  password?: string | null;
  password_set?: boolean;
  password_hint?: string | null;
  path?: string | null; // 文件类（sqlite/duckdb）
}

export interface RuntimeFilter {
  ref: { kind: string; id?: string | null; name?: string | null; display_name?: string | null };
  op: string; // eq / ne / gt / lt / like
  value?: unknown;
}

export interface ScreenParam {
  id: string;
  label: string;
  column: string; // 物理/本体列名，用于匹配各数据集
  op?: string; // 默认 eq
  default?: string;
}

export interface DataAppWidget {
  id: string;
  domain_id: string;
  ontology_id?: string | null;
  name: string;
  description?: string | null;
  widget_type: string; // table/bar/kpi/line/pie
  primary_object_type_id?: string | null;
  binding: DataAppBinding;
  viz?: Record<string, unknown> | null;
  compiled_sql?: string | null;
  data_source_id?: string | null;
  status: string;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface PublicShareStatus {
  public_enabled: boolean;
  public_token?: string | null;
  password_set: boolean;
  public_expires_at?: string | null;
}

// ---- 物化契约（M1）----
// 本体是一级源数据、物理表是二级投影；契约补齐本体不承载的落地配置。

export type MaterializationTargetKind = "object_type" | "relation_type" | "business_logic";

/** 物化任务选择树：某本体下一个可物化实体（业务对象 / 事实·桥表关系）+ 自动表名。 */
export interface MaterializeTargetEntity {
  name: string;
  display_name?: string | null;
  kind: MaterializationTargetKind;
  layer: string;
  table: string;
}
export interface MaterializeTargetOntology {
  ontology_id: string;
  domain_name: string;
  version: number;
  status: string;
  entities: MaterializeTargetEntity[];
}
export interface MaterializeTargetsResult {
  ontologies: MaterializeTargetOntology[];
}
export type MaterializationLayer = "dim" | "dwd" | "dws" | "ads";
export type MaterializationLoadStrategy = "full" | "incremental" | "cdc";
export type MaterializationScdType = "none" | "scd1" | "scd2";

export interface IngestionContract {
  id: string;
  ontology_id: string;
  ontology_version: number;
  object_type_id: string;
  source_datasource_id: string;
  source_physical_table: string;
  source_mapping: Record<string, string>;
  doris_datasource_id: string;
  target_ods_database: string;
  target_ods_table: string;
  mode: MaterializationLoadStrategy;
  primary_keys: string[];
  sequence_column?: string | null;
  incremental_column?: string | null;
  initial_watermark?: string | null;
  late_arrival_policy: string;
  idempotency_strategy: string;
  delete_policy: "ignore" | "soft_delete" | "hard_delete";
  refresh_cron?: string | null;
  flink_params: Record<string, unknown>;
  status: string;
  last_success_at?: string | null;
  sync_watermark?: string | null;
  flink_job_id?: string | null;
  checkpoint_path?: string | null;
  savepoint_path?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export type IngestionContractInput = Omit<
  IngestionContract,
  | "id"
  | "target_ods_table"
  | "ontology_id"
  | "ontology_version"
  | "last_success_at"
  | "sync_watermark"
  | "flink_job_id"
  | "checkpoint_path"
  | "savepoint_path"
  | "created_at"
  | "updated_at"
>;

export interface MaterializationContract {
  id: string;
  ontology_id: string;
  target_kind: MaterializationTargetKind;
  target_id: string;
  target_name?: string | null;
  target_display_name?: string | null;
  target_layer: MaterializationLayer;
  engines: string[];
  load_strategy: MaterializationLoadStrategy;
  partition_key?: string | null;
  scd_type: MaterializationScdType;
  refresh_cron?: string | null;
  materialized: boolean;
  /** 机器推导的判定依据，供人工复核 */
  derivation_reason?: string | null;
  origin: string;
  /** 被人工钉住的列名；机器重推导不会覆盖这些字段 */
  pinned_fields: string[];
  created_at: string;
  updated_at: string;
}

export interface MaterializationContractUpdateInput {
  target_layer?: MaterializationLayer;
  engines?: string[];
  load_strategy?: MaterializationLoadStrategy;
  partition_key?: string | null;
  scd_type?: MaterializationScdType;
  refresh_cron?: string | null;
  materialized?: boolean;
}

export interface MaterializationContractSyncResult {
  ontology_id: string;
  created: number;
  updated: number;
  skipped_pinned: number;
  total: number;
}

export interface MaterializationReceipt {
  ontology_id: string;
  /** 物化总是交 Airflow 编排（已去除直连落库模式）。 */
  execute_mode?: "orchestrated";
  /** 前置条件就没过的提交（目标源缺连接串、搬运工具无可用镜像…）只回一个 error，
   *  下面这些字段都不会有。声明为可选是照实描述，好让 TS 逼出取值处的判空。 */
  target_datasource?: { id: string; name: string; kind: string };
  engine?: string;
  database_prefix?: string | null;
  tables?: string[];
  /** orchestrated 提交回执：建表与搬运由 Airflow 执行，成败看 DagRun。 */
  dag_id?: string;
  dag_run_id?: string;
  state?: string;
  run_url?: string;
  schedule?: string | null;
  jobs?: string[];
  artifacts?: Record<string, string>;
  /** M16：一次物化可产多个 DAG（按 cron 分组 + 分批）。顶层字段指向首批，此处是全量。 */
  batches?: MaterializeBatch[];
  error?: string | null;
  schema_notes?: { target: string; reason: string }[];
  warnings?: { target: string; feature: string; detail: string }[];
  unsupported?: { target: string; reason: string }[];
  ok: boolean;
}

/** M16：一次物化里的一个 DAG（一个 cron 分组的一个分批）。 */
export interface MaterializeBatch {
  suffix?: string;
  dag_id: string | null;
  dag_run_id: string | null;
  state: string | null;
  terminal?: boolean;
  run_url?: string | null;
  schedule?: string | null;
  tables?: string[];
  jobs?: string[];
  error?: string | null;
  tasks?: { task_id: string; state: string | null; try_number?: number }[];
}

/** 编排物化的运行状态（轮询 status 端点获得；权威在 Airflow，前端不缓存）。 */
export interface MaterializeStatus {
  artifact_id: string;
  dag_id: string;
  dag_run_id: string;
  state: string | null;
  terminal: boolean;
  start_date?: string | null;
  end_date?: string | null;
  run_url: string;
  tasks: { task_id: string; state: string | null; try_number?: number }[];
  /** M16：各批 DagRun 的明细（顶层 state 是它们的聚合）。 */
  batches?: MaterializeBatch[];
}

/** 一个搬运任务的执行结果（来自该任务的 XCom）。 */
export interface MaterializeTaskResult {
  task_id: string;
  dag_id: string | null;
  task_state?: string | null;
  ingestion_status?: string | null;
  job_id?: string | null;
  rows_read?: number | null;
  rows_written?: number | null;
  watermark_after?: string | null;
  /** 形状不合预期时原样带出的 XCom 值。 */
  raw?: unknown;
}

/** M11：物化血缘上报计划/回执（源表 → 目标表）。 */
export interface LineageEdgeView {
  source_urn: string;
  target_urn: string;
  target_table: string;
  columns: { source: string; target: string }[];
  skipped_reason: string | null;
}

export interface LineageEmitResult {
  ontology_id: string;
  blocked_reason: string | null;
  total: number;
  applicable: number;
  skipped: number;
  column_mappings: number;
  edges: LineageEdgeView[];
  applied?: number;
  failed?: number;
  errors?: { target: string; source_urn: string; error: string }[];
}

/** M13：物化提交前自检的单项结果。 */
export interface PreflightItem {
  key: string;
  label: string;
  status: "pass" | "warn" | "fail";
  /** 为真且 fail 时应禁用提交；提醒项（warn / 非阻断 fail）可忽略。 */
  blocking: boolean;
  detail: string;
  next_step?: string | null;
}

export interface MaterializePreflightResult {
  /** 无阻断失败即可提交。 */
  ok: boolean;
  items: PreflightItem[];
}

export interface MaterializeRequestInput {
  target_datasource_id: string;
  engine: string;
  database_prefix?: string | null;
  /** 分层 → 目标库名；命中的层不再按「层[_前缀]」生成库名。 */
  database_overrides?: Record<string, string>;
  /** 契约 id → 物理表名；缺省用实体技术名。 */
  table_overrides?: Record<string, string>;
  selected_targets?: string[] | null;
  overrides?: Record<string, MaterializationContractUpdateInput>;
  intent?: string;
  operator?: string;
}

/** 物化执行记录（治理制品回执视图）。 */
export interface MaterializationRun {
  artifact_id: string;
  status: string; // succeeded / failed / …
  ok: boolean;
  name: string;
  receipt?: MaterializationReceipt | null;
  executed_at?: string | null;
  operator?: string | null;
  created_at?: string | null;
}

// ---- RBAC 主体与角色（M0）----

export type PrincipalRole = "reader" | "editor" | "reviewer" | "publisher";

export interface Principal {
  id: string;
  name: string;
  role: PrincipalRole;
  token_prefix: string;
  active: boolean;
  last_used_at?: string | null;
  created_at: string;
  updated_at: string;
}

/** 创建/轮换时返回，token 明文仅此一次。 */
export interface PrincipalCreated extends Principal {
  token: string;
}

export interface RolePolicy {
  roles: PrincipalRole[];
  method_defaults: Record<string, string>;
  overrides: { method: string; path_pattern: string; minimum_role: string }[];
}

// ---- 治理智能体制品（M5/M6，写侧）----

export type ArtifactKind = "sync" | "transform" | "metric";
export type ArtifactStatus =
  "drafted" | "validated" | "confirmed" | "executing" | "succeeded" | "failed";

export interface AgentValidationIssue {
  code: string;
  message: string;
  entity_type?: string | null;
  entity_id?: string | null;
  entity_name?: string | null;
  /** 后端 is_blocking 的判据（唯一真源）。旧报告没有这个字段，前端按码表兜底。 */
  blocking?: boolean;
}

export interface AgentValidationReport {
  issues: AgentValidationIssue[];
  blocking_count: number;
  /** dry-run 差异（将要发生什么）；有阻断项时为 null */
  dry_run?: Record<string, unknown> | null;
  dry_run_error?: string | null;
  validated_at?: string;
}

export interface GovernanceArtifact {
  id: string;
  kind: ArtifactKind | string;
  name: string;
  ontology_id?: string | null;
  intent?: string | null;
  spec: Record<string, unknown>;
  status: ArtifactStatus | string;
  is_high_risk: boolean;
  validation_report?: AgentValidationReport | null;
  execution_receipt?: Record<string, unknown> | null;
  /** Airflow DagRun 实时态（best-effort 回读）。materialize 制品的 status 在提交 DAG 后即 succeeded，
   * 但 DAG 在 Airflow 里可能还在跑——实时权威在此。读不到即为 null，退回 status。*/
  live_state?: { live_state: string; terminal: boolean; run_url?: string } | null;
  confirmed_by?: string | null;
  confirmed_at?: string | null;
  executed_at?: string | null;
  origin: string;
  created_at: string;
  updated_at: string;
}

/**
 * 任务链的一步：**先是一份待起草的意图，起草后才有制品**。
 *
 * `artifact_id` 为空 = 还没起草到这一步；此时 `artifact_status` 也是 null，
 * 不是 "drafted"——那会让人以为已经建了一条制品。
 */
export interface TaskPipelineStep {
  id: string;
  step_index: number;
  kind: string;
  intent: string;
  context: Record<string, unknown>;
  artifact_id?: string | null;
  artifact_status?: string | null;
  artifact_name?: string | null;
  /** C2：血缘依赖（上游步序列表）。空 = 线性默认（依赖上一步）。 */
  depends_on?: number[];
}

/**
 * 任务链：把「物化 → 清洗 → 聚合」这种前后相继的任务串起来。
 *
 * 链只管顺序与上下文传递——每一步仍是一条独立制品，照旧各自走「校验 → dry-run →
 * 人工确认 → 执行」。故这里没有、也不该有「一键跑完整条链」。
 */
export interface TaskPipeline {
  id: string;
  name: string;
  intent?: string | null;
  ontology_id?: string | null;
  /** 由各步制品聚合推导：drafted / running / succeeded / failed。 */
  status: string;
  steps: TaskPipelineStep[];
  /** 下一个待起草的步序；全起草完为 null。 */
  next_step_index?: number | null;
  /** 下一步为什么还不能起草（上游没跑成功）；能起草为 null。 */
  next_blocked_reason?: string | null;
  // P2：编译成周期 DAG 的状态
  schedule_cron?: string | null;
  compiled_dag_id?: string | null;
  compiled_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskPipelineAdvanceResult {
  pipeline: TaskPipeline;
  artifact: GovernanceArtifact;
}

export interface TaskPipelineDraftAllResult {
  pipeline: TaskPipeline;
  artifacts: GovernanceArtifact[];
}

/** 编译成周期 DAG 的结果（P2）。 */
export interface PipelineCompileResult {
  pipeline_id: string;
  compiled_dag_id: string;
  schedule_cron: string;
  steps: {
    step_index: number;
    kind: string;
    artifact_id: string;
    dag_ids: string[];
  }[];
  dag_path: string;
  spec_path: string;
}

export interface AgentKinds {
  all_kinds: string[];
  registered: string[];
  high_risk: string[];
}
