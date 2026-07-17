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
        messages.map((msg, idx) => (
          <ChatBubble key={idx} message={msg} />
        ))
      )}
    </div>
  );
}
