import type {
  BusinessLogic,
  BusinessLogicCategory,
  BusinessLogicCreateInput,
  BusinessLogicDetail,
  BusinessLogicImportInput,
  BusinessLogicObjectBinding,
  BusinessLogicPropertyBinding,
  BusinessLogicUpdateInput,
  ChangeLog,
  MergeReport,
  OntologyConflicts,
  ChatBiAnswer,
  ChatBiCategoryList,
  ChatBiConversation,
  ChatBiDecision,
  ChatBiDecisionClosure,
  ChatBiFormRequest,
  ChatBiHistoryItem,
  ChatBiMessageItem,
  ChatBiStreamEvent,
  ChatBiSuggestions,
  Confirmation,
  DataHubDatasetOption,
  AirflowSettings,
  DatahubSettings,
  CubeSettings,
  DomainContext,
  DomainContextDetail,
  DraftGenerationScope,
  DraftGenerationSettings,
  DraftProgress,
  ExpressionDraft,
  ExpressionJson,
  DataAppSummary,
  DataAppDetail,
  DataAppPreviewResult,
  DataAppVersion,
  DataAppDatasetInput,
  DataAppBinding,
  DataAppWidget,
  PublicShareStatus,
  DataSource,
  DorisWarehouseConfig,
  DorisWarehouseConfigInput,
  LlmModelOption,
  LlmServiceConfig,
  IngestionContract,
  IngestionContractInput,
  MaterializationContract,
  MaterializationContractSyncResult,
  MaterializationContractUpdateInput,
  MaterializationTargetKind,
  MaterializeTargetsResult,
  MaterializationRun,
  MaterializeRequestInput,
  MaterializeStatus,
  MaterializeTaskResult,
  MaterializePreflightResult,
  SyncToolPlan,
  LineageEmitResult,
  Principal,
  PrincipalCreated,
  PrincipalRole,
  RolePolicy,
  AgentKinds,
  GovernanceArtifact,
  TaskPipeline,
  TaskPipelineAdvanceResult,
  TaskPipelineDraftAllResult,
  PipelineCompileResult,
  LlmConnectionTestResult,
  ObjectTypeDetail,
  ObjectTypeSummary,
  OntologyGraph,
  OntologyGroupedGraph,
  ClusterDetail,
  OntologySummary,
  PageResult,
  Property,
  RelationGroup,
  RelationType,
  RelationTypeDetail,
  TaskRecord,
  OntologyValidationResult,
  FormalValidationResult,
  VersionDiff,
  VersionRecord,
  VersionSnapshot,
  DependencySchema,
  DependencyComponent,
  DependencyProbeResult,
  DependencyDeployResult,
  PublishPreflight,
} from "./types";
import { buildQuery } from "./utils/format";

const ADMIN_TOKEN_STORAGE_KEY = "ontometa_admin_token";

/** 读取管理 Token：优先 localStorage，其次 Vite 环境变量。 */
export function getAdminToken(): string {
  try {
    const fromStorage = localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    if (fromStorage?.trim()) return fromStorage.trim();
  } catch {
    // ignore (SSR / privacy mode)
  }
  const fromEnv = import.meta.env.VITE_ONTOMETA_ADMIN_TOKEN;
  return typeof fromEnv === "string" ? fromEnv.trim() : "";
}

export function setAdminToken(token: string): void {
  localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token.trim());
}

export function clearAdminToken(): void {
  localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
}

