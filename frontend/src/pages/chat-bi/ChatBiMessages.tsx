import { ArrowUpOutlined, RobotOutlined } from "@ant-design/icons";
import { Spin } from "antd";
import type { RefObject } from "react";
import { ChatBubble } from "./ChatBiReferences";
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
    </div>
  );
}

