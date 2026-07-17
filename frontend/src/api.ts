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
  ChatBiAnswer,
  ChatBiCategoryList,
  ChatBiConversation,
  ChatBiHistoryItem,
  ChatBiMessageItem,
  ChatBiSuggestions,
  Confirmation,
  DataHubDatasetOption,
  DatahubSettings,
  DomainContext,
  DomainContextDetail,
  DraftProgress,
  ExpressionDraft,
  ExpressionJson,
  ExternalApiCallLog,
  ExternalApiCatalogItem,
  McpToolCallResult,
  ExternalApp,
  ExternalAppCreated,
  LlmModelOption,
  LlmServiceConfig,
  ObjectTypeDetail,
  ObjectTypeSummary,
  OntologyGraph,
  OntologySummary,
  PageResult,
  Property,
  RelationType,
  RelationTypeDetail,
  TaskRecord,
  OntologyValidationResult,
  VersionDiff,
  VersionRecord,
  VersionSnapshot,
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
      } else if (parsed.detail && typeof parsed.detail === "object" && !Array.isArray(parsed.detail)) {
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
    throw new Error(detail);
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
  generateDraft: (domainId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/generate-draft`, { method: "POST" }),
  getProgress: (domainId: string) => request<DraftProgress>(`/api/domains/${domainId}/progress`),
  listTasks: (domainId: string) => request<TaskRecord[]>(`/api/domains/${domainId}/tasks`),
  stopDraftTask: (domainId: string, taskId: string) =>
    request<TaskRecord>(`/api/domains/${domainId}/tasks/${taskId}/stop`, { method: "POST" }),
  retryDraftTask: (domainId: string, taskId: string) =>
    request<DraftProgress>(`/api/domains/${domainId}/tasks/${taskId}/retry`, { method: "POST" }),
  getDraftDuplicates: (domainId: string) =>
    request<{
      domain_id: string;
      draft_count: number;
      draft_ontology_ids: string[];
      will_purge_on_regenerate: boolean;
      message: string;
    }>(`/api/domains/${domainId}/draft-duplicates`),
  getTaskLogs: (domainId: string, taskId: string) =>
    request<ChangeLog[]>(`/api/domains/${domainId}/tasks/${taskId}/logs`),

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
    body: { name?: string; display_name?: string; description?: string },
  ) =>
    request<ObjectTypeDetail>(`/api/object-types/${objectTypeId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  prePublishObjectType: (objectTypeId: string) =>
    request<ObjectTypeSummary>(`/api/object-types/${objectTypeId}/pre-publish`, {
      method: "PATCH",
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

  createRelationType: (
    body: {
      ontology_id: string;
      display_name: string;
      source_object_type_id: string;
      target_object_type_id: string;
      name?: string;
      description?: string;
      cardinality?: string;
      structure_type?: string;
      mapping_object_type_id?: string | null;
    },
  ) =>
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
    limit?: number;
    offset?: number;
  }) =>
    request<PageResult<RelationType>>(
      `/api/relation-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
        q: params?.q,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),

  getRelationType: (id: string) => request<RelationTypeDetail>(`/api/relation-types/${id}`),

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
  getOntologyGraph: (
    id: string,
    params?: { centerId?: string; depth?: number; full?: boolean; maxNodes?: number },
  ) =>
    request<OntologyGraph>(
      `/api/ontologies/${id}/graph${buildQuery({
        center_id: params?.centerId,
        depth: params?.depth,
        full: params?.full,
        max_nodes: params?.maxNodes,
      })}`,
    ),

  listObjectTypes: (params?: {
    ontologyId?: string;
    domainId?: string;
    publishedOnly?: boolean;
    q?: string;
    limit?: number;
    offset?: number;
  }) =>
    request<PageResult<ObjectTypeSummary>>(
      `/api/object-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
        q: params?.q,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),
  getObjectType: (id: string) => request<ObjectTypeDetail>(`/api/object-types/${id}`),

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
    request<BusinessLogicObjectBinding>(
      `/api/business-logics/${logicId}/object-bindings`,
      { method: "POST", body: JSON.stringify({ ...body, business_logic_id: logicId }) },
    ),
  unbindObjectFromLogic: (bindingId: string) =>
    request<{ id: string; deleted: boolean }>(
      `/api/business-logics/object-bindings/${bindingId}`,
      { method: "DELETE" },
    ),

  bindPropertyToLogic: (
    logicId: string,
    body: { property_id: string; role?: string; operator?: string },
  ) =>
    request<BusinessLogicPropertyBinding>(
      `/api/business-logics/${logicId}/property-bindings`,
      { method: "POST", body: JSON.stringify({ ...body, business_logic_id: logicId }) },
    ),
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

  getLlmService: (id: string) =>
    request<LlmServiceConfig>(`/api/settings/llm-services/${id}`),

  createLlmService: (body: {
    name: string;
    provider?: string;
    api_base_url?: string;
    api_key?: string;
    model: string;
    is_default?: boolean;
    enabled?: boolean;
    use_mock?: boolean;
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
      use_mock?: boolean;
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

  getDatahubSettings: () => request<DatahubSettings>("/api/settings/datahub"),

  updateDatahubSettings: (body: {
    gms_url: string;
    frontend_url: string;
    token?: string;
    use_mock?: boolean;
  }) =>
    request<DatahubSettings>("/api/settings/datahub", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  askChatBi: (body: {
    domain_id: string;
    question: string;
    history?: ChatBiHistoryItem[];
    conversation_id?: string;
  }) =>
    request<ChatBiAnswer>("/api/chat-bi/ask", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  chatBiSuggestions: (domainId: string) =>
    request<ChatBiSuggestions>(
      `/api/chat-bi/suggestions${buildQuery({ domain_id: domainId })}`,
    ),

  listChatBiConversations: (
    domainId: string,
    q?: string,
    includeArchived?: boolean,
  ) =>
    request<ChatBiConversation[]>(
      `/api/chat-bi/conversations${buildQuery({ domain_id: domainId, q, include_archived: includeArchived ? "true" : undefined })}`,
    ),

  createChatBiConversation: (body: {
    domain_id: string;
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
    request<{ id: string; deleted: boolean }>(
      `/api/chat-bi/conversations/${id}`,
      { method: "DELETE" },
    ),

  getChatBiMessages: (id: string) =>
    request<ChatBiMessageItem[]>(`/api/chat-bi/conversations/${id}/messages`),

  listChatBiCategories: (domainId: string) =>
    request<ChatBiCategoryList>(
      `/api/chat-bi/categories${buildQuery({ domain_id: domainId })}`,
    ),

  renameChatBiCategory: (body: {
    domain_id: string;
    old_name: string;
    new_name: string;
  }) =>
    request<{ success: boolean }>("/api/chat-bi/categories/rename", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteChatBiCategory: (body: { domain_id: string; name: string }) =>
    request<{ success: boolean }>("/api/chat-bi/categories/delete", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- External API ---

  listExternalApps: () => request<ExternalApp[]>("/api/external-apps"),

  listExternalScopes: () =>
    request<{ scopes: string[] }>("/api/external-apps/scopes"),

  createExternalApp: (body: {
    name: string;
    description?: string;
    scopes?: string[];
    rate_limit_per_minute?: number | null;
  }) =>
    request<ExternalAppCreated>("/api/external-apps", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getExternalApp: (id: string) =>
    request<ExternalApp>(`/api/external-apps/${id}`),

  updateExternalApp: (
    id: string,
    body: {
      name?: string;
      description?: string;
      status?: string;
      scopes?: string[];
      rate_limit_per_minute?: number | null;
    },
  ) =>
    request<ExternalApp>(`/api/external-apps/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  regenerateExternalAppKey: (id: string) =>
    request<ExternalAppCreated>(`/api/external-apps/${id}/regenerate-key`, {
      method: "POST",
    }),

  deleteExternalApp: (id: string) =>
    request<{ id: string; deleted: boolean }>(`/api/external-apps/${id}`, {
      method: "DELETE",
    }),

  listExternalAppCallLogs: (appId: string, limit = 50) =>
    request<ExternalApiCallLog[]>(
      `/api/external-apps/${appId}/call-logs?limit=${limit}`,
    ),

  listExternalApiCatalog: () =>
    request<ExternalApiCatalogItem[]>("/api/external-api/catalog"),

  getExternalApiCatalogItem: (apiId: string) =>
    request<ExternalApiCatalogItem>(`/api/external-api/catalog/${apiId}`),

  /** 调用 MCP tools/call（试用），需传入 API Key（真实鉴权） */
  callMcpTool: async (
    toolName: string,
    arguments_: Record<string, unknown>,
    apiKey: string,
  ): Promise<{ status: number; data: McpToolCallResult | unknown }> => {
    const response = await fetch("/api/mcp/tools/call", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": apiKey,
      },
      body: JSON.stringify({ name: toolName, arguments: arguments_ }),
    });
    let data: unknown;
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    return { status: response.status, data };
  },
};
