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
  /** 源表定位（DataHub urn）。为空 = 无法建同步任务（SyncDrafter 会拒）。 */
  source_ref?: string;
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

export interface CubeSettings {
  api_url: string;
  secret_set: boolean;
  secret_hint?: string | null;
  preagg_refresh: string;
  tenant_dimension?: string | null;
  timeout_seconds: number;
  updated_at: string;
}

/** Airflow 编排配置。凭据只回「是否已设 + 掩码」，不回明文。 */
export interface AirflowSettings {
  endpoint: string;
  username?: string | null;
  password_set: boolean;
  password_hint?: string | null;
  token_set: boolean;
  api_version: string;
  enabled: boolean;
  /** 启用且 endpoint 已填才算真的可用；否则物化报错无法执行。 */
  available: boolean;
  /** DAG 与作业配置的投递目录（必须是 Airflow 真正挂进容器的那个）。 */
  dags_dir: string;
  jobs_dir: string;
  /** DAG 投递方式：local（写共享目录）/ git（commit+push，Airflow 侧 git-sync 拉取）。 */
  dag_delivery_method: string;
  /** git 投递参数（仅 dag_delivery_method=git 时生效）。 */
  git_remote: string;
  git_branch: string;
  git_auto_init: boolean;
  git_author: string;
  git_email: string;
  max_tasks_per_dag: number;
  max_active_tasks_per_dag: number;
  dag_parse_timeout: number;
  preflight_sentinel_timeout: number;
  staging_swap: boolean;
  updated_at: string;
}

/** runner 侧一个别名的连接配置概览。**不含机密明文**——机密键只回「已设置」。 */
export interface SyncRunnerSecret {
  alias: string;
  /** store：设置页写入 runner 存储，可改；env：部署时钉死的环境变量，只读。 */
  source: string;
  values: Record<string, string>;
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
export interface DependencySchema {
  components: DependencyComponentMeta[];
  connection_schemas: Record<string, DependencySchemaField[]>;
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
}

/**
 * Agent 动态生成的可填写表单（P6）：一次向用户收集多个结构化参数。
 * 与 clarification 同为终态出口——本轮结束、等用户填完提交带回（结构化回填文本进 history）。
 */
export interface ChatBiFormRequest {
  title: string;
  intent?: string;
  submit_label?: string;
  fields: ChatBiFormField[];
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
        /** 新建：POST /api/business-logics。 */
        create_payload?: BusinessLogicCreateInput;
        /** 给已有口径补表达式：PATCH /api/business-logics/{logic_id}。 */
        logic_id?: string;
        update_payload?: {
          logic_type?: string;
          expression_summary?: string;
          expression_json?: Record<string, unknown>;
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
  | {
      id: string;
      /** P3.1：记忆提案（跨会话约定）。agent 只提案，点「记住」才写入本域约定。 */
      type: "preference_proposal";
      proposal: { kind: string; text: string; domain_id?: string | null };
    }
  | { id: string; type: "clarify"; clarification: ChatBiClarification }
  | { id: string; type: "form"; form: ChatBiFormRequest };

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
  steps?: ChatBiAgentStep[];
  data_result?: ChatBiDataResult | null;
  /** V3 S0 渲染块（后端双写）；缺失时前端 answerToBlocks 由旧字段兜底。 */
  blocks?: ChatBiBlock[];
  conversation_id?: string | null;
  conversation_title?: string | null;
}

export type ChatBiStreamEvent =
  | { type: "meta"; conversation_id: string; conversation_title?: string | null }
  | { type: "step_start"; index: number; tool: string; arguments?: Record<string, unknown> }
  | { type: "step_done"; index: number; status: "succeeded" | "failed"; summary?: string | null }
  | { type: "thought"; index: number; text: string }
  /** 答案未过可靠性校验，正在让模型重写一次（P4.3 自愈回环）。 */
  | { type: "repair"; reasons: string[] }
  | { type: "token"; delta: string }
  | { type: "done"; payload: ChatBiAnswer }
  | { type: "error"; message: string };

export interface ChatBiSuggestions {
  domain_ids?: string[];
  domain_id?: string;
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
  // 连接的非机密部分，供编辑弹窗回显（密码不回显，仅 password_set 标志）。
  dsn_set?: boolean;
  host?: string | null;
  port?: number | null;
  database?: string | null;
  username?: string | null;
  password_set?: boolean;
  path?: string | null; // 文件类（sqlite/duckdb）
  url?: string | null; // cube 语义层地址
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
export type SyncTool = "flink";

/** 本次物化**会用什么搬**（GET /warehouse/sync-tools）。
 *
 * 统一执行架构下搬运恒为 Flink SQL on YARN，不再有工具选择。这里是恒定结果的告知。
 */
export interface SyncToolPlan {
  channel: "flink_on_yarn";
  /** 本次会用的工具；统一架构下恒为 flink。 */
  resolved: string | null;
  /** 是否为自动决策。统一架构下恒为 true。 */
  auto: boolean;
  /** 一句可解释的理由，直接展示。 */
  detail: string;
  /** 选中的工具覆盖不了的装载方式（统一架构下恒为空）。 */
  uncovered_modes: MaterializationLoadStrategy[];
  /** 选不出工具时的原因；正常为 null。 */
  error: string | null;
  /**
   * 目标引擎在执行侧真正支持的装载方式。统一架构下恒为 full/incremental/cdc 全集
   * （Flink SQL 支持所有引擎），供弹窗置灰「同步方式」。
   */
  modes: MaterializationLoadStrategy[] | null;
  /** modes 的来源说明。 */
  modes_detail: string;
  default: SyncTool;
}
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

export interface MaterializationReceipt {
  ontology_id: string;
  /** 物化总是交 Airflow 编排（已去除直连落库模式）。 */
  execute_mode?: "orchestrated";
  /** 前置条件就没过的提交（目标源缺连接串、搬运工具无可用镜像…）只回一个 error，
   *  下面这些字段都不会有。声明为可选是照实描述，好让 TS 逼出取值处的判空。 */
  target_datasource?: { id: string; name: string; kind: string };
  engine?: string;
  /** 这次实际用什么搬（"auto" = runner 逐表自选档位）+ 为什么是它。 */
  sync_tool?: string | null;
  sync_tool_detail?: string | null;
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

/** 一个搬运任务的执行结果（来自该任务的 XCom）。
 *
 * runner 逐表自选档位，``backend`` 就是这张表实际用的那一档（native / seatunnel）。
 * 任务还没跑完、或它是建表/切换任务（不产 XCom）时全为 null——这不是错误。
 */
export interface MaterializeTaskResult {
  task_id: string;
  dag_id: string | null;
  backend?: string | null;
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
  load_strategy?: MaterializationLoadStrategy | null;
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
