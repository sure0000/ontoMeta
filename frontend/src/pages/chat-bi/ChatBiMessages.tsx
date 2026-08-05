import { RobotOutlined } from "@ant-design/icons";
import { Spin } from "antd";
import type { RefObject } from "react";
import type { DomainContext } from "../../types";
import { ChatBubble } from "./ChatBiReferences";
import type { ChatMessage } from "./utils";

export interface ChatBiMessagesProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  loadingMessages: boolean;
  messages: ChatMessage[];
  activeConversationId: string | null;
  activeDomain: DomainContext;
  loadingSuggestions: boolean;
  suggestions: string[];
  submitting: boolean;
  onSuggestionClick: (s: string) => void;
  onGenerateApp?: (
    question: string,
    appType: "data_table" | "screen" | "dashboard",
    payload?: import("../../types").ChatBiAnswer,
  ) => void;
  onAddToDashboard?: (
    question: string,
    payload?: import("../../types").ChatBiAnswer,
  ) => void;
}

export function ChatBiMessages({
  scrollRef,
  loadingMessages,
  messages,
  activeConversationId,
  activeDomain,
  loadingSuggestions,
  suggestions,
  submitting,
  onSuggestionClick,
  onGenerateApp,
  onAddToDashboard,
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
          <div className="chatbi-welcome-title">
            Data Agent · {activeDomain.name}
          </div>
          <div className="chatbi-welcome-desc">
            面向数据工程师、业务、运营与管理者的统一数据入口。用自然语言问数，
            获取口径解读与可执行 SQL，并可一键把结果沉淀为数据表格或可视化大屏。
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
              onGenerateApp={onGenerateApp}
              onAddToDashboard={onAddToDashboard}
              onClarify={onSuggestionClick}
            />
          );
        })
      )}
    </div>
  );
}
