import {
  DeleteOutlined,
  EditOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  InboxOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  MoreOutlined,
  PushpinFilled,
  PushpinOutlined,
  PlusOutlined,
  RightOutlined,
  RobotOutlined,
  SearchOutlined,
  SendOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Badge,
  Button,
  Dropdown,
  Input,
  message,
  Modal,
  Select,
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
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { useApi } from "../hooks/useApi";
import type {
  ChatBiAnswer,
  ChatBiCaliberItem,
  ChatBiCaliberKind,
  ChatBiCaliberReference,
  ChatBiCategoryItem,
  ChatBiConversation,
  ChatBiHistoryItem,
  ChatBiMessageItem,
  DomainContext,
} from "../types";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  payload?: ChatBiAnswer;
  pending?: boolean;
  error?: boolean;
}

/* ============================================================
 * Helpers
 * ============================================================ */

type TimeGroup = "pinned" | "today" | "yesterday" | "thisWeek" | "thisMonth" | "older";

function getTimeGroup(conv: ChatBiConversation): TimeGroup {
  if (conv.is_pinned) return "pinned";
  const now = new Date();
  const d = new Date(conv.updated_at);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
  const startOfWeek = new Date(startOfToday.getTime() - startOfToday.getDay() * 86400000);
  const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);

  if (d >= startOfToday) return "today";
  if (d >= startOfYesterday) return "yesterday";
  if (d >= startOfWeek) return "thisWeek";
  if (d >= startOfMonth) return "thisMonth";
  return "older";
}

const TIME_GROUP_LABEL: Record<TimeGroup, string> = {
  pinned: "置顶",
  today: "今天",
  yesterday: "昨天",
  thisWeek: "本周",
  thisMonth: "本月",
  older: "更早",
};

const TIME_GROUP_ORDER: TimeGroup[] = ["pinned", "today", "yesterday", "thisWeek", "thisMonth", "older"];

function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

const EMPTY_DEPS: unknown[] = [];

/* ============================================================
 * ChatBiPage — domain + conversation list owner only
 * ============================================================ */

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

  // ---- conversation list state (parent owns, sidebar consumes) ----
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

  // ---- modals (parent owns, can be triggered from sidebar or detail) ----
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

  // ---- Reset active conversation when domain changes ----
  useEffect(() => {
    setActiveConversationId(null);
    hasConversationDataRef.current = false;
  }, [domainId]);

  // ---- Load conversations (initial + search only; sidebar ops use local updates) ----
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

  // ---- Conversation actions ----
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

  // ---- Category dialog ----
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

  // ---- derived data ----
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

  // ---- Render ----
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
          <ChatBiDetail
            domainId={domainId}
            activeDomain={activeDomain!}
            activeConversationId={activeConversationId}
            conversations={conversations}
            archivedConversations={archivedConversations}
            sidebarVisible={sidebarVisible}
            onToggleSidebar={() => setSidebarVisible((v) => !v)}
            onConversationActivity={handleConversationActivity}
          />
        </div>
      )}

      {/* ---- Rename modal ---- */}
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

      {/* ---- Category dialog ---- */}
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

/* ============================================================
 * ChatBiSidebar — memoized conversation list
 * ============================================================ */

interface SidebarProps {
  domainId: string;
  domainList: DomainContext[];
  conversations: ChatBiConversation[];
  archivedConversations: ChatBiConversation[];
  activeConversationId: string | null;
  loadingConversations: boolean;
  searchQuery: string;
  showArchived: boolean;
  expandedCategories: Set<string>;
  displayConversations: ChatBiConversation[];
  categorizedConvs: Record<string, ChatBiConversation[]>;
  timeGrouped: Record<TimeGroup, ChatBiConversation[]>;
  sortedCatNames: string[];
  existingCategories: string[];
  totalArchived: number;
  onSearchQueryChange: (q: string) => void;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => Promise<void>;
  onOpenRename: (conv: ChatBiConversation) => void;
  onMoveConversation: (conv: ChatBiConversation, category: string | null) => void;
  onDeleteConversation: (conv: ChatBiConversation) => void;
  onTogglePin: (conv: ChatBiConversation) => void;
  onToggleArchive: (conv: ChatBiConversation) => void;
  onToggleCategory: (name: string) => void;
  onNewCategory: (conv: ChatBiConversation) => void;
  onOpenCategoryDialog: (mode: "create" | "rename" | "delete", name?: string, moveConv?: ChatBiConversation | null) => void;
  onCreateConvInCategory: (catName: string) => Promise<void>;
  onSetSearchParams: (params: Record<string, string>) => void;
  onSetShowArchived: (v: boolean) => void;
}

