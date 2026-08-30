import { ArrowUpOutlined, RobotOutlined } from "@ant-design/icons";
import { Spin, message } from "antd";
import type { RefObject } from "react";
import { api } from "../../api";
import { ChatBubble, useArtifactDrawer } from "./ChatBiReferences";
import { ClosureCards } from "./ClosureCard";
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
          <div className="chatbi-welcome-kicker">
            <span className="chatbi-welcome-icon" aria-hidden>
              <RobotOutlined />
            </span>
            <span className="chatbi-welcome-kicker-label">DATA AGENT</span>
            <span className="chatbi-welcome-scope">{scopeLabel}</span>
          </div>
          <h1 className="chatbi-welcome-title">从数据问题开始</h1>
          <div className="chatbi-welcome-desc">
            用自然语言探索当前数据域，Data Agent 会整理口径、检索本体并给出可核验的结果。
          </div>
          {loadingSuggestions ? (
            <Spin size="small" style={{ marginTop: 8 }} />
          ) : suggestions.length > 0 ? (
            <div className="chatbi-suggestions" aria-label="推荐问题">
              <div className="chatbi-suggestions-label">推荐问题</div>
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  className="chatbi-suggestion-chip"
                  onClick={() => void onSuggestionClick(s)}
                  disabled={submitting}
                  type="button"
                >
                  <span>{s}</span>
                  <ArrowUpOutlined className="chatbi-suggestion-arrow" aria-hidden />
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
        确认闭环（P2）：**一条数据任务一张卡，钉在对话末尾**。
        它是任务的当前状态，不是某一条消息的属性——做成随消息落库的块，就得在
        「每轮都重复同一张图」和「靠时间戳猜有没有新决策」之间二选一，两条都不成立。
        数据来自 DecisionLedgerProvider，每次留痕写入后自动重取，故恒为最新。
      */}
      {activeConversationId && messages.length > 0 && <ConversationClosure />}
    </div>
  );
}

/**
 * 本会话建过的每条数据任务各一张闭环卡。
 *
 * **没建任务就一张都不画**：闭环是"要落一条数据任务"才谈得上的东西。此前只要账本里有
 * 任何一条记录就出卡，于是随口问一句数、在结果上点个「认可」，对话末尾就顶出一张
 * 「1/6 环已确认」——那次查询没有任何要闭的环，卡片只是在自说自话。
 */
function ConversationClosure() {
  const { closure } = useDecisionLedger();
  // 后三环都在这个抽屉里确认。抽屉挂在闭环卡这一层，就与任何一条消息块的生命周期
  // 无关了——人关掉窗口、甚至刷新页面，卡片还在，点一下就重新进来。
  const drawer = useArtifactDrawer(undefined, closure?.conversation_id);
  if (!closure?.tasks?.length) return null;
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
      <ClosureCards tasks={closure.tasks} onEnterTask={enterTask} />
      {drawer.node}
    </>
  );
}
