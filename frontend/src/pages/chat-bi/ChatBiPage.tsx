import { MenuFoldOutlined, MenuUnfoldOutlined } from "@ant-design/icons";
import { Alert, Button, Input, message, Modal, Select, Spin, Tag, Tooltip } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../api";
import { PageContainer } from "../../components/PageContainer";
import { useApi } from "../../hooks/useApi";
import type {
  ChatBiAgentStep,
  ChatBiAnswer,
  ChatBiBlock,
  ChatBiCategoryItem,
  ChatBiConversation,
  ChatBiHistoryItem,
  ChatBiMessageItem,
  DataAppSummary,
  DomainContext,
} from "../../types";
import { ChatBiComposer } from "./ChatBiComposer";
import { ChatBiMessages } from "./ChatBiMessages";
import { ChatBiSidebar } from "./ChatBiSidebar";
import { DecisionLedgerProvider } from "./DecisionLedger";
import { recordDecisionQuietly } from "./ledger";
import { EMPTY_DEPS, getTimeGroup, type ChatMessage, type TimeGroup } from "./utils";

export function ChatBiPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainsParam = searchParams.get("domains") || "";

  const { data: domains, loading: loadingDomains } = useApi<DomainContext[]>(
    async () => api.listDomains(),
    EMPTY_DEPS,
  );

  const domainList = domains ?? [];
  // 不选域 = 全域通盘（domainIds 为空数组，合法状态）；仅系统一个域都没有时才算未接入。
  // 记忆依赖必须是 domainsParam 字符串（而非每次渲染都新建的数组），否则 domainIds 每帧都是新引用，
  // 会让下游依赖 [domainIds] 的 effect 无限重跑——表现为右侧消息区/推荐永远 loading。
  const domainIds = useMemo(() => {
    const requested = domainsParam
      ? domainsParam
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    // 过滤掉已不存在的域 id
    return requested.filter((id) => domainList.some((d) => d.id === id));
  }, [domainsParam, domainList]);

  useEffect(() => {
    // 同步 URL：去空白/空段归一化 domains 参数。空选 = 去掉参数（全域）。
    if (!domainsParam) return;
    const next = domainsParam
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .join(",");
    if (next !== domainsParam) {
      if (next) setSearchParams({ domains: next }, { replace: true });
      else setSearchParams({}, { replace: true });
    }
  }, [domainsParam, setSearchParams]);

  const activeDomains = domainIds
    .map((id) => domainList.find((d) => d.id === id))
    .filter((d): d is DomainContext => Boolean(d));
  const scopeLabel = activeDomains.length ? activeDomains.map((d) => d.name).join("、") : "全域";

  const [conversations, setConversations] = useState<ChatBiConversation[]>([]);
  const [archivedConversations, setArchivedConversations] = useState<ChatBiConversation[]>([]);
  const [categories, setCategories] = useState<ChatBiCategoryItem[]>([]);
  const [loadingConversations, setLoadingConversations] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [sidebarVisible, setSidebarVisible] = useState(
    () => typeof window === "undefined" || window.innerWidth > 768,
  );
  const [showArchived, setShowArchived] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());

  const patchConversation = useCallback((id: string, patch: Partial<ChatBiConversation>) => {
    const apply = (list: ChatBiConversation[]) =>
      list.map((c) => (c.id === id ? { ...c, ...patch } : c));
    setConversations(apply);
    setArchivedConversations(apply);
  }, []);

  const removeConversationFromLists = useCallback((id: string) => {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    setArchivedConversations((prev) => prev.filter((c) => c.id !== id));
  }, []);

  const prependConversation = useCallback((conv: ChatBiConversation) => {
    if (conv.is_archived) {
      setArchivedConversations((prev) =>
        prev.some((c) => c.id === conv.id) ? prev : [conv, ...prev],
      );
    } else {
      setConversations((prev) => (prev.some((c) => c.id === conv.id) ? prev : [conv, ...prev]));
    }
  }, []);

  const [renameModalOpen, setRenameModalOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<ChatBiConversation | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const [batchMode, setBatchMode] = useState(false);
  const [batchSelectedIds, setBatchSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  const [catDialogOpen, setCatDialogOpen] = useState(false);
  const [catDialogMode, setCatDialogMode] = useState<"create" | "rename" | "delete">("create");
  const [catDialogName, setCatDialogName] = useState("");
  const [catDialogNewName, setCatDialogNewName] = useState("");
  const [catDialogMoveConv, setCatDialogMoveConv] = useState<ChatBiConversation | null>(null);

  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hasConversationDataRef = useRef(false);

  useEffect(() => {
    setActiveConversationId(null);
    setBatchMode(false);
    setBatchSelectedIds(new Set());
    hasConversationDataRef.current = false;
  }, [domainIds.join(",")]);

  useEffect(() => {
    // 全域通盘（空选）也是合法作用域，照常拉取全部会话。
    const showLoading = !hasConversationDataRef.current;
    if (showLoading) setLoadingConversations(true);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(async () => {
      try {
        const [active, archived, cats] = await Promise.all([
          api.listChatBiConversations(domainIds, searchQuery || undefined, false),
          api.listChatBiConversations(domainIds, searchQuery || undefined, true),
          api.listChatBiCategories(domainIds),
        ]);
        const archivedOnly = archived.filter((c) => c.is_archived);
        setConversations(active.filter((c) => !c.is_archived));
        setArchivedConversations(archivedOnly);
        setCategories(cats.categories);
        hasConversationDataRef.current = true;
      } catch {
        // ignore
      } finally {
        setLoadingConversations(false);
      }
    }, 200);
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [domainIds, searchQuery]);

  const handleSelectConversation = useCallback((id: string) => {
    setActiveConversationId(id);
    if (window.innerWidth <= 768) setSidebarVisible(false);
    setShowArchived(false);
  }, []);

  const handleNewConversation = useCallback(async () => {
    setShowArchived(false);
    try {
      const conv = await api.createChatBiConversation({ domain_ids: domainIds });
      prependConversation(conv);
      setActiveConversationId(conv.id);
      if (window.innerWidth <= 768) setSidebarVisible(false);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建对话失败");
    }
  }, [domainIds, prependConversation]);

  const handleOpenRename = useCallback((conv: ChatBiConversation) => {
    setRenameTarget(conv);
    setRenameValue(conv.title);
    setRenameModalOpen(true);
  }, []);

  const handleConfirmRename = useCallback(async () => {
    if (!renameTarget || !renameValue.trim()) return;
    try {
      const updated = await api.updateChatBiConversation(renameTarget.id, {
        title: renameValue.trim(),
      });
      patchConversation(renameTarget.id, {
        title: updated.title,
        updated_at: updated.updated_at,
      });
      setRenameModalOpen(false);
      setRenameTarget(null);
      setRenameValue("");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "重命名失败");
    }
  }, [renameTarget, renameValue, patchConversation]);

  const handleTogglePin = useCallback(
    async (conv: ChatBiConversation) => {
      try {
        const updated = await api.updateChatBiConversation(conv.id, {
          is_pinned: !conv.is_pinned,
        });
        patchConversation(conv.id, {
          is_pinned: updated.is_pinned,
          updated_at: updated.updated_at,
        });
      } catch (err) {
        message.error(err instanceof Error ? err.message : "操作失败");
      }
    },
    [patchConversation],
  );

  const handleToggleArchive = useCallback(
    async (conv: ChatBiConversation) => {
      try {
        const updated = await api.updateChatBiConversation(conv.id, {
          is_archived: !conv.is_archived,
        });
        removeConversationFromLists(conv.id);
        if (updated.is_archived) {
          setArchivedConversations((prev) => [updated, ...prev]);
        } else {
          setConversations((prev) => [updated, ...prev]);
        }
        if (activeConversationId === conv.id) {
          setActiveConversationId(null);
        }
      } catch (err) {
        message.error(err instanceof Error ? err.message : "操作失败");
      }
    },
    [activeConversationId, removeConversationFromLists],
  );

  const handleMoveConversation = useCallback(
    async (conv: ChatBiConversation, category: string | null) => {
      try {
        const updated = await api.updateChatBiConversation(conv.id, { category });
        patchConversation(conv.id, {
          category: updated.category,
          updated_at: updated.updated_at,
        });
      } catch (err) {
        message.error(err instanceof Error ? err.message : "操作失败");
      }
    },
    [patchConversation],
  );

  const handleDeleteConversation = useCallback(
    async (conv: ChatBiConversation) => {
      Modal.confirm({
        title: "删除对话",
        content: `确定删除「${conv.title}」吗？对话内的所有消息将被一并删除，不可恢复。`,
        okText: "删除",
        okType: "danger",
        cancelText: "取消",
        onOk: async () => {
          try {
            await api.deleteChatBiConversation(conv.id);
            removeConversationFromLists(conv.id);
            if (activeConversationId === conv.id) {
              setActiveConversationId(null);
            }
          } catch (err) {
            message.error(err instanceof Error ? err.message : "删除失败");
          }
        },
      });
    },
    [activeConversationId, removeConversationFromLists],
  );

  const displayConversations = showArchived ? archivedConversations : conversations;

  const handleEnterBatchMode = useCallback(() => {
    setBatchMode(true);
    setBatchSelectedIds(new Set());
  }, []);

  const handleExitBatchMode = useCallback(() => {
    setBatchMode(false);
    setBatchSelectedIds(new Set());
  }, []);

  const handleToggleBatchSelect = useCallback((id: string) => {
    setBatchSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleBatchSelectAll = useCallback(
    (selectAll: boolean) => {
      if (selectAll) {
        setBatchSelectedIds(new Set(displayConversations.map((c) => c.id)));
      } else {
        setBatchSelectedIds(new Set());
      }
    },
    [displayConversations],
  );

  const handleBatchDelete = useCallback(async () => {
    const ids = Array.from(batchSelectedIds);
    if (ids.length === 0) return;
    Modal.confirm({
      title: "批量删除对话",
      content: `确定删除选中的 ${ids.length} 个对话吗？对话内的所有消息将被一并删除，不可恢复。`,
      okText: "删除",
      okType: "danger",
      cancelText: "取消",
      onOk: async () => {
        setBatchDeleting(true);
        let failed = 0;
        await Promise.all(
          ids.map((id) =>
            api
              .deleteChatBiConversation(id)
              .then(() => {
                removeConversationFromLists(id);
              })
              .catch(() => {
                failed++;
              }),
          ),
        );
        setBatchDeleting(false);
        if (activeConversationId && ids.includes(activeConversationId)) {
          setActiveConversationId(null);
        }
        setBatchMode(false);
        setBatchSelectedIds(new Set());
        if (failed > 0) {
          message.error(`${ids.length - failed} 个已删除，${failed} 个删除失败`);
        } else {
          message.success(`已删除 ${ids.length} 个对话`);
        }
      },
    });
  }, [batchSelectedIds, activeConversationId, removeConversationFromLists]);

  const openCategoryDialog = useCallback(
    (mode: "create" | "rename" | "delete", name?: string, moveConv?: ChatBiConversation | null) => {
      setCatDialogMode(mode);
      setCatDialogName(name || "");
      setCatDialogNewName("");
      setCatDialogMoveConv(moveConv || null);
      setCatDialogOpen(true);
    },
    [],
  );

  const handleCategoryDialogConfirm = useCallback(async () => {
    try {
      if (catDialogMode === "create") {
        const name = catDialogName.trim();
        if (!name) return;
        if (catDialogMoveConv) {
          const updated = await api.updateChatBiConversation(catDialogMoveConv.id, {
            category: name,
          });
          patchConversation(catDialogMoveConv.id, {
            category: updated.category,
            updated_at: updated.updated_at,
          });
        } else {
          const conv = await api.createChatBiConversation({
            domain_ids: domainIds,
            category: name,
          });
          prependConversation(conv);
          setActiveConversationId(conv.id);
        }
        setCategories((prev) =>
          prev.some((c) => c.name === name) ? prev : [...prev, { name, conversation_count: 1 }],
        );
        setExpandedCategories((prev) => new Set(prev).add(name));
        setCatDialogOpen(false);
      } else if (catDialogMode === "rename") {
        if (!catDialogNewName.trim() || !catDialogName) return;
        const newName = catDialogNewName.trim();
        await api.renameChatBiCategory({
          domain_ids: domainIds,
          old_name: catDialogName,
          new_name: newName,
        });
        const renameCategory = (list: ChatBiConversation[]) =>
          list.map((c) => (c.category === catDialogName ? { ...c, category: newName } : c));
        setConversations(renameCategory);
        setArchivedConversations(renameCategory);
        setCategories((prev) =>
          prev.map((c) => (c.name === catDialogName ? { ...c, name: newName } : c)),
        );
        setExpandedCategories((prev) => {
          const next = new Set(prev);
          if (next.has(catDialogName)) {
            next.delete(catDialogName);
            next.add(newName);
          }
          return next;
        });
        setCatDialogOpen(false);
      } else if (catDialogMode === "delete") {
        if (!catDialogName) return;
        await api.deleteChatBiCategory({
          domain_ids: domainIds,
          name: catDialogName,
        });
        const clearCategory = (list: ChatBiConversation[]) =>
          list.map((c) => (c.category === catDialogName ? { ...c, category: null } : c));
        setConversations(clearCategory);
        setArchivedConversations(clearCategory);
        setCategories((prev) => prev.filter((c) => c.name !== catDialogName));
        setExpandedCategories((prev) => {
          const next = new Set(prev);
          next.delete(catDialogName);
          return next;
        });
        setCatDialogOpen(false);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    }
  }, [
    domainIds,
    catDialogMode,
    catDialogName,
    catDialogNewName,
    catDialogMoveConv,
    patchConversation,
    prependConversation,
  ]);

  const handleNewCategory = useCallback(
    (conv: ChatBiConversation) => openCategoryDialog("create", "", conv),
    [openCategoryDialog],
  );

  const handleCreateConvInCategory = useCallback(
    async (catName: string) => {
      try {
        const conv = await api.createChatBiConversation({
          domain_ids: domainIds,
          category: catName,
        });
        prependConversation(conv);
        setActiveConversationId(conv.id);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "创建对话失败");
      }
    },
    [domainIds, prependConversation],
  );

  const handleConversationActivity = useCallback(
    (update: {
      id: string;
      title?: string | null;
      last_message_preview?: string;
      isNew?: boolean;
    }) => {
      const now = new Date().toISOString();
      if (update.isNew) {
        prependConversation({
          id: update.id,
          domain_ids: domainIds,
          domain_id: domainIds[0] ?? null,
          title: update.title || "新对话",
          is_pinned: false,
          is_archived: false,
          message_count: 1,
          last_message_preview: update.last_message_preview ?? null,
          created_at: now,
          updated_at: now,
        });
        setActiveConversationId(update.id);
        return;
      }
      patchConversation(update.id, {
        ...(update.title ? { title: update.title } : {}),
        ...(update.last_message_preview
          ? { last_message_preview: update.last_message_preview }
          : {}),
        updated_at: now,
      });
    },
    [domainIds, patchConversation, prependConversation],
  );

  const handleToggleCategory = useCallback((name: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const { categorizedConvs, uncategorizedConvs } = useMemo(() => {
    const withCat: Record<string, ChatBiConversation[]> = {};
    const withoutCat: ChatBiConversation[] = [];
    for (const conv of displayConversations) {
      if (conv.category) {
        if (!withCat[conv.category]) withCat[conv.category] = [];
        withCat[conv.category].push(conv);
      } else {
        withoutCat.push(conv);
      }
    }
    return { categorizedConvs: withCat, uncategorizedConvs: withoutCat };
  }, [displayConversations]);

  const timeGrouped = useMemo(() => {
    const groups: Record<TimeGroup, ChatBiConversation[]> = {
      pinned: [],
      today: [],
      yesterday: [],
      thisWeek: [],
      thisMonth: [],
      older: [],
    };
    for (const conv of uncategorizedConvs) {
      const group = showArchived ? "older" : getTimeGroup(conv);
      groups[group].push(conv);
    }
    return groups;
  }, [uncategorizedConvs, showArchived]);

  const sortedCatNames = useMemo(() => {
    return Object.keys(categorizedConvs).sort((a, b) =>
      a.toLowerCase() < b.toLowerCase() ? -1 : 1,
    );
  }, [categorizedConvs]);

  const existingCategoryNames = useMemo(() => {
    const names = new Set<string>();
    for (const c of categories) {
      if (c.name !== "__uncategorized__") names.add(c.name);
    }
    for (const name of sortedCatNames) names.add(name);
    return Array.from(names).sort();
  }, [categories, sortedCatNames]);

  const totalArchived = archivedConversations.length;

  return (
    <PageContainer full>
      {domainList.length === 0 ? (
        loadingDomains || domains === null ? (
          <div style={{ display: "grid", placeItems: "center", height: "100%" }}>
            <Spin size="large" />
          </div>
        ) : (
          <Alert
            type="info"
            message="尚未接入任何数据域"
            description="尚未从 DataHub 同步到任何数据域，请在「本体建模」页确认接入配置。"
            showIcon
          />
        )
      ) : (
        <div className="chatbi-layout">
          {sidebarVisible && (
            <ChatBiSidebar
              domainIds={domainIds}
              domainList={domainList}
              conversations={conversations}
              archivedConversations={archivedConversations}
              activeConversationId={activeConversationId}
              loadingConversations={loadingConversations}
              searchQuery={searchQuery}
              showArchived={showArchived}
              expandedCategories={expandedCategories}
              displayConversations={displayConversations}
              categorizedConvs={categorizedConvs}
              timeGrouped={timeGrouped}
              sortedCatNames={sortedCatNames}
              existingCategories={existingCategoryNames}
              totalArchived={totalArchived}
              onSearchQueryChange={setSearchQuery}
              onSelectConversation={handleSelectConversation}
              onNewConversation={handleNewConversation}
              onOpenRename={handleOpenRename}
              onMoveConversation={handleMoveConversation}
              onDeleteConversation={handleDeleteConversation}
              onTogglePin={handleTogglePin}
              onToggleArchive={handleToggleArchive}
              onToggleCategory={handleToggleCategory}
              onNewCategory={handleNewCategory}
              onOpenCategoryDialog={openCategoryDialog}
              onCreateConvInCategory={handleCreateConvInCategory}
              onSetSearchParams={setSearchParams}
              onSetShowArchived={setShowArchived}
              batchMode={batchMode}
              batchSelectedIds={batchSelectedIds}
              batchDeleting={batchDeleting}
              onEnterBatchMode={handleEnterBatchMode}
              onExitBatchMode={handleExitBatchMode}
              onToggleBatchSelect={handleToggleBatchSelect}
              onBatchSelectAll={handleBatchSelectAll}
              onBatchDelete={handleBatchDelete}
              onClose={() => setSidebarVisible(false)}
            />
          )}
          <ChatBiMain
            domainIds={domainIds}
            scopeLabel={scopeLabel}
            activeConversationId={activeConversationId}
            conversations={conversations}
            archivedConversations={archivedConversations}
            sidebarVisible={sidebarVisible}
            onToggleSidebar={() => setSidebarVisible((v) => !v)}
            onConversationActivity={handleConversationActivity}
          />
        </div>
      )}

      <Modal
        title="重命名对话"
        open={renameModalOpen}
        onOk={() => void handleConfirmRename()}
        onCancel={() => {
          setRenameModalOpen(false);
          setRenameTarget(null);
        }}
        okText="确认"
        cancelText="取消"
        destroyOnHidden
      >
        <Input
          placeholder="对话名称"
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onPressEnter={() => void handleConfirmRename()}
          autoFocus
        />
      </Modal>

      <Modal
        title={
          catDialogMode === "create"
            ? "新建分类"
            : catDialogMode === "rename"
              ? "重命名分类"
              : "删除分类"
        }
        open={catDialogOpen}
        onOk={() => void handleCategoryDialogConfirm()}
        onCancel={() => setCatDialogOpen(false)}
        okText={catDialogMode === "delete" ? "删除" : "确认"}
        okType={catDialogMode === "delete" ? "danger" : "primary"}
        cancelText="取消"
        destroyOnHidden
      >
        {catDialogMode === "create" && (
          <Input
            placeholder="输入新分类名称"
            value={catDialogName}
            onChange={(e) => setCatDialogName(e.target.value)}
            onPressEnter={() => void handleCategoryDialogConfirm()}
            autoFocus
          />
        )}
        {catDialogMode === "rename" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <span style={{ color: "var(--om-text-tertiary)", fontSize: 12 }}>当前名称</span>
              <div style={{ fontWeight: 500, marginTop: 4 }}>{catDialogName}</div>
            </div>
            <Input
              placeholder="输入新名称"
              value={catDialogNewName}
              onChange={(e) => setCatDialogNewName(e.target.value)}
              onPressEnter={() => void handleCategoryDialogConfirm()}
              autoFocus
            />
          </div>
        )}
        {catDialogMode === "delete" && (
          <div>确定删除分类「{catDialogName}」吗？分类下的所有对话将被移至「未分类」。</div>
        )}
      </Modal>
    </PageContainer>
  );
}

interface ChatBiMainProps {
  domainIds: string[];
  scopeLabel: string;
  activeConversationId: string | null;
  conversations: ChatBiConversation[];
  archivedConversations: ChatBiConversation[];
  sidebarVisible: boolean;
  onToggleSidebar: () => void;
  onConversationActivity: (update: {
    id: string;
    title?: string | null;
    last_message_preview?: string;
    isNew?: boolean;
  }) => void;
}

const ChatBiMain = memo(function ChatBiMain({
  domainIds,
  scopeLabel,
  activeConversationId,
  conversations,
  archivedConversations,
  sidebarVisible,
  onToggleSidebar,
  onConversationActivity,
}: ChatBiMainProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [input, setInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const loadingConversationRef = useRef<string | null>(null);
  const skipLoadForIdRef = useRef<string | null>(null);
  const navigate = useNavigate();

  const handleGenerateApp = useCallback(
    async (
      question: string,
      appType: "data_table" | "screen" | "dashboard",
      payload?: ChatBiAnswer,
      /** agent 提案给的看板名；动作条不传，沿用后端按问题截断的默认名。 */
      name?: string,
    ) => {
      const anchorDomainId = domainIds[0];
      if (!anchorDomainId || !question.trim()) {
        message.warning("请先选择至少一个数据域再生成数据应用");
        return;
      }
      const hide = message.loading(
        appType === "screen"
          ? "正在生成大屏…"
          : appType === "dashboard"
            ? "正在生成看板…"
            : "正在生成表格…",
        0,
      );
      try {
        const app = await api.generateDataAppFromChat({
          domain_id: anchorDomainId,
          app_type: appType,
          question,
          conversation_id: activeConversationId ?? undefined,
          // 复用当前回答的口径，保证生成应用与对话展示一致（不重调 LLM）
          caliber_decomposition: payload?.caliber_decomposition,
          referenced_objects: payload?.referenced_objects,
          name,
        });
        hide();
        message.success("已生成数据应用草稿");
        // 留痕：数据应用落成即「结果确认」环——此前这个确认走 REST 旁路且立刻导航离开，
        // 会话里完全看不出用户到底点没点、生成了哪个应用。
        recordDecisionQuietly(activeConversationId ?? undefined, {
          node: "result",
          stage: "data_app",
          trigger: "app_generated",
          summary: `生成${appType === "dashboard" ? "看板" : appType === "screen" ? "大屏" : "表格"}「${app.name ?? name ?? question}」`,
          chosen: { app_type: appType, question, name: app.name ?? name },
          ref_kind: "data_app",
          ref_id: app.id,
        });
        navigate(`/data-apps/${app.id}/edit`);
      } catch (err) {
        hide();
        message.error(err instanceof Error ? err.message : "生成失败");
      }
    },
    [domainIds, navigate, activeConversationId],
  );

  const [addDashOpen, setAddDashOpen] = useState(false);
  const [addDashTarget, setAddDashTarget] = useState<string | undefined>();
  const [addingDash, setAddingDash] = useState(false);
  const [dashboards, setDashboards] = useState<DataAppSummary[]>([]);
  const [pendingAdd, setPendingAdd] = useState<{
    question: string;
    payload?: ChatBiAnswer;
    /** agent 提案给的面板标题/图型；动作条不传，走默认（问题截断 + bar）。 */
    title?: string;
    vizType?: string;
  } | null>(null);

  const openAddToDashboard = useCallback(
    (question: string, payload?: ChatBiAnswer, title?: string, vizType?: string) => {
      const anchorDomainId = domainIds[0];
      if (!anchorDomainId) {
        message.warning("请先选择至少一个数据域再加入看板");
        return;
      }
      setPendingAdd({ question, payload, title, vizType });
      setAddDashTarget("__new__");
      setAddDashOpen(true);
      void api
        .listDataApps(anchorDomainId, "dashboard")
        .then((list) => {
          setDashboards(list);
          if (list.length > 0) setAddDashTarget(list[0].id);
        })
        .catch(() => setDashboards([]));
    },
    [domainIds],
  );

  const confirmAddToDashboard = useCallback(async () => {
    const anchorDomainId = domainIds[0];
    if (!anchorDomainId || !pendingAdd) return;
    setAddingDash(true);
    try {
      let dashboardId = addDashTarget;
      if (!dashboardId || dashboardId === "__new__") {
        const created = await api.createDataApp({
          domain_id: anchorDomainId,
          app_type: "dashboard",
          name: pendingAdd.title || pendingAdd.question.slice(0, 20) || "新看板",
        });
        dashboardId = created.id;
      }
      await api.generateWidgetFromChat({
        domain_id: anchorDomainId,
        question: pendingAdd.question,
        widget_type: pendingAdd.vizType || "bar",
        name: pendingAdd.title,
        caliber_decomposition: pendingAdd.payload?.caliber_decomposition,
        referenced_objects: pendingAdd.payload?.referenced_objects,
        dashboard_id: dashboardId,
      });
      setAddDashOpen(false);
      setPendingAdd(null);
      message.success("已生成图表并加入看板");
      // 留痕：面板落成同属「结果确认」环。记的是最终落到哪块看板，不是用户点开弹窗那一下——
      // 打开弹窗又取消不该在账本里留下一条「他确认了」。
      recordDecisionQuietly(activeConversationId ?? undefined, {
        node: "result",
        stage: "data_app",
        trigger: "widget_added",
        summary: `生成面板「${pendingAdd.title || pendingAdd.question}」并加入看板`,
        chosen: {
          question: pendingAdd.question,
          widget_type: pendingAdd.vizType || "bar",
          dashboard_id: dashboardId,
        },
        ref_kind: "data_app",
        ref_id: dashboardId,
      });
      navigate(`/data-apps/${dashboardId}/edit`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加入失败");
    } finally {
      setAddingDash(false);
    }
  }, [domainIds, pendingAdd, addDashTarget, navigate, activeConversationId]);

  /**
   * agent 主动提的面板/看板提案被点确认（app_proposal 块）。
   *
   * 走的是与动作条**完全相同**的两条路，只是标题/图型来自提案：
   * panel → 开「加入看板」弹窗让用户选落到哪块板；dashboard → 直接新建看板。
   * 口径一律取该条消息的 payload，不重新问一次数。
   */
  const handleProposeApp = useCallback(
    (
      proposal: Extract<ChatBiBlock, { type: "app_proposal" }>["proposal"],
      payload?: ChatBiAnswer,
    ) => {
      const question = proposal.create_payload.question;
      if (proposal.kind === "dashboard") {
        void handleGenerateApp(question, "dashboard", payload, proposal.name || proposal.title);
        return;
      }
      openAddToDashboard(question, payload, proposal.title, proposal.viz_type);
    },
    [handleGenerateApp, openAddToDashboard],
  );

  useEffect(() => {
    setLoadingSuggestions(true);
    api
      .chatBiSuggestions(domainIds)
      .then((res) => setSuggestions(res.suggestions))
      .catch(() => setSuggestions([]))
      .finally(() => setLoadingSuggestions(false));
  }, [domainIds]);

  useEffect(() => {
    if (!activeConversationId) {
      loadingConversationRef.current = null;
      setMessages([]);
      setLoadingMessages(false);
      return;
    }
    const conversationId = activeConversationId;
    if (skipLoadForIdRef.current === conversationId) {
      skipLoadForIdRef.current = null;
      loadingConversationRef.current = conversationId;
      setLoadingMessages(false);
      return;
    }
    loadingConversationRef.current = conversationId;
    setMessages([]);
    setLoadingMessages(true);
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getChatBiMessages(conversationId);
        if (cancelled || loadingConversationRef.current !== conversationId) return;
        const chatMessages: ChatMessage[] = data.map((m: ChatBiMessageItem) => ({
          id: m.id,
          role: m.role as "user" | "assistant",
          content: m.content,
          payload: m.payload
            ? ({
                ...m.payload,
                domain_ids: (m.payload.domain_ids as string[] | undefined) || domainIds,
                domain_id: (m.payload.domain_id as string | undefined) || domainIds[0],
                domain_name: (m.payload.domain_name as string | undefined) || scopeLabel,
              } as ChatBiAnswer)
            : undefined,
        }));
        setMessages(chatMessages);
      } catch {
        if (!cancelled && loadingConversationRef.current === conversationId) {
          setMessages([]);
        }
      } finally {
        if (!cancelled && loadingConversationRef.current === conversationId) {
          setLoadingMessages(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeConversationId, domainIds, scopeLabel]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, submitting]);

  const activeConversation = useMemo(() => {
    if (!activeConversationId) return null;
    return (
      [...conversations, ...archivedConversations].find((c) => c.id === activeConversationId) ??
      null
    );
  }, [activeConversationId, conversations, archivedConversations]);

  const submit = useCallback(
    async (question: string) => {
      const trimmed = question.trim();
      if (!trimmed || submitting) return;
      setInput("");

      const userMsg: ChatMessage = { role: "user", content: trimmed };
      const pendingMsg: ChatMessage = {
        role: "assistant",
        // 正文留空：等待期的交代由 pending 的打字点和思考流水折叠头的「正在思考…」负责，
        // 再放一句「思考中…」就是同一件事在同一屏说两遍。
        content: "",
        pending: true,
        live: true,
      };
      const history: ChatBiHistoryItem[] = messages
        .filter((m) => !m.pending && !m.error)
        .map((m) => ({ role: m.role, content: m.content }));

      setMessages((prev) => [...prev, userMsg, pendingMsg]);
      setSubmitting(true);
      // 思考流水折叠头显示的耗时锚点。落库的 agent_run 也有起止时间，但那要等这一轮结束
      // 才回来；现场展示需要一个此刻就能读的秒表。
      const askedAt = Date.now();

      try {
        const requestDomainIds = activeConversation?.domain_ids ?? domainIds;
        await api.askChatBiStream(
          {
            // 续聊以会话创建时的作用域为准；页面筛选可能在填写交互表单期间变化。
            domain_ids: requestDomainIds,
            question: trimmed,
            history,
            conversation_id: activeConversationId ?? undefined,
          },
          (ev) => {
            if (ev.type === "meta") {
              if (!activeConversationId && ev.conversation_id) {
                skipLoadForIdRef.current = ev.conversation_id;
                onConversationActivity({
                  id: ev.conversation_id,
                  title: ev.conversation_title ?? undefined,
                  last_message_preview: trimmed.slice(0, 80),
                  isNew: true,
                });
              } else if (ev.conversation_id) {
                onConversationActivity({
                  id: ev.conversation_id,
                  title: ev.conversation_title ?? undefined,
                  last_message_preview: trimmed.slice(0, 80),
                });
              }
              return;
            }
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (!last || last.role !== "assistant") return prev;
              const cur = { ...last };
              // 收到任一事件即退出纯 pending（typing dots）态，转为实时展示步骤/答案
              cur.pending = false;
              // 每个事件都推进秒表，最后一个事件的读数就是这一轮的总耗时。
              cur.thinkingMs = Date.now() - askedAt;
              // done/error 之外都还在路上——含「最后一步跑完、答案未出」的那段校验空档。
              cur.live = ev.type !== "done" && ev.type !== "error";
              const payload: ChatBiAnswer = {
                domain_ids: activeConversation?.domain_ids ?? domainIds,
                domain_id: (activeConversation?.domain_ids ?? domainIds)[0],
                domain_name: scopeLabel,
                answer: "",
                used_mock: false,
                ...(cur.payload ?? {}),
                steps: [...(cur.payload?.steps ?? [])],
              };
              switch (ev.type) {
                case "step_start":
                  payload.steps = [
                    ...(payload.steps ?? []),
                    {
                      index: ev.index,
                      tool: ev.tool,
                      arguments: ev.arguments,
                      status: "running",
                    } as ChatBiAgentStep,
                  ];
                  cur.payload = payload;
                  break;
                case "step_done":
                  payload.steps = (payload.steps ?? []).map((s) =>
                    s.index === ev.index ? { ...s, status: ev.status, summary: ev.summary } : s,
                  );
                  cur.payload = payload;
                  break;
                case "thought":
                  payload.steps = [
                    ...(payload.steps ?? []),
                    {
                      index: ev.index,
                      kind: "thought",
                      tool: "",
                      text: ev.text,
                      status: "succeeded",
                    } as ChatBiAgentStep,
                  ];
                  cur.payload = payload;
                  break;
                case "repair": {
                  // 校验未过、正在重写。不展示是最差的选择——用户只会看到一段
                  // 无解释的停顿；这里如实说明「在核对」，而不是假装无事发生。
                  // 后端发的是具体哪几处没被证实，用它，不要在前端另编一句更含糊的。
                  const reasons = (ev.reasons ?? []).filter(Boolean);
                  const shown = reasons.slice(0, 2).join("、");
                  const more = reasons.length > 2 ? `等 ${reasons.length} 处` : "";
                  payload.steps = [
                    ...(payload.steps ?? []),
                    {
                      index: (payload.steps ?? []).length,
                      kind: "repair",
                      tool: "",
                      text: shown
                        ? `${shown}${more}，正在依据已查到的事实重写`
                        : "答案未通过可靠性核验，正在依据已检索到的事实重写",
                      status: "running",
                    } as ChatBiAgentStep,
                  ];
                  cur.payload = payload;
                  break;
                }
                case "token":
                  cur.content = cur.content + ev.delta;
                  cur.streaming = true;
                  cur.payload = payload;
                  break;
                case "done":
                  cur.content = ev.payload.answer;
                  cur.payload = ev.payload;
                  cur.streaming = false;
                  break;
                case "error":
                  cur.content = `抱歉，回答失败：${ev.message}`;
                  cur.error = true;
                  cur.streaming = false;
                  break;
              }
              next[next.length - 1] = cur;
              return next;
            });
          },
        );
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err);
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content: `抱歉，回答失败：${errMsg}`,
            error: true,
          };
          return next;
        });
      } finally {
        setSubmitting(false);
        // 流被掐断（网络断、服务重启）时不会有 done/error 事件，live 会一直挂着，
        // 折叠头就永远停在「正在思考…」。这里兜底收尾。
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== "assistant" || !last.live) return prev;
          const next = [...prev];
          next[next.length - 1] = { ...last, live: false };
          return next;
        });
      }
    },
    [
      domainIds,
      scopeLabel,
      activeConversationId,
      activeConversation,
      messages,
      submitting,
      onConversationActivity,
    ],
  );

  return (
    <DecisionLedgerProvider conversationId={activeConversationId ?? undefined}>
      <section className="chatbi-shell">
        <div className="chatbi-shell-topbar">
          <Tooltip title={sidebarVisible ? "收起侧栏" : "展开侧栏"}>
            <Button
              type="text"
              icon={sidebarVisible ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />}
              onClick={onToggleSidebar}
            />
          </Tooltip>
          {activeConversation && (
            <div className="chatbi-shell-topbar-title">{activeConversation.title}</div>
          )}
          <div className="chatbi-shell-domain">
            <Tag color="blue" style={{ borderRadius: 6 }}>
              {scopeLabel}
            </Tag>
          </div>
        </div>

        <ChatBiMessages
          scrollRef={scrollRef}
          loadingMessages={loadingMessages}
          messages={messages}
          activeConversationId={activeConversationId}
          scopeLabel={scopeLabel}
          loadingSuggestions={loadingSuggestions}
          suggestions={suggestions}
          submitting={submitting}
          onSuggestionClick={submit}
          onGenerateApp={handleGenerateApp}
          onAddToDashboard={openAddToDashboard}
          onProposeApp={handleProposeApp}
        />

        <ChatBiComposer
          scopeLabel={scopeLabel}
          input={input}
          submitting={submitting}
          onInputChange={setInput}
          onSubmit={submit}
        />

        <Modal
          title="加入看板"
          open={addDashOpen}
          onCancel={() => setAddDashOpen(false)}
          onOk={confirmAddToDashboard}
          okText="生成图表并加入"
          confirmLoading={addingDash}
        >
          <div style={{ marginBottom: 8, color: "var(--om-text-tertiary)" }}>
            基于当前回答的口径生成一个可复用图表，并加入选定看板（不重调模型）。
          </div>
          <Select
            style={{ width: "100%" }}
            placeholder="选择目标看板（或新建）"
            value={addDashTarget}
            onChange={setAddDashTarget}
            options={[
              { label: "＋ 新建看板", value: "__new__" },
              ...dashboards.map((d) => ({ label: d.name, value: d.id })),
            ]}
          />
        </Modal>
      </section>
    </DecisionLedgerProvider>
  );
});
