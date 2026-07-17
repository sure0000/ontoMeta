import { SendOutlined } from "@ant-design/icons";
import { Spin } from "antd";
import type { DomainContext } from "../../types";

export interface ChatBiComposerProps {
  activeDomain: DomainContext;
  input: string;
  submitting: boolean;
  onInputChange: (value: string) => void;
  onSubmit: (question: string) => void;
}

export function ChatBiComposer({
  activeDomain,
  input,
  submitting,
  onInputChange,
  onSubmit,
}: ChatBiComposerProps) {
  return (
    <div className="chatbi-composer">
      <div className="chatbi-composer-inner">
        <textarea
          className="chatbi-input"
          placeholder={`向「${activeDomain.name}」提问… (Enter 发送，Shift+Enter 换行)`}
          value={input}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void onSubmit(input);
            }
          }}
          rows={1}
          disabled={submitting}
        />
        <button
          className={`chatbi-send-btn ${input.trim() && !submitting ? "chatbi-send-btn--active" : "chatbi-send-btn--disabled"}`}
          onClick={() => void onSubmit(input)}
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
  );
}