const ChatBiSidebar = memo(function ChatBiSidebar({
  domainId,
  domainList,
  conversations,
  archivedConversations,
  activeConversationId,
  loadingConversations,
  searchQuery,
  showArchived,
  expandedCategories,
  displayConversations,
  categorizedConvs,
  timeGrouped,
  sortedCatNames,
  existingCategories,
  totalArchived,
  onSearchQueryChange,
  onSelectConversation,
  onNewConversation,
  onOpenRename,
  onMoveConversation,
  onDeleteConversation,
  onTogglePin,
  onToggleArchive,
  onToggleCategory,
  onNewCategory,
  onOpenCategoryDialog,
  onCreateConvInCategory,
  onSetSearchParams,
  onSetShowArchived,
}: SidebarProps) {
  return (
    <aside className="chatbi-sidebar">
      <div className="chatbi-sidebar-header">
        <Select
          style={{ width: "100%" }}
          placeholder="选择数据域"
          value={domainId}
          onChange={(value) => onSetSearchParams({ domain: value })}
          options={domainList.map((d) => ({
            value: d.id,
            label: d.name,
          }))}
          notFoundContent="暂无数据域"
        />
        <Input
          placeholder="搜索对话…"
          prefix={<SearchOutlined />}
          value={searchQuery}
          onChange={(e) => onSearchQueryChange(e.target.value)}
          allowClear
          size="small"
        />
        <Dropdown
          menu={{
            items: [
              {
                key: "new-conv",
                label: "新对话",
                icon: <MessageOutlined />,
                onClick: () => void onNewConversation(),
              },
              {
                key: "new-cat",
                label: "新建分类",
                icon: <FolderAddOutlined />,
                onClick: () => onOpenCategoryDialog("create"),
              },
            ],
          }}
          trigger={["hover"]}
        >
          <Button
            type="primary"
            icon={<PlusOutlined />}
            block
            className="chatbi-new-chat-btn"
          >
            新对话
          </Button>
        </Dropdown>
      </div>

      <div className="chatbi-sidebar-list">
        {loadingConversations &&
        conversations.length === 0 &&
        archivedConversations.length === 0 ? (
          <div className="chatbi-sidebar-empty">
            <Spin size="small" />
          </div>
        ) : showArchived ? (
          <>
            <div style={{ padding: "4px 12px 8px" }}>
              <Button
                type="text"
                size="small"
                icon={<FolderOpenOutlined />}
                onClick={() => onSetShowArchived(false)}
                style={{ fontSize: 12 }}
              >
                返回活跃对话
              </Button>
            </div>
            {displayConversations.length === 0 ? (
              <div className="chatbi-sidebar-empty">暂无归档对话</div>
            ) : (
              displayConversations.map((conv) => (
                <ConversationItem
                  key={conv.id}
                  conversation={conv}
                  isActive={conv.id === activeConversationId}
                  onSelect={onSelectConversation}
                  onRename={onOpenRename}
                  onMove={onMoveConversation}
                  onDelete={onDeleteConversation}
                  onTogglePin={onTogglePin}
                  onToggleArchive={onToggleArchive}
                  existingCategories={existingCategories}
                  onNewCategory={onNewCategory}
                />
              ))
            )}
          </>
        ) : (
          <>
            {sortedCatNames.map((catName) => {
              const convs = categorizedConvs[catName] || [];
              const isExpanded = expandedCategories.has(catName);
              return (
                <div className="chatbi-category-folder" key={`cat-${catName}`}>
                  <button
                    type="button"
                    className="chatbi-category-folder-header"
                    onClick={() => onToggleCategory(catName)}
                  >
                    <span
                      className={`chatbi-category-folder-chevron${isExpanded ? " chatbi-category-folder-chevron--expanded" : ""}`}
                    >
                      <RightOutlined style={{ fontSize: 10 }} />
                    </span>
                    <FolderOutlined className="chatbi-category-folder-icon" />
                    <span className="chatbi-category-folder-name">
                      {catName}
                    </span>
                    <span className="chatbi-category-folder-count">
                      {convs.length}
                    </span>
                    <span
                      className="chatbi-category-folder-menu"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <Dropdown
                        menu={{
                          items: [
                            {
                              key: "rename",
                              label: "重命名",
                              icon: <EditOutlined />,
                              onClick: () =>
                                onOpenCategoryDialog("rename", catName),
                            },
                            {
                              key: "new-conv",
                              label: "在此新建对话",
                              icon: <PlusOutlined />,
                              onClick: () => void onCreateConvInCategory(catName),
                            },
                            { type: "divider" },
                            {
                              key: "delete",
                              label: "删除分类",
                              icon: <DeleteOutlined />,
                              danger: true,
                              onClick: () =>
                                onOpenCategoryDialog("delete", catName),
                            },
                          ],
                        }}
                        trigger={["click"]}
                      >
                        <Button
                          size="small"
                          type="text"
                          icon={<MoreOutlined />}
                        />
                      </Dropdown>
                    </span>
                  </button>
                  {isExpanded && (
                    <div className="chatbi-category-folder-items">
                      {convs.map((conv) => (
                        <ConversationItem
                          key={conv.id}
                          conversation={conv}
                          isActive={conv.id === activeConversationId}
                          onSelect={onSelectConversation}
                          onRename={onOpenRename}
                          onMove={onMoveConversation}
                          onDelete={onDeleteConversation}
                          onTogglePin={onTogglePin}
                          onToggleArchive={onToggleArchive}
                          existingCategories={existingCategories}
                          onNewCategory={onNewCategory}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}

            {TIME_GROUP_ORDER.map((group) => {
              const convs = timeGrouped[group];
              if (convs.length === 0) return null;
              return (
                <div className="chatbi-time-group" key={group}>
                  <div className="chatbi-time-group-label">
                    {TIME_GROUP_LABEL[group]}
                  </div>
                  {convs.map((conv) => (
                    <ConversationItem
                      key={conv.id}
                      conversation={conv}
                      isActive={conv.id === activeConversationId}
                      onSelect={onSelectConversation}
                      onRename={onOpenRename}
                      onMove={onMoveConversation}
                      onDelete={onDeleteConversation}
                      onTogglePin={onTogglePin}
                      onToggleArchive={onToggleArchive}
                      existingCategories={existingCategories}
                      onNewCategory={onNewCategory}
                    />
                  ))}
                </div>
              );
            })}
          </>
        )}

        {!showArchived && displayConversations.length === 0 && (
          <div className="chatbi-sidebar-empty">
            {searchQuery ? "未找到匹配的对话" : "暂无对话，点击上方「新对话」开始"}
          </div>
        )}
      </div>

      <div className="chatbi-sidebar-footer">
        <div style={{ flex: 1 }} />
        {totalArchived > 0 && !showArchived && (
          <Tooltip title={`归档 (${totalArchived})`}>
            <Badge count={totalArchived} size="small" offset={[-2, 2]}>
              <Button
                type="text"
                size="small"
                icon={<InboxOutlined />}
                onClick={() => onSetShowArchived(true)}
                style={{ color: "var(--om-text-secondary)" }}
              />
            </Badge>
          </Tooltip>
        )}
      </div>
    </aside>
  );
});

/* ============================================================
 * ChatBiDetail — owns messages + chat state; active id from parent
 * ============================================================ */

interface DetailProps {
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

const ChatBiDetail = memo(function ChatBiDetail({
  domainId,
  activeDomain,
  activeConversationId,
  conversations,
  archivedConversations,
  sidebarVisible,
  onToggleSidebar,
  onConversationActivity,
}: DetailProps) {
  // ---- chat state (local to this component) ----
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [input, setInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const loadingConversationRef = useRef<string | null>(null);
  const skipLoadForIdRef = useRef<string | null>(null);

  // ---- Load suggestions ----
  useEffect(() => {
    if (!domainId) return;
    setLoadingSuggestions(true);
    api.chatBiSuggestions(domainId)
      .then((res) => setSuggestions(res.suggestions))
      .catch(() => setSuggestions([]))
      .finally(() => setLoadingSuggestions(false));
  }, [domainId]);

  // ---- Load messages when activeConversationId changes ----
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

  // ---- Auto-scroll ----
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, submitting]);

  // ---- Active conversation info ----
  const activeConversation = useMemo(() => {
    if (!activeConversationId) return null;
    return [...conversations, ...archivedConversations].find(
      (c) => c.id === activeConversationId,
    ) ?? null;
  }, [activeConversationId, conversations, archivedConversations]);

  // ---- Submit ----
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

  // ---- Render ----
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

      <div className="chatbi-messages" ref={scrollRef}>
        {loadingMessages && messages.length === 0 && activeConversationId ? (
          <div className="chatbi-messages-loading">
            <Spin size="large" />
          </div>
        ) : messages.length === 0 ? (
          <div className="chatbi-welcome">
            <div className="chatbi-welcome-icon">
              <RobotOutlined />
            </div>
            <div className="chatbi-welcome-title">
              智能问数 · {activeDomain.name}
            </div>
            <div className="chatbi-welcome-desc">
              基于已发布的本体知识（对象、字段、关系、业务逻辑），
              用自然语言提问，获取数据口径解读与 SQL 建议。
            </div>
            {loadingSuggestions ? (
              <Spin size="small" style={{ marginTop: 8 }} />
            ) : suggestions.length > 0 ? (
              <div className="chatbi-suggestions">
                {suggestions.map((s, i) => (
                  <button
                    key={i}
                    className="chatbi-suggestion-chip"
                    onClick={() => void submit(s)}
                    disabled={submitting}
                    type="button"
                  >
                    {s}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          messages.map((msg, idx) => (
            <ChatBubble key={idx} message={msg} />
          ))
        )}
      </div>

      <div className="chatbi-composer">
        <div className="chatbi-composer-inner">
          <textarea
            className="chatbi-input"
            placeholder={`向「${activeDomain.name}」提问… (Enter 发送，Shift+Enter 换行)`}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit(input);
              }
            }}
            rows={1}
            disabled={submitting}
          />
          <button
            className={`chatbi-send-btn ${input.trim() && !submitting ? "chatbi-send-btn--active" : "chatbi-send-btn--disabled"}`}
            onClick={() => void submit(input)}
            disabled={!input.trim() || submitting}
            type="button"
            title="发送"
          >
            {submitting ? (
              <Spin size="small" style={{ color: "inherit" }} />
            ) : (
              <SendOutlined style={{ fontSize: 15 }} />
            )}
          </button>
        </div>
      </div>
    </section>
  );
});

/* ============================================================
 * ConversationItem
 * ============================================================ */

const ConversationItem = memo(function ConversationItem({
  conversation,
  isActive,
  onSelect,
  onRename,
  onMove,
  onDelete,
  onTogglePin,
  onToggleArchive,
  existingCategories,
  onNewCategory,
}: {
  conversation: ChatBiConversation;
  isActive: boolean;
  onSelect: (id: string) => void;
  onRename: (conv: ChatBiConversation) => void;
  onMove: (conv: ChatBiConversation, category: string | null) => void;
  onDelete: (conv: ChatBiConversation) => void;
  onTogglePin: (conv: ChatBiConversation) => void;
  onToggleArchive: (conv: ChatBiConversation) => void;
  existingCategories: string[];
  onNewCategory: (conv: ChatBiConversation) => void;
}) {
  const moveSubmenuItems = [
    {
      key: "move-uncategorized",
      label: "未分类",
      icon: <FolderOutlined />,
      onClick: () => onMove(conversation, null),
    },
    ...existingCategories
      .filter((c) => c !== conversation.category)
      .map((cat) => ({
        key: `move-${cat}`,
        label: cat,
        icon: <FolderOutlined />,
        onClick: () => onMove(conversation, cat),
      })),
    { type: "divider" as const },
    {
      key: "move-new",
      label: "+ 新建分类",
      icon: <FolderAddOutlined />,
      onClick: () => onNewCategory(conversation),
    },
  ];

  return (
    <div
      className={`chatbi-conv-item${isActive ? " chatbi-conv-item--active" : ""}`}
      onClick={() => onSelect(conversation.id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter") onSelect(conversation.id);
      }}
    >
      {conversation.is_pinned && !isActive ? (
        <PushpinFilled className="chatbi-conv-pin-icon" />
      ) : (
        <MessageOutlined className="chatbi-conv-icon" />
      )}
      <div className="chatbi-conv-body">
        <div className="chatbi-conv-title">
          {conversation.title || "新对话"}
        </div>
        <div className="chatbi-conv-meta">
          {conversation.last_message_preview && (
            <span className="chatbi-conv-preview">
              {conversation.last_message_preview}
            </span>
          )}
          {conversation.category && (
            <span className="chatbi-conv-category-tag">
              {conversation.category}
            </span>
          )}
        </div>
      </div>
      <span className="chatbi-conv-time">
        {relativeTime(conversation.updated_at)}
      </span>
      <span className="chatbi-conv-menu">
        <Dropdown
          menu={{
            items: [
              {
                key: "pin",
                label: conversation.is_pinned ? "取消置顶" : "置顶",
                icon: <PushpinOutlined />,
                onClick: () => onTogglePin(conversation),
              },
              {
                key: "rename",
                label: "重命名",
                icon: <EditOutlined />,
                onClick: () => onRename(conversation),
              },
              {
                key: "move",
                label: "移动到分类",
                icon: <FolderOutlined />,
                children: moveSubmenuItems,
              },
              { type: "divider" },
              {
                key: "archive",
                label: conversation.is_archived ? "取消归档" : "归档",
                icon: conversation.is_archived ? <FolderOpenOutlined /> : <InboxOutlined />,
                onClick: () => onToggleArchive(conversation),
              },
              { type: "divider" },
              {
                key: "delete",
                label: "删除",
                icon: <DeleteOutlined />,
                danger: true,
                onClick: () => onDelete(conversation),
              },
            ],
          }}
          trigger={["click"]}
        >
          <Button
            size="small"
            type="text"
            icon={<MoreOutlined />}
            onClick={(e) => e.stopPropagation()}
          />
        </Dropdown>
      </span>
    </div>
  );
});

/* ============================================================
 * ChatBubble
 * ============================================================ */

function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div
      className={`chatbi-bubble chatbi-bubble--${
        isUser ? "user" : "assistant"
      }`}
    >
      <div className="chatbi-bubble-body">
        {message.pending ? (
          <div className="chatbi-bubble-pending">
            <div className="chatbi-typing-dots">
              <span />
              <span />
              <span />
            </div>
            <span style={{ color: "var(--om-text-tertiary)", fontSize: 13 }}>正在结合本体知识思考…</span>
          </div>
        ) : (
          <>
            <MarkdownLite content={message.content} />
            {message.payload?.caliber_decomposition &&
              message.payload.caliber_decomposition.length > 0 && (
                <CaliberDecomposition
                  items={message.payload.caliber_decomposition}
                />
              )}
            {message.payload?.suggested_sql && (
              <SqlBlock sql={message.payload.suggested_sql} />
            )}
            {message.payload &&
              !isUser &&
              (message.payload.referenced_objects?.length ||
                message.payload.referenced_logics?.length) ? (
              <div className="chatbi-refs">
                {message.payload.referenced_objects?.map((r, i) => (
                  <Tag key={`o-${i}`} color="blue" style={{ borderRadius: 6 }}>
                    对象：{r.display_name ?? r.name ?? "—"}
                  </Tag>
                ))}
                {message.payload.referenced_logics?.map((r, i) => (
                  <Tag key={`l-${i}`} color="purple" style={{ borderRadius: 6 }}>
                    逻辑：{r.display_name ?? r.name ?? "—"}
                  </Tag>
                ))}
              </div>
            ) : null}
            {message.payload?.used_mock && !isUser && (
              <div className="chatbi-mock-hint">
                <Tag color="warning" style={{ borderRadius: 6 }}>Mock 模式</Tag>
                <span>未接入真实 LLM，已使用规则匹配回答。</span>
              </div>
            )}
            {message.error && (
              <div className="chatbi-mock-hint" style={{ color: "#ef4444" }}>
                回答出错，请重试。
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ============================================================
 * MarkdownLite / Line / InlineRender
 * ============================================================ */

function MarkdownLite({ content }: { content: string }) {
  const blocks: React.ReactNode[] = [];
  const lines = content.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fenceMatch = line.trim().match(/^```(\w*)$/);
    if (fenceMatch) {
      const lang = fenceMatch[1].toLowerCase();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      if (lang === "sql") {
        continue;
      }
      blocks.push(
        <pre key={key++} className="chatbi-codeblock">
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    blocks.push(<Line key={key++} raw={line} />);
    i++;
  }
  return <div className="chatbi-md">{blocks}</div>;
}

function Line({ raw }: { raw: string }) {
  if (!raw.trim()) return <div className="chatbi-md-line" />;

  if (raw.trim().startsWith(">")) {
    return (
      <blockquote className="chatbi-md-quote">
        <InlineRender text={raw.replace(/^\s*>\s?/, "")} />
      </blockquote>
    );
  }
  const listMatch = raw.match(/^\s*[-*]\s+(.*)$/);
  if (listMatch) {
    return (
      <div className="chatbi-md-listitem">
        <span className="chatbi-md-bullet">•</span>
        <span>
          <InlineRender text={listMatch[1]} />
        </span>
      </div>
    );
  }
  const headerMatch = raw.match(/^(#{1,4})\s+(.*)$/);
  if (headerMatch) {
    const level = headerMatch[1].length;
    const text = headerMatch[2];
    const className = `chatbi-md-h${Math.min(level, 4)}`;
    return (
      <div className={className}>
        <InlineRender text={text} />
      </div>
    );
  }
  return (
    <div className="chatbi-md-line">
      <InlineRender text={raw} />
    </div>
  );
}

function InlineRender({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  const rest = text;
  let key = 0;
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(rest)) !== null) {
    if (match.index > lastIndex) {
      parts.push(
        <span key={key++}>{rest.slice(lastIndex, match.index)}</span>,
      );
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      parts.push(
        <code key={key++} className="chatbi-md-inline-code">
          {token.slice(1, -1)}
        </code>,
      );
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < rest.length) {
    parts.push(<span key={key++}>{rest.slice(lastIndex)}</span>);
  }
  return <>{parts}</>;
}

/* ============================================================
 * CaliberDecomposition
 * ============================================================ */

const CALIBER_KIND_LABEL: Record<ChatBiCaliberKind, string> = {
  object_type: "对象",
  property: "字段",
  relation_type: "关系",
  business_logic: "业务逻辑",
};

const CALIBER_KIND_COLOR: Record<ChatBiCaliberKind, string> = {
  object_type: "blue",
  property: "cyan",
  relation_type: "geekblue",
  business_logic: "purple",
};

function CaliberDecomposition({
  items,
}: {
  items: ChatBiCaliberItem[];
}) {
  return (
    <div className="chatbi-caliber">
      <div className="chatbi-caliber-title">口径拆解 · 本体映射</div>
      <div className="chatbi-caliber-list">
        {items.map((item, idx) => (
          <div className="chatbi-caliber-item" key={idx}>
            <div className="chatbi-caliber-item-index">{idx + 1}</div>
            <div className="chatbi-caliber-item-body">
              <div className="chatbi-caliber-item-label">{item.label}</div>
              {item.description && (
                <div className="chatbi-caliber-item-desc">
                  {item.description}
                </div>
              )}
              {item.references.length > 0 && (
                <div className="chatbi-caliber-item-refs">
                  {item.references.map((ref, ri) => (
                    <CaliberRefChip key={ri} ref={ref} />
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CaliberRefChip({ ref }: { ref: ChatBiCaliberReference }) {
  const label = ref.display_name ?? ref.name ?? "—";
  const href = refToPath(ref);
  const kindLabel = CALIBER_KIND_LABEL[ref.kind] ?? ref.kind;
  const color = CALIBER_KIND_COLOR[ref.kind] ?? "default";
  if (href) {
    return (
      <Link to={href} className="chatbi-caliber-chip">
        <Tag color={color} bordered={false}>
          {kindLabel}
        </Tag>
        <span className="chatbi-caliber-chip-label">{label}</span>
        <span className="chatbi-caliber-chip-arrow">↗</span>
      </Link>
    );
  }
  return (
    <span className="chatbi-caliber-chip chatbi-caliber-chip--static">
      <Tag color={color} bordered={false}>
        {kindLabel}
      </Tag>
      <span className="chatbi-caliber-chip-label">{label}</span>
    </span>
  );
}

function refToPath(ref: ChatBiCaliberReference): string | null {
  if (!ref.id) return null;
  switch (ref.kind) {
    case "object_type":
      return `/ontology/${ref.id}`;
    case "relation_type":
      return `/ontology/relations/${ref.id}`;
    case "business_logic":
      return `/business-logic/${ref.id}`;
    case "property":
    default:
      return null;
  }
}

/* ============================================================
 * SqlBlock
 * ============================================================ */

const SQL_KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING",
  "LIMIT", "OFFSET", "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN",
  "OUTER JOIN", "FULL JOIN", "ON", "AS", "AND", "OR", "NOT",
  "IN", "NOT IN", "EXISTS", "BETWEEN", "LIKE", "IS NULL", "IS NOT NULL",
  "DISTINCT", "UNION", "UNION ALL", "INSERT INTO", "VALUES",
  "UPDATE", "SET", "DELETE", "CASE", "WHEN", "THEN", "ELSE", "END",
  "WITH", "OVER", "PARTITION BY", "DATE_SUB", "DATE_ADD",
  "CURDATE", "NOW", "CURRENT_DATE", "CURRENT_TIMESTAMP",
  "INTERVAL", "DAY", "MONTH", "YEAR",
  "COUNT", "SUM", "AVG", "MIN", "MAX",
  "ASC", "DESC", "TRUE", "FALSE", "NULL",
]);

function isSqlKeyword(token: string): boolean {
  return SQL_KEYWORDS.has(token.toUpperCase());
}

function highlightSql(sql: string): React.ReactNode[] {
  return sql.split("\n").map((line, idx) => (
    <div key={idx} className="chatbi-sql-line">
      {highlightSqlLine(line)}
    </div>
  ));
}

function highlightSqlLine(line: string): React.ReactNode[] {
  const tokens: React.ReactNode[] = [];
  const rest = line;
  let key = 0;
  const tokenRegex =
    /(--[^\n]*|'[^']*'|"[^"]*"|\b\d+(?:\.\d+)?\b|[(),.;]|\b[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:BY|JOIN|ALL|INTO|NOT|NULL))?\b)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenRegex.exec(rest)) !== null) {
    if (match.index > lastIndex) {
      tokens.push(
        <span key={key++}>{rest.slice(lastIndex, match.index)}</span>,
      );
    }
    const tok = match[0];
    if (tok.startsWith("--")) {
      tokens.push(
        <span key={key++} className="chatbi-sql-comment">{tok}</span>,
      );
    } else if (tok.startsWith("'") || tok.startsWith('"')) {
      tokens.push(
        <span key={key++} className="chatbi-sql-string">{tok}</span>,
      );
    } else if (/^\d/.test(tok)) {
      tokens.push(
        <span key={key++} className="chatbi-sql-number">{tok}</span>,
      );
    } else if (/^[(),.;]$/.test(tok)) {
      tokens.push(
        <span key={key++} className="chatbi-sql-punct">{tok}</span>,
      );
    } else if (isSqlKeyword(tok)) {
      tokens.push(
        <span key={key++} className="chatbi-sql-keyword">{tok}</span>,
      );
    } else {
      tokens.push(<span key={key++}>{tok}</span>);
    }
    lastIndex = match.index + tok.length;
  }
  if (lastIndex < rest.length) {
    tokens.push(<span key={key++}>{rest.slice(lastIndex)}</span>);
  }
  return tokens;
}

function SqlBlock({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(sql);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  };
  return (
    <div className="chatbi-sql">
      <div className="chatbi-sql-head">
        <span className="chatbi-sql-head-label">SUGGESTED SQL</span>
        <button
          className="chatbi-sql-copy"
          onClick={() => void handleCopy()}
          type="button"
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="chatbi-sql-pre">
        <code>{highlightSql(sql)}</code>
      </pre>
    </div>
  );
}
