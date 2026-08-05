import { Button, Space, Tag } from "antd";
import { AppstoreAddOutlined, AppstoreOutlined, DashboardOutlined, SafetyOutlined } from "@ant-design/icons";
import { useState } from "react";
import { Link } from "react-router-dom";
import type {
  ChatBiAgentStep,
  ChatBiCaliberItem,
  ChatBiCaliberKind,
  ChatBiCaliberReference,
  ChatBiDataResult,
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
        if (block.type === "table") {
          return <MarkdownTable key={key++} header={block.header} rows={block.rows} />;
        }
        return <Line key={key++} raw={block.raw} />;
      })}
    </div>
  );
}

function MarkdownTable({ header, rows }: { header: string[]; rows: string[][] }) {
  return (
    <div className="chatbi-md-tablewrap">
      <table className="chatbi-md-table">
        <thead>
          <tr>
            {header.map((cell, i) => (
              <th key={i}>
                <InlineRender text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td key={ci}>
                  <InlineRender text={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
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

export function ChatBubble({
  message,
  question,
  onGenerateApp,
  onAddToDashboard,
  onClarify,
}: {
  message: ChatMessage;
  question?: string;
  onGenerateApp?: (
    question: string,
    appType: "data_table" | "screen" | "dashboard",
    payload?: ChatMessage["payload"],
  ) => void;
  onAddToDashboard?: (question: string, payload?: ChatMessage["payload"]) => void;
  /** 点击澄清候选项 → 直接以该选项追问（P4.1）。 */
  onClarify?: (text: string) => void;
}) {
  const isUser = message.role === "user";
  const grounded =
    !isUser &&
    !message.payload?.grounding_refused &&
    Boolean(
      message.payload?.referenced_objects?.length ||
        message.payload?.caliber_decomposition?.length,
    );
  const canGenerate = grounded && Boolean(onGenerateApp && question);
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
            {!isUser && message.payload?.steps && message.payload.steps.length > 0 && (
              <StepTrace steps={message.payload.steps} />
            )}
            {!isUser && message.payload?.grounding_refused && (
              <div className="chatbi-notice chatbi-notice--warning">
                <SafetyOutlined className="chatbi-notice-icon" />
                <div className="chatbi-notice-body">
                  <span className="chatbi-notice-title">为避免不准确信息，已谨慎拒答</span>
                  <span className="chatbi-notice-desc">回答仅基于已发布本体可证实的内容；无法由本体证明的结论未作答。</span>
                </div>
              </div>
            )}
            {!isUser && message.payload?.clarification ? (
              // 澄清反问：正文已含问题与候选项，这里把候选项做成可点击的追问，
              // 让用户一步接上，而不是自己再打一遍。
              <div className="chatbi-clarify">
                <div className="chatbi-clarify-q">
                  {message.payload.clarification.question}
                </div>
                {message.payload.clarification.reason && (
                  <div className="chatbi-clarify-why">
                    {message.payload.clarification.reason}
                  </div>
                )}
                <div className="chatbi-clarify-options">
                  {message.payload.clarification.options.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      className="chatbi-clarify-option"
                      disabled={!onClarify}
                      onClick={() => onClarify?.(`${question ?? ""}（${opt}）`.trim())}
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="chatbi-answer-wrap">
                <MarkdownLite content={message.content} />
                {message.streaming && <span className="chatbi-answer-caret" />}
              </div>
            )}
            {message.payload?.caliber_decomposition &&
              message.payload.caliber_decomposition.length > 0 && (
                <CaliberDecomposition
                  items={message.payload.caliber_decomposition}
                />
              )}
            {message.payload?.suggested_sql && (
              <SqlBlock sql={message.payload.suggested_sql} />
            )}
            {!isUser && message.payload?.data_result &&
              message.payload.data_result.rows?.length > 0 && (
                <ResultTable result={message.payload.data_result} />
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
              <div className="chatbi-notice chatbi-notice--error">
                <span>回答出错，请重试。</span>
              </div>
            )}
            {canGenerate && (
              <div className="chatbi-generate-app" style={{ marginTop: 12 }}>
                <Space wrap>
                  <span style={{ color: "var(--om-text-tertiary)", fontSize: 13 }}>
                    <AppstoreOutlined /> 基于此口径（一个数据逻辑 = 一个面板）：
                  </span>
                  {onAddToDashboard && (
                    <Button
                      size="small"
                      type="primary"
                      icon={<AppstoreAddOutlined />}
                      onClick={() => onAddToDashboard(question!, message.payload)}
                    >
                      生成面板并加入看板
                    </Button>
                  )}
                  <Button
                    size="small"
                    icon={<DashboardOutlined />}
                    onClick={() => onGenerateApp!(question!, "dashboard", message.payload)}
                  >
                    生成新看板
                  </Button>
                </Space>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

const STEP_TOOL_META: Record<string, { icon: string; verb: string }> = {
  search_objects: { icon: "🔍", verb: "检索对象" },
  get_object: { icon: "📖", verb: "读取对象详情" },
  search_relations: { icon: "🔗", verb: "检索关系" },
  search_logics: { icon: "🧮", verb: "检索口径" },
  get_logic: { icon: "📐", verb: "读取口径详情" },
  get_domain_overview: { icon: "🗺️", verb: "获取数据域概览" },
  run_sql: { icon: "⚡", verb: "执行 SQL 查询" },
};

/** 把 (工具 + 入参) 翻译成一句人话动作 + 图标。 */
function stepAction(step: ChatBiAgentStep): { icon: string; text: string } {
  const meta = STEP_TOOL_META[step.tool] ?? { icon: "•", verb: step.tool };
  const args = step.arguments as Record<string, unknown> | undefined;
  if (step.tool === "run_sql") {
    const sql = String(args?.sql ?? "").replace(/\s+/g, " ").trim();
    return {
      icon: meta.icon,
      text: sql ? `${meta.verb}：${sql.slice(0, 48)}${sql.length > 48 ? "…" : ""}` : meta.verb,
    };
  }
  const kw = args?.keyword;
  const text = kw != null && String(kw) ? `${meta.verb}「${String(kw)}」` : meta.verb;
  return { icon: meta.icon, text };
}

function StepTrace({ steps }: { steps: ChatBiAgentStep[] }) {
  const running = steps.some((s) => s.status === "running");
  const hasFailed = steps.some((s) => s.status === "failed");
  const toolCount = steps.filter(
    (s) => s.kind !== "thought" && s.kind !== "repair",
  ).length;
  // 进行中/失败自动展开，其余默认折叠；用户手动点击后固定
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? (running || hasFailed);
  const headText = running
    ? `思考中 · 已执行 ${toolCount} 步`
    : `已执行 ${toolCount} 步工具编排${hasFailed ? " · 含失败" : ""}`;
  return (
    <div className="chatbi-steps">
      <button
        type="button"
        className="chatbi-steps-toggle"
        onClick={() => setManualOpen(!open)}
      >
        <span className={`chatbi-steps-caret${open ? " open" : ""}`}>▸</span>
        {running && <span className="chatbi-steps-spin" />}
        {headText}
      </button>
      {open && (
        <ol className="chatbi-steps-list">
          {steps.map((s) => {
            if (s.kind === "thought") {
              return (
                <li key={s.index} className="chatbi-step chatbi-step--thought">
                  <span className="chatbi-step-emoji">💭</span>
                  <span className="chatbi-step-thought">{s.text}</span>
                </li>
              );
            }
            if (s.kind === "repair") {
              return (
                <li key={s.index} className="chatbi-step chatbi-step--thought">
                  <span className="chatbi-step-emoji">🔁</span>
                  <span className="chatbi-step-thought">{s.text}</span>
                </li>
              );
            }
            const { icon, text } = stepAction(s);
            const status = s.status ?? "succeeded";
            return (
              <li key={s.index} className={`chatbi-step chatbi-step--${status}`}>
                <span className="chatbi-step-icon">
                  {status === "running" ? (
                    <span className="chatbi-step-spin" />
                  ) : status === "failed" ? (
                    "✗"
                  ) : (
                    "✓"
                  )}
                </span>
                <span className="chatbi-step-emoji">{icon}</span>
                <span className="chatbi-step-text">
                  {text}
                  {status === "running" ? "…" : ""}
                </span>
                {status !== "running" && s.summary && (
                  <span className="chatbi-step-summary">· {s.summary}</span>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function fmtCell(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function ResultTable({ result }: { result: ChatBiDataResult }) {
  const rows = result.rows ?? [];
  const cols =
    result.columns && result.columns.length
      ? result.columns
      : Object.keys(rows[0] ?? {}).map((k) => ({ key: k, title: k }));
  const keys = cols.map((c) => String(c.key ?? c.title ?? ""));
  const MAX = 50;
  const shown = rows.slice(0, MAX);
  return (
    <div className="chatbi-result">
      <div className="chatbi-result-title">
        查询结果 · {rows.length} 行{result.truncated ? "（已截断）" : ""}
      </div>
      <div className="chatbi-result-scroll">
        <table className="chatbi-result-table">
          <thead>
            <tr>
              {cols.map((c, i) => (
                <th key={i}>{String(c.title ?? c.key ?? "")}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, ri) => (
              <tr key={ri}>
                {keys.map((k, ci) => (
                  <td key={ci}>{fmtCell(row[k])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > shown.length && (
        <div className="chatbi-result-more">仅展示前 {shown.length} 行</div>
      )}
    </div>
  );
}

function CaliberDecomposition({
  items,
}: {
  items: ChatBiCaliberItem[];
}) {  return (
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
