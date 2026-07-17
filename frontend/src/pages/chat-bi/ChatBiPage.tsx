import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Input,
  message,
  Modal,
  Spin,
  Tag,
  Tooltip,
} from "antd";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../../api";
import { PageContainer } from "../../components/PageContainer";
import { useApi } from "../../hooks/useApi";
import type {
  ChatBiAnswer,
  ChatBiCategoryItem,
  ChatBiConversation,
  ChatBiHistoryItem,
  ChatBiMessageItem,
  DomainContext,
} from "../../types";
import { ChatBiComposer } from "./ChatBiComposer";
import { ChatBiMessages } from "./ChatBiMessages";
import { ChatBiSidebar } from "./ChatBiSidebar";
import {
  EMPTY_DEPS,
  getTimeGroup,
  type ChatMessage,
  type TimeGroup,
} from "./utils";

export function ChatBiPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const domainIdParam = searchParams.get("domain") || undefined;

  const { data: domains } = useApi<DomainContext[]>(
    async () => api.listDomains(),
    EMPTY_DEPS,
  );

  const domainList = domains ?? [];
  const domainId = useMemo(() => {
    if (domainIdParam) return domainIdParam;
    return domainList[0]?.id;
  }, [domainIdParam, domainList]);

  useEffect(() => {
    if (!domainIdParam && domainId) {
      setSearchParams({ domain: domainId }, { replace: true });
    }
  }, [domainIdParam, domainId, setSearchParams]);

  const activeDomain = domainList.find((d) => d.id === domainId);

  const [conversations, setConversations] = useState<ChatBiConversation[]>([]);
  const [archivedConversations, setArchivedConversations] = useState<ChatBiConversation[]>([]);
  const [categories, setCategories] = useState<ChatBiCategoryItem[]>([]);
  const [loadingConversations, setLoadingConversations] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [sidebarVisible, setSidebarVisible] = useState(true);
  const [showArchived, setShowArchived] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());

  const patchConversation = useCallback(
    (id: string, patch: Partial<ChatBiConversation>) => {
      const apply = (list: ChatBiConversation[]) =>
        list.map((c) => (c.id === id ? { ...c, ...patch } : c));
      setConversations(apply);
      setArchivedConversations(apply);
    },
    [],
  );

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
      setConversations((prev) =>
        prev.some((c) => c.id === conv.id) ? prev : [conv, ...prev],
      );
    }
  }, []);

  const [renameModalOpen, setRenameModalOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<ChatBiConversation | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const [catDialogOpen, setCatDialogOpen] = useState(false);
  const [catDialogMode, setCatDialogMode] = useState<"create" | "rename" | "delete">("create");
  const [catDialogName, setCatDialogName] = useState("");
  const [catDialogNewName, setCatDialogNewName] = useState("");
  const [catDialogMoveConv, setCatDialogMoveConv] = useState<ChatBiConversation | null>(null);

  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hasConversationDataRef = useRef(false);

  useEffect(() => {
    setActiveConversationId(null);
    hasConversationDataRef.current = false;
  }, [domainId]);

  useEffect(() => {
    if (!domainId) {
      setConversations([]);
      setArchivedConversations([]);
      setCategories([]);
      setLoadingConversations(false);
      hasConversationDataRef.current = false;
      return;
    }
    const showLoading = !hasConversationDataRef.current;
    if (showLoading) setLoadingConversations(true);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(async () => {
      try {
        const [active, archived, cats] = await Promise.all([
          api.listChatBiConversations(domainId, searchQuery || undefined, false),
          api.listChatBiConversations(domainId, searchQuery || undefined, true),
          api.listChatBiCategories(domainId),
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
  }, [domainId, searchQuery]);

  const handleSelectConversation = useCallback((id: string) => {
    setActiveConversationId(id);
    setShowArchived(false);
  }, []);

  const handleNewConversation = useCallback(async () => {
    if (!domainId) return;
    setShowArchived(false);
    try {
      const conv = await api.createChatBiConversation({ domain_id: domainId });
      prependConversation(conv);
      setActiveConversationId(conv.id);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建对话失败");
    }
  }, [domainId, prependConversation]);

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

  const openCategoryDialog = useCallback(
    (
      mode: "create" | "rename" | "delete",
      name?: string,
      moveConv?: ChatBiConversation | null,
    ) => {
      setCatDialogMode(mode);
      setCatDialogName(name || "");
      setCatDialogNewName("");
      setCatDialogMoveConv(moveConv || null);
      setCatDialogOpen(true);
    },
    [],
  );

  const handleCategoryDialogConfirm = useCallback(async () => {
    if (!domainId) return;
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
            domain_id: domainId,
            category: name,
          });
          prependConversation(conv);
          setActiveConversationId(conv.id);
        }
        setCategories((prev) =>
          prev.some((c) => c.name === name)
            ? prev
            : [...prev, { name, conversation_count: 1 }],
        );
        setExpandedCategories((prev) => new Set(prev).add(name));
        setCatDialogOpen(false);
      } else if (catDialogMode === "rename") {
        if (!catDialogNewName.trim() || !catDialogName) return;
        const newName = catDialogNewName.trim();
        await api.renameChatBiCategory({
          domain_id: domainId,
          old_name: catDialogName,
          new_name: newName,
        });
        const renameCategory = (list: ChatBiConversation[]) =>
          list.map((c) =>
            c.category === catDialogName ? { ...c, category: newName } : c,
          );
        setConversations(renameCategory);
        setArchivedConversations(renameCategory);
        setCategories((prev) =>
          prev.map((c) =>
            c.name === catDialogName ? { ...c, name: newName } : c,
          ),
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
          domain_id: domainId,
          name: catDialogName,
        });
        const clearCategory = (list: ChatBiConversation[]) =>
          list.map((c) =>
            c.category === catDialogName ? { ...c, category: null } : c,
          );
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
    domainId,
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

  const handleCreateConvInCategory = useCallback(async (catName: string) => {
    if (!domainId) return;
    try {
      const conv = await api.createChatBiConversation({
        domain_id: domainId,
        category: catName,
      });
      prependConversation(conv);
      setActiveConversationId(conv.id);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建对话失败");
    }
  }, [domainId, prependConversation]);

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
          domain_id: domainId!,
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
    [domainId, patchConversation, prependConversation],
  );

  const handleToggleCategory = useCallback((name: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const displayConversations = showArchived ? archivedConversations : conversations;

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
      {!domainId ? (
        <Alert
          type="info"
          message="请先选择数据域"
          description="尚未从 DataHub 同步到任何数据域，请在「本体建模」页确认接入配置。"
          showIcon
        />
      ) : !activeDomain ? (
        <div style={{ display: "grid", placeItems: "center", height: "100%" }}>
          <Spin size="large" />
        </div>
      ) : (
        <div className="chatbi-layout">
          {sidebarVisible && (
            <ChatBiSidebar
              domainId={domainId}
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
            />
          )}
          <ChatBiMain
            domainId={domainId}
            activeDomain={activeDomain}
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
        destroyOnClose
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
        destroyOnClose
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
              <span style={{ color: "var(--om-text-tertiary)", fontSize: 12 }}>
                当前名称
              </span>
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
          <div>
            确定删除分类「{catDialogName}」吗？分类下的所有对话将被移至「未分类」。
          </div>
        )}
      </Modal>
    </PageContainer>
  );
}

interface ChatBiMainProps {
  domainId: string;
  activeDomain: DomainContext;
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
  domainId,
  activeDomain,
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

  useEffect(() => {
    if (!domainId) return;
    setLoadingSuggestions(true);
    api.chatBiSuggestions(domainId)
      .then((res) => setSuggestions(res.suggestions))
      .catch(() => setSuggestions([]))
      .finally(() => setLoadingSuggestions(false));
  }, [domainId]);

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
        const chatMessages: ChatMessage[] = data.map(
          (m: ChatBiMessageItem) => ({
            role: m.role as "user" | "assistant",
            content: m.content,
            payload: m.payload
              ? ({
                  ...m.payload,
                  domain_id:
                    (m.payload.domain_id as string | undefined) || domainId,
                  domain_name:
                    (m.payload.domain_name as string | undefined) ||
                    activeDomain.name,
                } as ChatBiAnswer)
              : undefined,
          }),
        );
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
  }, [activeConversationId, domainId, activeDomain.name]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, submitting]);

  const activeConversation = useMemo(() => {
    if (!activeConversationId) return null;
    return [...conversations, ...archivedConversations].find(
      (c) => c.id === activeConversationId,
    ) ?? null;
  }, [activeConversationId, conversations, archivedConversations]);

  const submit = useCallback(
    async (question: string) => {
      const trimmed = question.trim();
      if (!trimmed || !domainId || submitting) return;
      setInput("");

      const userMsg: ChatMessage = { role: "user", content: trimmed };
      const pendingMsg: ChatMessage = {
        role: "assistant",
        content: "思考中…",
        pending: true,
      };
      const history: ChatBiHistoryItem[] = messages
        .filter((m) => !m.pending && !m.error)
        .map((m) => ({ role: m.role, content: m.content }));

      setMessages((prev) => [...prev, userMsg, pendingMsg]);
      setSubmitting(true);

      try {
        const answer = await api.askChatBi({
          domain_id: domainId,
          question: trimmed,
          history,
          conversation_id: activeConversationId ?? undefined,
        });

        if (!activeConversationId && answer.conversation_id) {
          skipLoadForIdRef.current = answer.conversation_id;
          onConversationActivity({
            id: answer.conversation_id,
            title: answer.conversation_title,
            last_message_preview: trimmed.slice(0, 80),
            isNew: true,
          });
        } else if (answer.conversation_id) {
          onConversationActivity({
            id: answer.conversation_id,
            title: answer.conversation_title,
            last_message_preview: trimmed.slice(0, 80),
          });
        }

        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content: answer.answer,
            payload: answer,
          };
          return next;
        });
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
      }
    },
    [domainId, activeConversationId, messages, submitting, onConversationActivity],
  );

  return (
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
          <div className="chatbi-shell-topbar-title">
            {activeConversation.title}
          </div>
        )}
        <div className="chatbi-shell-domain">
          <Tag color="blue" style={{ borderRadius: 6 }}>{activeDomain.name}</Tag>
        </div>
      </div>

      <ChatBiMessages
        scrollRef={scrollRef}
        loadingMessages={loadingMessages}
        messages={messages}
        activeConversationId={activeConversationId}
        activeDomain={activeDomain}
        loadingSuggestions={loadingSuggestions}
        suggestions={suggestions}
        submitting={submitting}
        onSuggestionClick={submit}
      />

      <ChatBiComposer
        activeDomain={activeDomain}
        input={input}
        submitting={submitting}
        onInputChange={setInput}
        onSubmit={submit}
      />
    </section>
  );
});
