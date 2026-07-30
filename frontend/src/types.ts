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
  latest_ontology_id?: string;
  latest_ontology_status?: string;
  published_ontology_id?: string;
  published_ontology_version?: number;
}

export type DraftGenerationScope = "full" | "objects" | "relations";

export interface DraftProgress {
  task_id: string;
  status: string;
  progress: number;
  message?: string;
  ontology_id?: string;
  scope: DraftGenerationScope;
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

export interface ObjectTypeSummary extends FieldProvenance {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  status: string;
  property_count: number;
  relation_count: number;
  business_logic_count: number;
  bound_logic_count?: number;
  source_confidence?: number;
  table_role?: string;
  role_confidence?: number;
  role_reason?: string;
  domain_context_id?: string;
  domain_name?: string;
  updated_at: string;
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
  use_mock: boolean;
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
  use_mock: boolean;
  updated_at: string;
}

export interface DraftGenerationSettings {
  object_chunk_concurrency: number;
  relation_chunk_concurrency: number;
  updated_at: string;
}

export interface CubeSettings {
  api_url: string;
  secret_set: boolean;
  secret_hint?: string | null;
  use_mock: boolean;
  preagg_refresh: string;
  tenant_dimension?: string | null;
  timeout_seconds: number;
  updated_at: string;
}

export interface ChatBiConversation {
  id: string;
  domain_id: string;
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

export interface ChatBiReference {
  id?: string | null;
  name?: string | null;
  display_name?: string | null;
}

export type ChatBiCaliberKind =
  | "object_type"
  | "property"
  | "relation_type"
  | "business_logic";

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

export interface ChatBiAnswer {
  domain_id: string;
  domain_name: string;
  ontology_id?: string | null;
  answer: string;
  suggested_sql?: string | null;
  caliber_decomposition?: ChatBiCaliberItem[];
  referenced_objects?: ChatBiReference[];
  referenced_logics?: ChatBiReference[];
  used_mock: boolean;
  grounding_refused?: boolean;
  conversation_id?: string | null;
  conversation_title?: string | null;
}

export interface ChatBiSuggestions {
  domain_id: string;
  suggestions: string[];
}

export interface ChatBiHistoryItem {
  role: "user" | "assistant";
  content: string;
}

// --- External API ---

export interface ExternalApp {
  id: string;
  name: string;
  description?: string | null;
  app_key: string;
  api_key_hint?: string | null;
  api_key?: string | null;
  scopes: string[];
  rate_limit_per_minute?: number | null;
  status: string;
  created_at: string;
  updated_at: string;
  last_used_at?: string | null;
}

export interface ExternalAppCreated extends ExternalApp {
  api_key: string;
}

export interface ExternalApiFieldDoc {
  name: string;
  type: string;
  description: string;
}

export interface ExternalApiCatalogItem {
  id: string;
  name: string;
  tool_name: string;
  category: string;
  description: string;
  auth_required: boolean;
  required_scope: string;
  rest_method?: string | null;
  rest_path?: string | null;
  input_schema: Record<string, unknown>;
  output_fields: ExternalApiFieldDoc[];
  example_result?: unknown;
  mcp_endpoint: string;
}

export interface ExternalApiCallLog {
  id: string;
  app_id: string;
  tool_name?: string | null;
  path?: string | null;
  status_code: number;
  duration_ms?: number | null;
  error_message?: string | null;
  created_at: string;
}

export interface McpToolCallResult {
  content: Array<{ type: string; text?: string; [key: string]: unknown }>;
  structuredContent?: unknown;
  isError?: boolean;
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

export interface DataSource {
  id: string;
  name: string;
  kind: string; // sqlite / duckdb / postgres / mysql / mock
  status: string; // untested / ok / error
  mapping?: { tables?: Record<string, string>; columns?: Record<string, string> } | null;
  tested_at?: string | null;
  created_at: string;
  updated_at: string;
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

export type MaterializationTargetKind =
  | "object_type"
  | "relation_type"
  | "business_logic";
export type MaterializationLayer = "dim" | "dwd" | "dws" | "ads";
export type MaterializationLoadStrategy = "full" | "incremental" | "cdc";
export type MaterializationScdType = "none" | "scd1" | "scd2";

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

/** 一次落库执行的逐条结果（DDL/ETL 各一批）。 */
export interface MaterializationPhaseReceipt {
  total: number;
  executed: number;
  failed: number;
  error?: string | null;
  skipped?: boolean;
  skip_reason?: string;
  targets?: string[];
  per_statement?: {
    index: number;
    ok: boolean;
    target?: string;
    error?: string;
    rolled_back?: boolean;
    sql?: string;
  }[];
}

export interface MaterializationReceipt {
  ontology_id: string;
  target_datasource: { id: string; name: string; kind: string };
  engine: string;
  database_prefix?: string | null;
  tables: string[];
  ddl: MaterializationPhaseReceipt;
  etl: MaterializationPhaseReceipt;
  warnings?: { target: string; feature: string; detail: string }[];
  unsupported?: { target: string; reason: string }[];
  ok: boolean;
}

export interface MaterializeRequestInput {
  target_datasource_id: string;
  engine: string;
  database_prefix?: string | null;
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

export type ArtifactKind = "cluster" | "sync" | "transform" | "metric";
export type ArtifactStatus =
  | "drafted"
  | "validated"
  | "confirmed"
  | "executing"
  | "succeeded"
  | "failed";

export interface AgentValidationIssue {
  code: string;
  message: string;
  entity_type?: string | null;
  entity_id?: string | null;
  entity_name?: string | null;
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
  confirmed_by?: string | null;
  confirmed_at?: string | null;
  executed_at?: string | null;
  origin: string;
  created_at: string;
  updated_at: string;
}

export interface AgentKinds {
  all_kinds: string[];
  registered: string[];
  high_risk: string[];
}
