import { Tag } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import type {
  ChatBiCaliberItem,
  ChatBiCaliberKind,
  ChatBiCaliberReference,
} from "../../types";
import {
  splitInlineTokens,
  splitMarkdownBlocks,
  tokenizeSqlLine,
  type ChatMessage,
} from "./utils";

function MarkdownLite({ content }: { content: string }) {
  const blocks = splitMarkdownBlocks(content);
  let key = 0;
  return (
    <div className="chatbi-md">
      {blocks.map((block) => {
        if (block.type === "code") {
          return (
            <pre key={key++} className="chatbi-codeblock">
              <code>{block.code}</code>
            </pre>
          );
        }
        return <Line key={key++} raw={block.raw} />;
      })}
    </div>
  );
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
  const parts = splitInlineTokens(text);
  let key = 0;
  return (
    <>
      {parts.map((part) => {
        if (part.type === "bold") {
          return <strong key={key++}>{part.value}</strong>;
        }
        if (part.type === "code") {
          return (
            <code key={key++} className="chatbi-md-inline-code">
              {part.value}
            </code>
          );
        }
        return <span key={key++}>{part.value}</span>;
      })}
    </>
  );
}

function highlightSql(sql: string) {
  return sql.split("\n").map((line, idx) => (
    <div key={idx} className="chatbi-sql-line">
      {tokenizeSqlLine(line).map((tok, ti) => {
        if (tok.kind === "comment") {
          return (
            <span key={ti} className="chatbi-sql-comment">{tok.text}</span>
          );
        }
        if (tok.kind === "string") {
          return (
            <span key={ti} className="chatbi-sql-string">{tok.text}</span>
          );
        }
        if (tok.kind === "number") {
          return (
            <span key={ti} className="chatbi-sql-number">{tok.text}</span>
          );
        }
        if (tok.kind === "punct") {
          return (
            <span key={ti} className="chatbi-sql-punct">{tok.text}</span>
          );
        }
        if (tok.kind === "keyword") {
          return (
            <span key={ti} className="chatbi-sql-keyword">{tok.text}</span>
          );
        }
        return <span key={ti}>{tok.text}</span>;
      })}
    </div>
  ));
}

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

export function ChatBubble({ message }: { message: ChatMessage }) {
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
                  {item.references.map((reference, ri) => (
                    <CaliberRefChip key={ri} reference={reference} />
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

function CaliberRefChip({ reference }: { reference: ChatBiCaliberReference }) {
  const label = reference.display_name ?? reference.name ?? "—";
  const href = refToPath(reference);
  const kindLabel = CALIBER_KIND_LABEL[reference.kind] ?? reference.kind;
  const color = CALIBER_KIND_COLOR[reference.kind] ?? "default";
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