/** 携带 HTTP 状态码的 API 错误，便于调用方区分 403（权限不足）等情形。 */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  const headers = new Headers(options?.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const adminToken = getAdminToken();
  if (adminToken && !headers.has("X-Admin-Token")) {
    headers.set("X-Admin-Token", adminToken);
  }
  try {
    response = await fetch(path, {
      ...options,
      headers,
    });
  } catch (err) {
    throw new Error(
      `无法连接服务端 (${path})：${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!response.ok) {
    const raw = await response.text();
    let detail = raw || `请求失败：HTTP ${response.status}`;
    try {
      const parsed = JSON.parse(raw) as {
        detail?: string | Array<{ msg?: string }> | { message?: string; issues?: unknown };
      };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        detail = parsed.detail;
      } else if (
        parsed.detail &&
        typeof parsed.detail === "object" &&
        !Array.isArray(parsed.detail)
      ) {
        const obj = parsed.detail as { message?: string };
        if (obj.message) detail = obj.message;
      } else if (Array.isArray(parsed.detail)) {
        const joined = parsed.detail
          .map((item) => item.msg)
          .filter(Boolean)
          .join("；");
        if (joined) detail = joined;
      }
    } catch {
      // 响应不是 JSON（例如纯文本 "Internal Server Error"）
      detail = `服务端返回了非 JSON 响应（HTTP ${response.status}）：${raw.slice(0, 120)}`;
    }
    throw new ApiError(detail, response.status);
  }

  try {
    return (await response.json()) as T;
  } catch (err) {
    throw new Error(
      `服务端响应解析失败（${path}）：${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

export const api = {
  listDomains: () => request<DomainContext[]>("/api/domains"),
  getDomain: (id: string) => request<DomainContextDetail>(`/api/domains/${id}`),
  createManualObject: (
    domainId: string,
    body: {
      name: string;
      display_name: string;
      description?: string;
      dialect?: string;
      data_source?: string;
      properties: {
        name: string;
        display_name?: string;
        data_type?: string;
        semantic_type?: string;
        required?: boolean;
        primary_key?: boolean;
      }[];
    },
  ) =>
    request<{ ontology_id: string; object_type_id: string; table_name: string; ddl: string }>(
      `/api/domains/${domainId}/manual/object-types`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  generateDraft: (domainId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/generate-draft`, { method: "POST" }),
  generateObjects: (domainId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/generate-objects`, { method: "POST" }),
  generateRelations: (domainId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/generate-relations`, { method: "POST" }),
  getProgress: (domainId: string, scope?: DraftGenerationScope) =>
    request<DraftProgress>(`/api/domains/${domainId}/progress${buildQuery({ scope })}`),
  listTasks: (domainId: string) => request<TaskRecord[]>(`/api/domains/${domainId}/tasks`),
  stopDraftTask: (domainId: string, taskId: string) =>
    request<TaskRecord>(`/api/domains/${domainId}/tasks/${taskId}/stop`, { method: "POST" }),
  retryDraftTask: (domainId: string, taskId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/tasks/${taskId}/retry`, { method: "POST" }),
  getTaskLogs: (domainId: string, taskId: string) =>
    request<ChangeLog[]>(`/api/domains/${domainId}/tasks/${taskId}/logs`),
  publishPreflight: (ontologyId: string) =>
    request<PublishPreflight>(`/api/ontologies/${ontologyId}/publish-preflight`),
  discardUnpublished: (domainId: string) =>
    request<{ object_types: number; relation_types: number; properties: number }>(
      `/api/domains/${domainId}/discard-unpublished`,
      { method: "POST" },
    ),
  getMergeReport: (domainId: string, taskId: string) =>
    request<MergeReport>(`/api/domains/${domainId}/tasks/${taskId}/merge-report`),

  listOntologyConflicts: (ontologyId: string) =>
    request<OntologyConflicts>(`/api/ontologies/${ontologyId}/conflicts`),
  resolveConflict: (body: {
    entity_type: string;
    entity_id: string;
    field: string;
    resolution: "accept_theirs" | "keep_ours";
    operator?: string;
  }) =>
    request<{ id: string; field: string; resolution: string }>(`/api/conflicts/resolve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  resolveAllConflicts: (ontologyId: string, resolution: "accept_theirs" | "keep_ours") =>
    request<{ ontology_id: string; resolved: number; resolution: string }>(
      `/api/ontologies/${ontologyId}/conflicts/resolve-all${buildQuery({ resolution })}`,
      { method: "POST" },
    ),
  setFieldPin: (body: {
    entity_type: string;
    entity_id: string;
    field: string;
    pinned: boolean;
    operator?: string;
  }) =>
    request<{ id: string; field: string; pinned: boolean }>(`/api/fields/pin`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getConfig: () =>
    request<{ datahub_gms_url: string; datahub_frontend_url?: string }>("/api/config"),

  searchDatahubDatasets: (params?: { query?: string; ontologyId?: string }) =>
    request<DataHubDatasetOption[]>(
      `/api/datahub/datasets${buildQuery({
        query: params?.query,
        ontology_id: params?.ontologyId,
      })}`,
    ),

  ensureObjectTypeFromDataset: (body: {
    ontology_id: string;
    dataset_urn: string;
    operator?: string;
  }) =>
    request<ObjectTypeSummary>("/api/object-types/ensure", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateObjectType: (
    objectTypeId: string,
    body: {
      name?: string;
      display_name?: string;
      description?: string;
      table_role?: string;
      needs_review?: boolean;
    },
  ) =>
    request<ObjectTypeDetail>(`/api/object-types/${objectTypeId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  prePublishObjectType: (objectTypeId: string) =>
    request<ObjectTypeSummary>(`/api/object-types/${objectTypeId}/pre-publish`, {
      method: "PATCH",
    }),

  // 把被误判为业务对象的事实/明细/动作表转成一条业务关系（原表作为实现表）。
  convertObjectToRelation: (
    objectTypeId: string,
    body: {
      source_object_type_id: string;
      target_object_type_id: string;
      display_name: string;
      description?: string;
      cardinality?: string;
      structure_type?: string;
    },
  ) =>
    request<{
      relation: RelationType;
      retired_object: ObjectTypeSummary;
      promoted_endpoints: string[];
    }>(`/api/object-types/${objectTypeId}/convert-to-relation`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  batchUpdateObjectTypes: (body: {
    ids: string[];
    table_role?: string;
    needs_review?: boolean;
    operator?: string;
  }) =>
    request<{ updated: number; items: ObjectTypeSummary[] }>(`/api/object-types/batch`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  updateProperty: (
    propertyId: string,
    body: {
      display_name?: string;
      description?: string;
      data_type?: string;
      semantic_type?: string;
    },
  ) =>
    request<Property>(`/api/properties/${propertyId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  createRelationType: (body: {
    ontology_id: string;
    display_name: string;
    source_object_type_id: string;
    target_object_type_id: string;
    name?: string;
    description?: string;
    cardinality?: string;
    structure_type?: string;
    mapping_object_type_id?: string | null;
  }) =>
    request<RelationType>("/api/relation-types", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateRelationType: (
    relationTypeId: string,
    body: {
      display_name?: string;
      description?: string;
      cardinality?: string;
      structure_type?: string;
      mapping_object_type_id?: string | null;
      source_object_type_id?: string;
      target_object_type_id?: string;
    },
  ) =>
    request<RelationType>(`/api/relation-types/${relationTypeId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  prePublishRelationType: (relationTypeId: string) =>
    request<RelationType>(`/api/relation-types/${relationTypeId}/pre-publish`, {
      method: "PATCH",
    }),

  listRelationTypes: (params?: {
    ontologyId?: string;
    domainId?: string;
    publishedOnly?: boolean;
    q?: string;
    displayName?: string;
    limit?: number;
    offset?: number;
  }) =>
    request<PageResult<RelationType>>(
      `/api/relation-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
        q: params?.q,
        display_name: params?.displayName,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),

  listRelationGroups: (params?: {
    ontologyId?: string;
    domainId?: string;
    publishedOnly?: boolean;
    q?: string;
  }) =>
    request<RelationGroup[]>(
      `/api/relation-groups${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
        q: params?.q,
      })}`,
    ),

  getRelationType: (id: string, publishedOnly?: boolean) =>
    request<RelationTypeDetail>(
      `/api/relation-types/${id}${buildQuery({ published_only: publishedOnly })}`,
    ),

  listOntologies: (params?: { domainId?: string; publishedOnly?: boolean }) =>
    request<OntologySummary[]>(
      `/api/ontologies${buildQuery({
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
      })}`,
    ),
  getOntology: (id: string) => request<OntologySummary>(`/api/ontologies/${id}`),
  listOntologyVersions: (ontologyId: string) =>
    request<VersionRecord[]>(`/api/ontologies/${ontologyId}/versions`),
  getOntologyVersionDiff: (ontologyId: string, version: number) =>
    request<VersionDiff>(`/api/ontologies/${ontologyId}/versions/${version}/diff`),
  getOntologyVersionSnapshot: (ontologyId: string, version: number) =>
    request<VersionSnapshot>(`/api/ontologies/${ontologyId}/versions/${version}/snapshot`),
  validateOntology: (ontologyId: string) =>
    request<OntologyValidationResult>(`/api/ontologies/${ontologyId}/validate`, {
      method: "POST",
    }),
  formalValidateOntology: (ontologyId: string) =>
    request<FormalValidationResult>(`/api/ontologies/${ontologyId}/formal-validate`),
  getOntologyGraph: (
    id: string,
    params?: {
      centerId?: string;
      depth?: number;
      full?: boolean;
      maxNodes?: number;
      publishedOnly?: boolean;
    },
  ) =>
    request<OntologyGraph>(
      `/api/ontologies/${id}/graph${buildQuery({
        center_id: params?.centerId,
        depth: params?.depth,
        full: params?.full,
        max_nodes: params?.maxNodes,
        published_only: params?.publishedOnly,
      })}`,
    ),
  getOntologyGroupedGraph: (id: string) =>
    request<OntologyGroupedGraph>(`/api/ontologies/${id}/grouped-graph`),
  getOntologyCluster: (id: string, clusterId: string) =>
    request<ClusterDetail>(`/api/ontologies/${id}/clusters/${clusterId}`),

  listObjectTypes: (params?: {
    ontologyId?: string;
    domainId?: string;
    publishedOnly?: boolean;
    q?: string;
    roleIn?: string[];
    needsReview?: boolean;
    limit?: number;
    offset?: number;
  }) =>
    request<PageResult<ObjectTypeSummary>>(
      `/api/object-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
        q: params?.q,
        role_in: params?.roleIn?.length ? params.roleIn.join(",") : undefined,
        needs_review: params?.needsReview,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),
  getObjectType: (id: string, publishedOnly?: boolean) =>
    request<ObjectTypeDetail>(
      `/api/object-types/${id}${buildQuery({ published_only: publishedOnly })}`,
    ),

  listBusinessLogics: (params?: {
    ontologyId?: string;
    domainId?: string;
    categoryId?: string;
    publishedOnly?: boolean;
    q?: string;
    limit?: number;
    offset?: number;
  }) =>
    request<PageResult<BusinessLogic>>(
      `/api/business-logics${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        category_id: params?.categoryId,
        published_only: params?.publishedOnly,
        q: params?.q,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),
  listBusinessLogicCategories: () =>
    request<BusinessLogicCategory[]>("/api/business-logic-categories"),
  createBusinessLogicCategory: (body: { name: string; description?: string }) =>
    request<BusinessLogicCategory>("/api/business-logic-categories", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateBusinessLogicCategory: (id: string, body: { name?: string; description?: string }) =>
    request<BusinessLogicCategory>(`/api/business-logic-categories/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteBusinessLogicCategory: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/business-logic-categories/${id}`, {
      method: "DELETE",
    }),
  getBusinessLogic: (id: string) => request<BusinessLogicDetail>(`/api/business-logics/${id}`),

  createBusinessLogic: (body: BusinessLogicCreateInput) =>
    request<BusinessLogicDetail>("/api/business-logics", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  formatExpression: (body: {
    domain_id: string;
    expression_draft: ExpressionDraft;
    logic_type?: string;
    description?: string;
  }) =>
    request<{ expression_json: ExpressionJson; expression_summary: string }>(
      "/api/business-logics/format-expression",
      { method: "POST", body: JSON.stringify(body) },
    ),

  importBusinessLogic: (body: BusinessLogicImportInput) =>
    request<BusinessLogicDetail>("/api/business-logics/import", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateBusinessLogic: (id: string, body: BusinessLogicUpdateInput) =>
    request<BusinessLogicDetail>(`/api/business-logics/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  prePublishBusinessLogic: (id: string) =>
    request<BusinessLogic>(`/api/business-logics/${id}/pre-publish`, {
      method: "PATCH",
    }),

  publishBusinessLogic: (id: string) =>
    request<Confirmation>(`/api/business-logics/${id}/publish`, { method: "POST" }),

  deleteBusinessLogic: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/business-logics/${id}`, {
      method: "DELETE",
    }),

  bindObjectToLogic: (
    logicId: string,
    body: { object_type_id: string; role?: string; operator?: string },
  ) =>
    request<BusinessLogicObjectBinding>(`/api/business-logics/${logicId}/object-bindings`, {
      method: "POST",
      body: JSON.stringify({ ...body, business_logic_id: logicId }),
    }),
  unbindObjectFromLogic: (bindingId: string) =>
    request<{ id: string; deleted: boolean }>(`/api/business-logics/object-bindings/${bindingId}`, {
      method: "DELETE",
    }),

  bindPropertyToLogic: (
    logicId: string,
    body: { property_id: string; role?: string; operator?: string },
  ) =>
    request<BusinessLogicPropertyBinding>(`/api/business-logics/${logicId}/property-bindings`, {
      method: "POST",
      body: JSON.stringify({ ...body, business_logic_id: logicId }),
    }),
  unbindPropertyFromLogic: (bindingId: string) =>
    request<{ id: string; deleted: boolean }>(
      `/api/business-logics/property-bindings/${bindingId}`,
      { method: "DELETE" },
    ),

  createConfirmation: (body: {
    ontology_id: string;
    target_type: string;
    action_type: string;
    target_id?: string;
    reason?: string;
  }) =>
    request<Confirmation>("/api/confirmations", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  confirmAction: (id: string) =>
    request<Confirmation>(`/api/confirmations/${id}/confirm`, { method: "POST" }),

  listLlmModels: () => request<LlmModelOption[]>("/api/settings/llm-models"),

  listLlmServices: () => request<LlmServiceConfig[]>("/api/settings/llm-services"),

  getLlmService: (id: string) => request<LlmServiceConfig>(`/api/settings/llm-services/${id}`),

  createLlmService: (body: {
    name: string;
    provider?: string;
    api_base_url?: string;
    api_key?: string;
    model: string;
    is_default?: boolean;
    enabled?: boolean;
  }) =>
    request<LlmServiceConfig>("/api/settings/llm-services", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateLlmService: (
    id: string,
    body: {
      name?: string;
      provider?: string;
      api_base_url?: string;
      api_key?: string;
      model?: string;
      is_default?: boolean;
      enabled?: boolean;
    },
  ) =>
    request<LlmServiceConfig>(`/api/settings/llm-services/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteLlmService: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/settings/llm-services/${id}`, {
      method: "DELETE",
    }),

  testLlmConnection: (body: {
    api_base_url: string;
    model: string;
    provider?: string;
    api_key?: string;
    service_id?: string;
  }) =>
    request<LlmConnectionTestResult>("/api/settings/llm-services/test", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getDatahubSettings: () => request<DatahubSettings>("/api/settings/datahub"),

  updateDatahubSettings: (body: {
    gms_url: string;
    frontend_url: string;
    token?: string;
    fabric?: string;
  }) =>
    request<DatahubSettings>("/api/settings/datahub", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getDraftGenerationSettings: () =>
    request<DraftGenerationSettings>("/api/settings/draft-generation"),

  updateDraftGenerationSettings: (body: {
    object_chunk_concurrency: number;
    relation_chunk_concurrency: number;
  }) =>
    request<DraftGenerationSettings>("/api/settings/draft-generation", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getAirflowSettings: () => request<AirflowSettings>("/api/settings/airflow"),
  updateAirflowSettings: (body: {
    endpoint: string;
    username?: string | null;
    password?: string;
    enabled: boolean;
    // 编排配置全部在设置页管理，不再需要配置文件。
    dags_dir?: string;
    // SSH 投递（唯一通道）：产物 rsync 到 Airflow 主机后原子切换。
    ssh_host?: string;
    ssh_port?: number;
    ssh_user?: string;
    ssh_password?: string;
    max_tasks_per_dag?: number;
    max_active_tasks_per_dag?: number;
    dag_parse_timeout?: number;
    preflight_sentinel_timeout?: number;
    staging_swap?: boolean;
  }) =>
    request<AirflowSettings>("/api/settings/airflow", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 调度 API 连通性测试（/health + 带版本前缀的 REST 鉴权）。 */
  testAirflowConnection: () =>
    request<{ ok: boolean; health: Record<string, unknown>; api_version: string }>(
      "/api/settings/airflow/test",
      { method: "POST" },
    ),
  /** DAG 投递（SSH）连通性测试：另一条连接，单独测。 */
  testAirflowSshDelivery: () =>
    request<{ ok: boolean; detail: string }>("/api/settings/airflow/test-ssh", {
      method: "POST",
    }),

  getCubeSettings: () => request<CubeSettings>("/api/settings/cube"),
  updateCubeSettings: (body: {
    api_url: string;
    api_secret?: string;
    preagg_refresh: string;
    tenant_dimension?: string | null;
    timeout_seconds: number;
  }) =>
    request<CubeSettings>("/api/settings/cube", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // ===== 依赖组件统一部署管理 =====
  getDependencySchema: () => request<DependencySchema>("/api/settings/dependencies/schema"),
  listDependencies: () => request<DependencyComponent[]>("/api/settings/dependencies"),
  getDependency: (id: string) => request<DependencyComponent>(`/api/settings/dependencies/${id}`),
  createDependency: (body: {
    key: string;
    name?: string;
    deploy_mode?: string;
    deploy_spec?: Record<string, unknown>;
    connection?: Record<string, unknown>;
    enabled?: boolean;
    is_default?: boolean;
  }) =>
    request<DependencyComponent>("/api/settings/dependencies", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDependency: (
    id: string,
    body: {
      name?: string;
      deploy_mode?: string;
      deploy_spec?: Record<string, unknown>;
      connection?: Record<string, unknown>;
      enabled?: boolean;
      is_default?: boolean;
    },
  ) =>
    request<DependencyComponent>(`/api/settings/dependencies/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteDependency: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/settings/dependencies/${id}`, {
      method: "DELETE",
    }),
  /** 拨测组件连接。`target` 只测其中一条（如 airflow 的 api / ssh），省略则全测。 */
  probeDependency: (id: string, target?: string) =>
    request<DependencyProbeResult>(
      `/api/settings/dependencies/${id}/probe${target ? `?target=${encodeURIComponent(target)}` : ""}`,
      { method: "POST" },
    ),
  deployDependency: (id: string) =>
    request<DependencyDeployResult>(`/api/settings/dependencies/${id}/deploy`, { method: "POST" }),
  teardownDependency: (id: string) =>
    request<{ status: string }>(`/api/settings/dependencies/${id}/teardown`, { method: "POST" }),

  askChatBi: (body: {
    domain_ids: string[];
    question: string;
    history?: ChatBiHistoryItem[];
    conversation_id?: string;
  }) =>
    request<ChatBiAnswer>("/api/chat-bi/ask", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** SSE 流式问答：逐事件回调（meta/step_start/step_done/token/done/error）。 */
  askChatBiStream: async (
    body: {
      domain_ids: string[];
      question: string;
      history?: ChatBiHistoryItem[];
      conversation_id?: string;
    },
    onEvent: (ev: ChatBiStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = getAdminToken();
    if (token) headers.set("X-Admin-Token", token);
    const res = await fetch("/api/chat-bi/ask/stream", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      let detail = `请求失败 (${res.status})`;
      try {
        const j = (await res.json()) as { detail?: string };
        if (j.detail) detail = j.detail;
      } catch {
        // ignore
      }
      throw new ApiError(detail, res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = chunk.trim();
        if (!line.startsWith("data:")) continue;
        const jsonStr = line.slice(5).trim();
        if (!jsonStr) continue;
        try {
          onEvent(JSON.parse(jsonStr) as ChatBiStreamEvent);
        } catch {
          // 忽略半包/坏行
        }
      }
    }
  },

  chatBiSuggestions: (domainIds: string[]) =>
    request<ChatBiSuggestions>(`/api/chat-bi/suggestions${buildQuery({ domain_ids: domainIds })}`),

  listChatBiConversations: (domainIds: string[], q?: string, includeArchived?: boolean) =>
    request<ChatBiConversation[]>(
      `/api/chat-bi/conversations${buildQuery({ domain_ids: domainIds, q, include_archived: includeArchived ? "true" : undefined })}`,
    ),

  createChatBiConversation: (body: {
    domain_ids: string[];
    title?: string;
    category?: string | null;
  }) =>
    request<ChatBiConversation>("/api/chat-bi/conversations", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateChatBiConversation: (
    id: string,
    body: {
      title?: string;
      category?: string | null;
      is_pinned?: boolean;
      is_archived?: boolean;
    },
  ) =>
    request<ChatBiConversation>(`/api/chat-bi/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteChatBiConversation: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/chat-bi/conversations/${id}`, {
      method: "DELETE",
    }),

  getChatBiMessages: (id: string) =>
    request<ChatBiMessageItem[]>(`/api/chat-bi/conversations/${id}/messages`),

  /** P1：记录「本会话催生了某数据任务（治理制品）」，使会话可免 id 追踪任务。 */
  linkChatBiTask: (
    conversationId: string,
    body: {
      artifact_id: string;
      kind?: string;
      intent?: string;
      /**
       * 决策留痕：提案原样 vs 人确认前改成的样子。
       * 两份都在前端手上（proposal.context 与本地编辑态），顺这一次已有的往返带回，
       * 无需额外请求。服务端据此算出「人改了哪些参数」。
       */
      proposed_context?: Record<string, unknown>;
      chosen_context?: Record<string, unknown>;
      message_id?: string;
      block_id?: string;
    },
  ) =>
    request<{ id: string; artifact_id: string; linked: boolean }>(
      `/api/chat-bi/conversations/${conversationId}/tasks`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  /**
   * 决策留痕：记一条人工确认。
   *
   * **责任人不用传**——服务端从已认证主体取，前端给了也会被忽略。
   * 端点恒返回 200（recorded 表是否记成），故调用方只需 fire-and-forget。
   */
  recordChatBiDecision: (
    conversationId: string,
    body: {
      node: string;
      outcome?: string;
      stage?: string;
      trigger?: string;
      message_id?: string;
      block_id?: string;
      summary?: string;
      proposed?: unknown;
      chosen?: unknown;
      ref_kind?: string;
      ref_id?: string;
      dedup_key?: string;
    },
  ) =>
    request<{ id: string | null; recorded: boolean }>(
      `/api/chat-bi/conversations/${conversationId}/decisions`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  listChatBiDecisions: (conversationId: string) =>
    request<ChatBiDecision[]>(
      `/api/chat-bi/conversations/${conversationId}/decisions`,
    ),

  getChatBiClosure: (conversationId: string) =>
    request<ChatBiDecisionClosure>(
      `/api/chat-bi/conversations/${conversationId}/closure`,
    ),

  /** 跨会话决策查询，供决策追踪页。结果附带 conversation_title。 */
  searchChatBiDecisions: (params: {
    node?: string;
    outcome?: string;
    ref_kind?: string;
    subject_id?: string;
    since?: string;
    until?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const suffix = qs.toString();
    return request<ChatBiDecision[]>(
      `/api/chat-bi/decisions${suffix ? `?${suffix}` : ""}`,
    );
  },

  /** P3.1：把用户确认的约定落库为本域记忆（点「记住」后调用）。 */
  rememberPreference: (domainId: string, text: string) =>
    request<{ id: string; text: string; remembered: boolean }>(
      "/api/chat-bi/domain-memory/preferences",
      { method: "POST", body: JSON.stringify({ domain_id: domainId, text }) },
    ),

  listChatBiCategories: (domainIds: string[]) =>
    request<ChatBiCategoryList>(`/api/chat-bi/categories${buildQuery({ domain_ids: domainIds })}`),

  renameChatBiCategory: (body: { domain_ids: string[]; old_name: string; new_name: string }) =>
    request<{ success: boolean }>("/api/chat-bi/categories/rename", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteChatBiCategory: (body: { domain_ids: string[]; name: string }) =>
    request<{ success: boolean }>("/api/chat-bi/categories/delete", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ------------------------------------------------------------ Data Apps

  listDataApps: (domainId?: string, appType?: string) => {
    const qs = new URLSearchParams();
    if (domainId) qs.set("domain_id", domainId);
    if (appType) qs.set("app_type", appType);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<DataAppSummary[]>(`/api/data-apps${suffix}`);
  },
  getDataApp: (id: string) => request<DataAppDetail>(`/api/data-apps/${id}`),
  createDataApp: (body: {
    domain_id: string;
    app_type: string;
    name?: string;
    description?: string;
    source?: string;
    spec?: Record<string, unknown>;
    datasets?: DataAppDatasetInput[];
  }) =>
    request<DataAppDetail>(`/api/data-apps`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDataApp: (
    id: string,
    body: {
      name?: string;
      description?: string;
      spec?: Record<string, unknown>;
      datasets?: DataAppDatasetInput[];
    },
  ) =>
    request<DataAppDetail>(`/api/data-apps/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteDataApp: (id: string) =>
    request<{ status: string }>(`/api/data-apps/${id}`, { method: "DELETE" }),
  previewDataAppDataset: (
    appId: string,
    datasetId: string,
    limit = 50,
    runtimeFilters?: {
      ref: { kind: string; id?: string | null; name?: string | null; display_name?: string | null };
      op: string;
      value?: unknown;
    }[],
  ) =>
    request<DataAppPreviewResult>(`/api/data-apps/${appId}/datasets/${datasetId}/preview`, {
      method: "POST",
      body: JSON.stringify({ limit, runtime_filters: runtimeFilters ?? [] }),
    }),
  publishDataApp: (id: string, versionComment?: string) =>
    request<DataAppDetail>(`/api/data-apps/${id}/publish`, {
      method: "POST",
      body: JSON.stringify({ version_comment: versionComment }),
    }),
  listDataAppVersions: (id: string) => request<DataAppVersion[]>(`/api/data-apps/${id}/versions`),
  generateDataAppFromChat: (body: {
    domain_id: string;
    app_type: string;
    question: string;
    conversation_id?: string;
    name?: string;
    caliber_decomposition?: unknown[];
    referenced_objects?: unknown[];
  }) =>
    request<DataAppDetail>(`/api/chat-bi/generate-app`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Data sources
  listDataSources: () => request<DataSource[]>(`/api/data-sources`),
  createDataSource: (body: {
    name: string;
    kind: string;
    purpose?: "business_source" | "warehouse";
    is_default_warehouse?: boolean;
    enabled?: boolean;
    dsn_secret_ref?: string;
    mapping?: Record<string, unknown>;
    /** 外部 catalog 元数据，不参与数仓查询路由。 */
    catalog_name?: string;
  }) =>
    request<DataSource>(`/api/data-sources`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDataSource: (
    id: string,
    body: {
      name?: string;
      kind?: string;
      purpose?: "business_source" | "warehouse";
      is_default_warehouse?: boolean;
      enabled?: boolean;
      dsn_secret_ref?: string;
      mapping?: Record<string, unknown>;
    },
  ) =>
    request<DataSource>(`/api/data-sources/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteDataSource: (id: string) =>
    request<{ status: string }>(`/api/data-sources/${id}`, { method: "DELETE" }),
  testDataSource: (id: string) =>
    request<DataSource>(`/api/data-sources/${id}/test`, { method: "POST" }),
  // 目标源内省：物化时选落库位置 / 推荐表名用。
  listDataSourceDatabases: (id: string) =>
    request<{ databases: string[] }>(`/api/data-sources/${id}/databases`),
  listDataSourceTables: (id: string, database?: string) => {
    const qs = database ? `?database=${encodeURIComponent(database)}` : "";
    return request<{ tables: string[] }>(`/api/data-sources/${id}/tables${qs}`);
  },

  getDorisWarehouseConfig: () =>
    request<DorisWarehouseConfig | null>(`/api/doris-warehouse`),
  saveDorisWarehouseConfig: (body: DorisWarehouseConfigInput) =>
    request<DorisWarehouseConfig>(`/api/doris-warehouse`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Widgets（可复用图表资产）
  listWidgets: (params?: { domainId?: string; q?: string; widgetType?: string }) => {
    const qs = new URLSearchParams();
    if (params?.domainId) qs.set("domain_id", params.domainId);
    if (params?.q) qs.set("q", params.q);
    if (params?.widgetType) qs.set("widget_type", params.widgetType);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<DataAppWidget[]>(`/api/data-app-widgets${suffix}`);
  },
  getWidget: (id: string) => request<DataAppWidget>(`/api/data-app-widgets/${id}`),
  createWidget: (body: {
    domain_id: string;
    name?: string;
    description?: string;
    widget_type: string;
    primary_object_type_id?: string | null;
    binding: DataAppBinding;
    viz?: Record<string, unknown> | null;
    data_source_id?: string | null;
    source?: string;
  }) =>
    request<DataAppWidget>(`/api/data-app-widgets`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateWidget: (
    id: string,
    body: {
      name?: string;
      description?: string;
      widget_type?: string;
      binding?: DataAppBinding;
      viz?: Record<string, unknown> | null;
      data_source_id?: string | null;
    },
  ) =>
    request<DataAppWidget>(`/api/data-app-widgets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteWidget: (id: string) =>
    request<{ status: string }>(`/api/data-app-widgets/${id}`, { method: "DELETE" }),
  previewWidget: (
    id: string,
    limit = 50,
    runtimeFilters?: {
      ref: { kind: string; id?: string | null; name?: string | null };
      op: string;
      value?: unknown;
    }[],
  ) =>
    request<DataAppPreviewResult>(`/api/data-app-widgets/${id}/preview`, {
      method: "POST",
      body: JSON.stringify({ limit, runtime_filters: runtimeFilters ?? [] }),
    }),
  addWidgetToDashboard: (appId: string, widgetId: string) =>
    request<DataAppDetail>(`/api/data-apps/${appId}/widgets`, {
      method: "POST",
      body: JSON.stringify({ widget_id: widgetId }),
    }),
  generateWidgetFromChat: (body: {
    domain_id: string;
    question: string;
    widget_type?: string;
    name?: string;
    caliber_decomposition?: unknown[];
    referenced_objects?: unknown[];
    dashboard_id?: string;
  }) =>
    request<DataAppWidget>(`/api/chat-bi/generate-widget`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // 公开分享
  getShareStatus: (appId: string) => request<PublicShareStatus>(`/api/data-apps/${appId}/share`),
  enableShare: (appId: string, body: { password?: string; expires_in_days?: number }) =>
    request<PublicShareStatus>(`/api/data-apps/${appId}/share`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  disableShare: (appId: string) =>
    request<PublicShareStatus>(`/api/data-apps/${appId}/share`, { method: "DELETE" }),
  getDataAppLineage: (appId: string) =>
    request<{
      app_id: string;
      name: string;
      nodes: {
        kind: string;
        id: string;
        name: string;
        object_type_ids: string[];
        property_ids: string[];
      }[];
      object_types: { id: string; name: string; display_name: string }[];
      properties: { id: string; name: string; display_name: string; object_type_id: string }[];
    }>(`/api/data-apps/${appId}/lineage`),

  // ---- 物化契约（M1）----
  listIngestionContracts: (ontologyId: string) =>
    request<IngestionContract[]>(`/api/ontologies/${ontologyId}/ingestion-contracts`),
  saveIngestionContract: (ontologyId: string, body: IngestionContractInput) =>
    request<IngestionContract>(`/api/ontologies/${ontologyId}/ingestion-contracts`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getIngestionContractHealth: (contractId: string) =>
    request<{
      contract_id: string;
      flink_job_id: string;
      state: string;
      healthy: boolean;
      status: string;
      start_time?: number | null;
      duration?: number | null;
    }>(`/api/ingestion-contracts/${contractId}/health`),
  reconcileIngestionContract: (
    contractId: string,
    body: { task_state: string; result?: Record<string, unknown> },
  ) =>
    request<IngestionContract>(`/api/ingestion-contracts/${contractId}/reconcile`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listMaterializationContracts: (
    ontologyId: string,
    params?: { target_kind?: MaterializationTargetKind; materialized_only?: boolean },
  ) =>
    request<MaterializationContract[]>(
      `/api/ontologies/${ontologyId}/materialization-contracts${buildQuery({
        target_kind: params?.target_kind,
        materialized_only: params?.materialized_only,
      })}`,
    ),

  /** 物化任务选择树：每域一个工作本体 + 其可物化实体（含自动表名），一次请求拿全。 */
  listMaterializeTargets: () => request<MaterializeTargetsResult>(`/api/materialize/targets`),

  /** 按本体实体重新推导默认值；人工钉住的字段不会被覆盖。 */
  syncMaterializationContracts: (ontologyId: string) =>
    request<MaterializationContractSyncResult>(
      `/api/ontologies/${ontologyId}/materialization-contracts/sync`,
      { method: "POST" },
    ),

  updateMaterializationContract: (contractId: string, body: MaterializationContractUpdateInput) =>
    request<MaterializationContract>(`/api/materialization-contracts/${contractId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /** 本次物化会用什么搬 + 目标引擎实际支持的装载方式（工具已改为自动决策）。 */
  getSyncTools: (params?: { ontologyId?: string; engine?: string }) => {
    const q = new URLSearchParams();
    if (params?.ontologyId) q.set("ontology_id", params.ontologyId);
    if (params?.engine) q.set("engine", params.engine);
    const qs = q.toString();
    return request<SyncToolPlan>(`/api/warehouse/sync-tools${qs ? `?${qs}` : ""}`);
  },
  /** 本体一键物化：生成 DDL/ETL 并对目标数据源真正建表落数。需 publisher 角色。 */
  /** 编排物化的运行状态（Airflow DagRun）。仅 orchestrated 回执可查。 */
  getMaterializeStatus: (artifactId: string) =>
    request<MaterializeStatus>(`/api/warehouse/materialize/${artifactId}/status`),
  /** 一个搬运任务的执行结果：实际用了哪一档、搬了多少行。
   *
   * **按需调，别进轮询**：Airflow 没有跨任务批量读 XCom 的端点，一次一请求，
   * 整轮几百个任务全读一遍会把 Airflow 打垮。 */
  getMaterializeTaskResult: (artifactId: string, taskId: string) =>
    request<MaterializeTaskResult>(
      `/api/warehouse/materialize/${artifactId}/tasks/${encodeURIComponent(taskId)}/result`,
    ),
  /** M11：本次物化将上报的 源表→目标表 血缘（纯读）。 */
  getMaterializeLineagePlan: (artifactId: string) =>
    request<LineageEmitResult>(`/api/warehouse/materialize/${artifactId}/lineage-plan`),
  /** M11：兜底上报表级血缘到 DataHub（插件缺位时用，重复幂等）。 */
  emitMaterializeLineage: (artifactId: string) =>
    request<LineageEmitResult>(`/api/warehouse/materialize/${artifactId}/lineage`, {
      method: "POST",
    }),
  materializeOntology: (ontologyId: string, body: MaterializeRequestInput) =>
    request<MaterializationRun>(`/api/ontologies/${ontologyId}/warehouse/materialize`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** M13：物化提交前自检。只读，不落产物、不触发运行，可随便重跑。 */
  materializePreflight: (
    ontologyId: string,
    body: {
      target_datasource_id: string;
      engine: string;
      selected_targets?: string[] | null;
    },
  ) =>
    request<MaterializePreflightResult>(
      `/api/ontologies/${ontologyId}/warehouse/materialize/preflight`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  /** 本体的历次物化执行记录（含回执），最新在前。 */
  listMaterializationRuns: (ontologyId: string) =>
    request<MaterializationRun[]>(`/api/ontologies/${ontologyId}/warehouse/materialization-runs`),

  // ---- RBAC 主体与角色（M0）----
  listPrincipals: () => request<Principal[]>("/api/principals"),
  getRolePolicy: () => request<RolePolicy>("/api/principals-policy"),
  createPrincipal: (body: { name: string; role: PrincipalRole }) =>
    request<PrincipalCreated>("/api/principals", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updatePrincipal: (id: string, body: { name?: string; role?: PrincipalRole; active?: boolean }) =>
    request<Principal>(`/api/principals/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  rotatePrincipalToken: (id: string) =>
    request<PrincipalCreated>(`/api/principals/${id}/rotate-token`, { method: "POST" }),
  deletePrincipal: (id: string) =>
    request<{ deleted: string }>(`/api/principals/${id}`, { method: "DELETE" }),

  // ---- 治理智能体流水线（M5/M6，写侧；整个命名空间需 publisher 角色）----
  listAgentKinds: () => request<AgentKinds>("/api/agents/kinds"),
  listArtifacts: (params?: { kind?: string; status?: string; ontology_id?: string }) =>
    request<GovernanceArtifact[]>(`/api/agents/artifacts${buildQuery(params ?? {})}`),
  getArtifact: (id: string) => request<GovernanceArtifact>(`/api/agents/artifacts/${id}`),

  // 结构化 Spec 表单的字段下拉数据源
  listWarehouseEngines: () =>
    request<{ default: string; engines: { name: string; implemented: boolean }[] }>(
      "/api/warehouse/engines",
    ),
  listOntologyProperties: (ontologyId: string) =>
    request<{ name: string; display_name: string; object_type_name: string }[]>(
      `/api/ontologies/${ontologyId}/properties`,
    ),

  /**
   * 按任务类型现取一张六环确认表单（字段骨架 + 真实候选 + 本次 confirmation_id）。
   * 任务链逐步确认用的就是这张，与对话里 request_form 出的是同一份。
   */
  taskConfirmationForm: (body: {
    kind: string;
    ontology_id: string;
    title?: string;
    intent?: string;
    prefill?: Record<string, unknown>;
  }) =>
    request<ChatBiFormRequest>("/api/agents/task-form", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  draftConfirmedArtifact: (body: {
    conversation_id: string;
    confirmation_id: string;
    kind: string;
    intent: string;
    context: Record<string, unknown>;
    ontology_id: string;
    message_id?: string;
    block_id?: string;
  }) =>
    request<GovernanceArtifact>("/api/agents/draft-confirmed", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  draftArtifact: (body: {
    kind: string;
    intent?: string | null;
    context?: Record<string, unknown>;
    ontology_id?: string | null;
    // 手动结构化起草：给了 spec 就跳过 drafter，直接落库并进校验闸门。
    spec?: Record<string, unknown> | null;
    name?: string | null;
    // 表单起草走 context+drafter 派生路径，但仍是用户发起：置 true 让溯源标 user。
    user_created?: boolean;
  }) =>
    request<GovernanceArtifact>("/api/agents/draft", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // 编辑草稿/已校验/失败态的制品。已确认/执行过的制品不可编辑（后端 409），请新建。
  // 给 spec 走直填覆盖，给 intent/context 走 drafter 重派生——语义与 draftArtifact 一致。
  updateArtifact: (
    id: string,
    body: {
      name?: string | null;
      intent?: string | null;
      context?: Record<string, unknown>;
      spec?: Record<string, unknown> | null;
      ontology_id?: string | null;
    },
  ) =>
    request<GovernanceArtifact>(`/api/agents/artifacts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  validateArtifact: (id: string, context?: Record<string, unknown>) =>
    request<GovernanceArtifact>(`/api/agents/artifacts/${id}/validate`, {
      method: "POST",
      body: JSON.stringify({ context: context ?? {} }),
    }),
  confirmArtifact: (id: string, operator?: string) =>
    request<GovernanceArtifact>(`/api/agents/artifacts/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ operator }),
    }),
  executeArtifact: (id: string, context?: Record<string, unknown>) =>
    request<GovernanceArtifact>(`/api/agents/artifacts/${id}/execute`, {
      method: "POST",
      body: JSON.stringify({ context: context ?? {} }),
    }),

  // ---- 任务链（多任务编排）----
  // 链只管顺序与上下文传递，逐步的校验/确认/执行仍走上面那几个制品端点。
  // 故这里**没有** executePipeline：一键跑完必然绕过逐制品的人工确认。
  createPipeline: (body: {
    name: string;
    intent?: string | null;
    ontology_id?: string | null;
    steps: { kind: string; intent: string; context?: Record<string, unknown> }[];
  }) =>
    request<TaskPipeline>("/api/agents/pipelines", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPipeline: (id: string) => request<TaskPipeline>(`/api/agents/pipelines/${id}`),
  listPipelines: (ontologyId?: string) =>
    request<TaskPipeline[]>(`/api/agents/pipelines${buildQuery({ ontology_id: ontologyId })}`),
  /** 起草链上的下一步。上游还没跑成功时后端 409，错误里说清卡在哪一步。 */
  advancePipeline: (id: string) =>
    request<TaskPipelineAdvanceResult>(`/api/agents/pipelines/${id}/advance`, {
      method: "POST",
    }),
  /**
   * 确认过前三环（需求/本体/数据）后起草链上的下一步，并直接产出执行方案预览。
   * 缺任何一环后端 409 并说清缺哪环——链不替谁确认。
   */
  advancePipelineConfirmed: (
    id: string,
    body: {
      conversation_id: string;
      confirmation_id: string;
      context: Record<string, unknown>;
      intent?: string;
    },
  ) =>
    request<TaskPipelineAdvanceResult>(`/api/agents/pipelines/${id}/advance-confirmed`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** C2：一键起草全部步骤（血缘驱动，起草阶段不阻塞）。只起草不执行。 */
  draftAllPipeline: (id: string) =>
    request<TaskPipelineDraftAllResult>(`/api/agents/pipelines/${id}/draft-all`, {
      method: "POST",
    }),
  /** 给链设置周期 cron（不触发编译；编译是显式的第二步）。 */
  setPipelineSchedule: (id: string, scheduleCron: string | null) =>
    request<TaskPipeline>(`/api/agents/pipelines/${id}/schedule`, {
      method: "PUT",
      body: JSON.stringify({ schedule_cron: scheduleCron }),
    }),
  /** 把链编译成周期 DAG。前提不满足时后端 409，错误里说清卡在哪一步。 */
  compilePipeline: (id: string) =>
    request<PipelineCompileResult>(`/api/agents/pipelines/${id}/compile`, {
      method: "POST",
    }),
  /** 下线周期调度：清 schedule_cron 与 compiled_dag_id。 */
  unschedulePipeline: (id: string) =>
    request<TaskPipeline>(`/api/agents/pipelines/${id}/schedule`, {
      method: "DELETE",
    }),
};
