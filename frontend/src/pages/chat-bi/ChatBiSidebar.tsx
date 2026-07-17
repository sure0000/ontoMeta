import {
  DeleteOutlined,
  EditOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  InboxOutlined,
  MessageOutlined,
  MoreOutlined,
  PlusOutlined,
  PushpinFilled,
  PushpinOutlined,
  RightOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  Badge,
  Button,
  Dropdown,
  Input,
  Select,
  Spin,
  Tooltip,
} from "antd";
import { memo } from "react";
import type { ChatBiConversation, DomainContext } from "../../types";
import {
  TIME_GROUP_LABEL,
  TIME_GROUP_ORDER,
  relativeTime,
  type TimeGroup,
} from "./utils";

export interface ChatBiSidebarProps {
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
  onOpenCategoryDialog: (
    mode: "create" | "rename" | "delete",
    name?: string,
    moveConv?: ChatBiConversation | null,
  ) => void;
  onCreateConvInCategory: (catName: string) => Promise<void>;
  onSetSearchParams: (params: Record<string, string>) => void;
  onSetShowArchived: (v: boolean) => void;
}

export const ChatBiSidebar = memo(function ChatBiSidebar({
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
}: ChatBiSidebarProps) {
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
