import { RobotOutlined } from "@ant-design/icons";
import { Spin, message } from "antd";
import type { RefObject } from "react";
import { api } from "../../api";
import { ChatBubble, useArtifactDrawer } from "./ChatBiReferences";
import { ClosureCard } from "./ClosureCard";
import { useDecisionLedger } from "./DecisionLedger";
import type { ChatMessage } from "./utils";

export interface ChatBiMessagesProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  loadingMessages: boolean;
  messages: ChatMessage[];
  activeConversationId: string | null;
  scopeLabel: string;
  loadingSuggestions: boolean;
  suggestions: string[];
  submitting: boolean;
  onSuggestionClick: (s: string) => void;
  onGenerateApp?: (
    question: string,
    appType: "data_table" | "screen" | "dashboard",
    payload?: import("../../types").ChatBiAnswer,
  ) => void;
  onAddToDashboard?: (question: string, payload?: import("../../types").ChatBiAnswer) => void;
  /** agent 主动提的面板/看板提案被确认（app_proposal 块的按钮）。 */
  onProposeApp?: (
    proposal: Extract<import("../../types").ChatBiBlock, { type: "app_proposal" }>["proposal"],
    payload?: import("../../types").ChatBiAnswer,
  ) => void;
}

export function ChatBiMessages({
  scrollRef,
  loadingMessages,
  messages,
  activeConversationId,
  scopeLabel,
  loadingSuggestions,
  suggestions,
  submitting,
  onSuggestionClick,
  onGenerateApp,
  onAddToDashboard,
  onProposeApp,
}: ChatBiMessagesProps) {
  return (
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
          <div className="chatbi-welcome-title">Data Agent · {scopeLabel}</div>
          <div className="chatbi-welcome-desc">
            面向数据工程师、业务、运营与管理者的统一数据入口。用自然语言问数， 获取口径解读与可执行
            SQL，并可一键把结果沉淀为数据表格或可视化大屏。
          </div>
          <div className="chatbi-welcome-caps">
            <span className="chatbi-welcome-cap">自然语言问数</span>
            <span className="chatbi-welcome-cap">口径拆解·本体落地</span>
            <span className="chatbi-welcome-cap">生成数据表格</span>
            <span className="chatbi-welcome-cap">生成可视化大屏</span>
          </div>
          {loadingSuggestions ? (
            <Spin size="small" style={{ marginTop: 8 }} />
          ) : suggestions.length > 0 ? (
            <div className="chatbi-suggestions">
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  className="chatbi-suggestion-chip"
                  onClick={() => void onSuggestionClick(s)}
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
        messages.map((msg, idx) => {
          // 为 assistant 气泡回溯前一条 user 提问，供“生成数据应用”使用
          let precedingQuestion: string | undefined;
          if (msg.role === "assistant") {
            for (let i = idx - 1; i >= 0; i -= 1) {
              if (messages[i].role === "user") {
                precedingQuestion = messages[i].content;
                break;
              }
            }
          }
          return (
            <ChatBubble
              key={idx}
              message={msg}
              question={precedingQuestion}
              conversationId={activeConversationId ?? undefined}
              onGenerateApp={onGenerateApp}
              onAddToDashboard={onAddToDashboard}
              onClarify={onSuggestionClick}
              onProposeApp={onProposeApp}
            />
          );
        })
      )}
      {/*
        确认闭环总结（P2）：**整个对话只有一份，钉在末尾**。
        它是会话级的当前状态，不是某一条消息的属性——做成随消息落库的块，就得在
        「每轮都重复同一张图」和「靠时间戳猜有没有新决策」之间二选一，两条都不成立。
        数据来自 DecisionLedgerProvider，每次留痕写入后自动重取，故恒为最新。
      */}
      {activeConversationId && messages.length > 0 && <ConversationClosure />}
    </div>
  );
}

/** 会话闭环卡；一环未达则整块不渲染——随口一问不该顶着一张全灰的六环图。 */
function ConversationClosure() {
  const { closure } = useDecisionLedger();
  // 后三环都在这个抽屉里确认。闭环卡是**会话级**的，抽屉挂在它身上就与任何一条消息块的
  // 生命周期无关了——人关掉窗口、甚至刷新页面，任务行还在，点一下就重新进来。
  const drawer = useArtifactDrawer(undefined, closure?.conversation_id);
  if (!closure || closure.reached_count === 0) return null;
  const enterTask = (artifactId: string) => {
    void api
      .getArtifact(artifactId)
      .then(drawer.open)
      .catch((err) =>
        message.error(err instanceof Error ? err.message : "任务详情读取失败，请重试"),
      );
  };
  return (
    <>
      <ClosureCard closure={closure} onEnterTask={enterTask} />
      {drawer.node}
    </>
  );
}
