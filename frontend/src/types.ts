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

export interface ObjectTypeSummary {
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
  domain_context_id?: string;
  domain_name?: string;
  updated_at: string;
}

export interface Property {
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

export interface RelationType {
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

export interface ObjectTypeDetail extends ObjectTypeSummary {
  ontology_id?: string;
  domain_context_id?: string;
  domain_name?: string;
  source_ref?: string;
  datahub_url?: string;
  properties: Property[];
  outgoing_relations: RelationType[];
  incoming_relations: RelationType[];
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
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  cardinality?: string;
  relationId?: string;
  relation_id?: string;
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
