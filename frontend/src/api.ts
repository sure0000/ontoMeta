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
  LlmModelOption,
  LlmServiceConfig,
  ObjectTypeDetail,
  ObjectTypeSummary,
  OntologyGraph,
  OntologySummary,
  Property,
  RelationType,
  RelationTypeDetail,
  TaskRecord,
} from "./types";
import { buildQuery } from "./utils/format";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
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
        detail?: string | Array<{ msg?: string }>;
      };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        detail = parsed.detail;
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
  }) =>
    request<RelationType[]>(
      `/api/relation-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
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
  getOntologyGraph: (id: string) => request<OntologyGraph>(`/api/ontologies/${id}/graph`),

  listObjectTypes: (params?: {
    ontologyId?: string;
    domainId?: string;
    publishedOnly?: boolean;
  }) =>
    request<ObjectTypeSummary[]>(
      `/api/object-types${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        published_only: params?.publishedOnly,
      })}`,
    ),
  getObjectType: (id: string) => request<ObjectTypeDetail>(`/api/object-types/${id}`),

  listBusinessLogics: (params?: {
    ontologyId?: string;
    domainId?: string;
    categoryId?: string;
    publishedOnly?: boolean;
  }) =>
    request<BusinessLogic[]>(
      `/api/business-logics${buildQuery({
        ontology_id: params?.ontologyId,
        domain_id: params?.domainId,
        category_id: params?.categoryId,
        published_only: params?.publishedOnly,
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
};
