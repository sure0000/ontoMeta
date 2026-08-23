import {
  AutoComplete,
  Button,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Radio,
  Select,
  Space,
  Steps,
  Switch,
  Tag,
  message,
} from "antd";
import {
  AppstoreAddOutlined,
  AppstoreOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  RobotOutlined,
  SafetyOutlined,
} from "@ant-design/icons";
import cronstrue from "cronstrue/i18n";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "../../api";
import { ArtifactDetail } from "../../components/AgentsPanel";
import { CronPicker } from "../../components/CronPicker";
import { DataSourcesModal } from "../../components/DataSourcesModal";
import { SpecForm } from "../../components/artifact-spec/SpecForm";
import { SPEC_FIELDS } from "../../components/artifact-spec/specFields";
import { useDecisionLedger } from "./DecisionLedger";
import {
  AckTarget,
  ackKey,
  formDefaults,
  recordAck,
  recordDecisionQuietly,
  toJsonSafe,
} from "./ledger";
import type {
  ChatBiAgentStep,
  ChatBiBlock,
  ChatBiCaliberItem,
  ChatBiCaliberKind,
  ChatBiCaliberReference,
  ChatBiClarification,
  ChatBiDataResult,
  ChatBiFormField,
  ChatBiFormRequest,
  ChatBiReference,
  GovernanceArtifact,
  GraphEdge,
  GraphNode,
  TaskPipeline,
} from "../../types";
import {
  answerToBlocks,
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
        if (block.type === "hr") {
          return <hr key={key++} className="chatbi-md-hr" />;
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

  // 兜底：未被解析器识别的裸 `---`/`***`/`___`（例如流式中途尚未成行）当作分隔线。
  if (/^(?:-{3,}|\*{3,}|_{3,})[\s-*_]*$/.test(raw.trim()) && !raw.includes("|")) {
    return <hr className="chatbi-md-hr" />;
  }
  if (raw.trim().startsWith(">")) {
    return (
      <blockquote className="chatbi-md-quote">
        <InlineRender text={raw.replace(/^\s*>\s?/, "")} />
      </blockquote>
    );
  }
  // 缩进量→左移：支持嵌套列表/引用块在视觉上分层。
  const indent = /^\s+/.exec(raw)?.[0].replace(/\t/g, "  ").length ?? 0;
  const indentPx = Math.min(indent, 24) * 7;
  const orderedMatch = raw.match(/^\s*(\d+)\.\s+(.*)$/);
  if (orderedMatch) {
    return (
      <div
        className="chatbi-md-listitem chatbi-md-listitem--ordered"
        style={{ marginLeft: indentPx }}
      >
        <span className="chatbi-md-num">{orderedMatch[1]}</span>
        <span>
          <InlineRender text={orderedMatch[2]} />
        </span>
      </div>
    );
  }
  const listMatch = raw.match(/^\s*[-*]\s+(.*)$/);
  if (listMatch) {
    return (
      <div className="chatbi-md-listitem" style={{ marginLeft: indentPx }}>
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
        if (part.type === "link") {
          return (
            <a
              key={key++}
              className="chatbi-md-link"
              href={part.href}
              target="_blank"
              rel="noopener noreferrer"
            >
              {part.value}
            </a>
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
            <span key={ti} className="chatbi-sql-comment">
              {tok.text}
            </span>
          );
        }
        if (tok.kind === "string") {
          return (
            <span key={ti} className="chatbi-sql-string">
              {tok.text}
            </span>
          );
        }
        if (tok.kind === "number") {
          return (
            <span key={ti} className="chatbi-sql-number">
              {tok.text}
            </span>
          );
        }
        if (tok.kind === "punct") {
          return (
            <span key={ti} className="chatbi-sql-punct">
              {tok.text}
            </span>
          );
        }
        if (tok.kind === "keyword") {
          return (
            <span key={ti} className="chatbi-sql-keyword">
              {tok.text}
            </span>
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

/**
 * 出现这些块就不挂「生成面板」动作条：终态出口（等用户回填，本轮没有结论）与写侧提案/状态
 * （建数任务车道，与看板无关）。此前判据只看接没接地，于是一张物化表单底下也会挂出
 * 「基于此口径生成面板」——它为找物化对象调过 get_object，一样算接地。
 */
const NON_ANALYTIC_BLOCKS = new Set<ChatBiBlock["type"]>([
  "clarify",
  "form",
  "action_proposal",
  "draft_proposal",
  "preference_proposal",
  "task_status",
  // 接数据车道：与看板无关。
  "onboard_proposal",
  // agent 已经主动提了面板/看板 → 底下再挂一条「基于此口径生成面板」是同一件事说两遍。
  "app_proposal",
]);

export function ChatBubble({
  message,
  question,
  conversationId,
  onGenerateApp,
  onAddToDashboard,
  onClarify,
  onProposeApp,
}: {
  message: ChatMessage;
  question?: string;
  conversationId?: string;
  onGenerateApp?: (
    question: string,
    appType: "data_table" | "screen" | "dashboard",
    payload?: ChatMessage["payload"],
  ) => void;
  onAddToDashboard?: (question: string, payload?: ChatMessage["payload"]) => void;
  /** 点击澄清候选项 → 直接以该选项追问（P4.1）。 */
  onClarify?: (text: string) => void;
  /**
   * agent 主动提的面板/看板提案被点确认。与动作条同一条生成路径，只是标题/图型来自提案；
   * 口径仍取本条消息的 payload（在这里绑上，卡片不必自己拿 payload）。
   */
  onProposeApp?: (
    proposal: Extract<ChatBiBlock, { type: "app_proposal" }>["proposal"],
    payload?: ChatMessage["payload"],
  ) => void;
}) {
  const isUser = message.role === "user";
  // V3 S0：回答由渲染块序列投影而来。后端双写 blocks 优先；流式/旧消息由 answerToBlocks
  // 从扁平字段兜底（流式时正文取实时 content）。用户气泡仍是纯文本。
  const blocks: ChatBiBlock[] = isUser
    ? []
    : message.payload?.blocks?.length
      ? message.payload.blocks
      : answerToBlocks(message.payload, message.content);
  // 「生成面板」的判据是**这条回答自己产出了口径或数据**，不是「查过本体」。动作条生成的
  // 图表由 caliber_decomposition + referenced_objects 服务端重建（见 ChatBiPage 的
  // generateWidgetFromChat），没有口径展开也没有数据行时，生成出来的只会是个空壳。
  const analytic =
    !isUser &&
    !message.payload?.grounding_refused &&
    ((message.payload?.caliber_decomposition?.length ?? 0) > 0 ||
      (message.payload?.data_result?.rows?.length ?? 0) > 0);
  const canGenerate =
    analytic &&
    !blocks.some((b) => NON_ANALYTIC_BLOCKS.has(b.type)) &&
    Boolean(onGenerateApp && question);
  return (
    <div className={`chatbi-bubble chatbi-bubble--${isUser ? "user" : "assistant"}`}>
      {!isUser && (
        <div className="chatbi-bubble-avatar" aria-hidden>
          <RobotOutlined />
        </div>
      )}
      <div className="chatbi-bubble-body">
        {message.pending ? (
          <div className="chatbi-bubble-pending">
            <div className="chatbi-typing-dots">
              <span />
              <span />
              <span />
            </div>
            <span style={{ color: "var(--om-text-tertiary)", fontSize: 13 }}>
              正在结合本体知识思考…
            </span>
          </div>
        ) : isUser ? (
          <div className="chatbi-answer-wrap">
            <MarkdownLite content={message.content} />
          </div>
        ) : (
          <>
            {blocks.map((block, idx) => (
              <div
                key={block.id}
                className="chatbi-block-in"
                style={{ animationDelay: `${Math.min(idx, 6) * 55}ms` }}
              >
                <BlockRenderer
                  block={block}
                  streaming={message.streaming}
                  question={question}
                  conversationId={conversationId}
                  messageId={message.id}
                  ontologyId={message.payload?.ontology_id}
                  onClarify={onClarify}
                  onProposeApp={
                    onProposeApp
                      ? (proposal) => onProposeApp(proposal, message.payload)
                      : undefined
                  }
                />
              </div>
            ))}
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

/**
 * 渲染块注册表（V3 S0）：按 `block.type` 分派到对应组件，替代改造前写死的 && 阶梯。
 * 未知块类型（S1 的 chart / lineage / draft_proposal）在本期由 default 优雅跳过。
 */
function BlockRenderer({
  block,
  streaming,
  question,
  conversationId,
  messageId,
  ontologyId,
  onClarify,
  onProposeApp,
}: {
  block: ChatBiBlock;
  streaming?: boolean;
  question?: string;
  conversationId?: string;
  /** 决策留痕的消息锚。流式刚产出的消息尚未落库、拿不到 id，故可空。 */
  messageId?: string;
  ontologyId?: string | null;
  onClarify?: (text: string) => void;
  onProposeApp?: (proposal: Extract<ChatBiBlock, { type: "app_proposal" }>["proposal"]) => void;
}) {
  switch (block.type) {
    case "steps":
      return <StepTrace steps={block.steps} />;
    case "markdown":
      return <MarkdownBlock content={block.content} streaming={streaming} />;
    case "mapping":
      return (
        <MappingBlock
          variant={block.variant}
          items={block.items}
          references={block.references}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "sql":
      return (
        <SqlBlock
          sql={block.sql}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "table":
      return (
        <ResultTable
          result={{
            columns: block.columns,
            rows: block.rows,
            truncated: block.truncated,
          }}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "chart":
      return <ChatBiChart spec={block.spec} columns={block.columns} rows={block.rows} />;
    case "insight":
      return <InsightBlock analysis={block.analysis} />;
    case "lineage":
      return (
        <ChatBiLineage
          centerId={block.center_id ?? undefined}
          nodes={block.nodes}
          edges={block.edges}
          truncated={block.truncated}
        />
      );
    case "refs":
      return <RefsRow objects={block.objects} logics={block.logics} />;
    case "plan":
      return <PlanBlock steps={block.steps} note={block.note} />;
    case "draft_proposal":
      return (
        <DraftProposalBlock
          proposal={block.proposal}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "action_proposal":
      return (
        <ActionProposalBlock
          proposal={block.proposal}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "pipeline_proposal":
      return <PipelineProposalBlock proposal={block.proposal} conversationId={conversationId} />;
    case "preference_proposal":
      return (
        <PreferenceProposalBlock
          proposal={block.proposal}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "app_proposal":
      return <AppProposalBlock proposal={block.proposal} onProposeApp={onProposeApp} />;
    case "onboard_proposal":
      return (
        <OnboardProposalBlock
          proposal={block.proposal}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "task_status":
      return (
        <TaskStatusBlock
          status={block.status}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "notice":
      return block.variant === "refused" ? <RefusedNotice /> : <MockNotice />;
    case "clarify":
      return (
        <ClarifyBlock
          clarification={block.clarification}
          question={question}
          onClarify={onClarify}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
        />
      );
    case "form":
      // P6：Agent 动态生成的可填写表单。提交后把结构化回填文本经澄清通道作为新一轮问题带回，
      // 同时把结构化原值单独留痕（散文里没有机器可解析的字段/取值对）。
      return (
        <FormBlock
          form={block.form}
          onSubmit={onClarify}
          conversationId={conversationId}
          messageId={messageId}
          blockId={block.id}
          ontologyId={ontologyId}
        />
      );
    default:
      return null;
  }
}

function MarkdownBlock({ content, streaming }: { content: string; streaming?: boolean }) {
  return (
    <div className="chatbi-answer-wrap">
      <MarkdownLite content={content} />
      {streaming && <span className="chatbi-answer-caret" />}
    </div>
  );
}

function RefusedNotice() {
  return (
    <div className="chatbi-notice chatbi-notice--warning">
      <SafetyOutlined className="chatbi-notice-icon" />
      <div className="chatbi-notice-body">
        <span className="chatbi-notice-title">为避免不准确信息，已谨慎拒答</span>
        <span className="chatbi-notice-desc">
          回答仅基于已发布本体可证实的内容；无法由本体证明的结论未作答。
        </span>
      </div>
    </div>
  );
}

function MockNotice() {
  return (
    <div className="chatbi-mock-hint">
      <Tag color="warning" style={{ borderRadius: 6 }}>
        Mock 模式
      </Tag>
      <span>未接入真实 LLM，已使用规则匹配回答。</span>
    </div>
  );
}

/**
 * 「认可 / 存疑」轻量表态条（P1）。
 *
 * 挂在本体映射 / 数据结果 / 任务回执三类**纯展示**块上，把「人看过并认了」这件事
 * 从推断变成记录。刻意**不做闸门**：不点不拦，答案照看照用。
 *
 * 无 `conversationId` 时（历史消息渲染、导出预览）整条不渲染——留痕无处可去，
 * 摆一对点不动的按钮只会让人以为坏了。
 *
 * 表态可改判：已认可的再点存疑会追加一条新记录、闭环取最新（账本追加式，不改写）。
 * 重复点同一个结论则整条挡掉——那不是决策，只是手抖。
 */
function AckControl({ target, label }: { target: AckTarget; label: string }) {
  const { ackOf, notifyWritten } = useDecisionLedger();
  const [local, setLocal] = useState<"accepted" | "rejected" | null>(null);
  const key = ackKey(target.messageId, target.node, target.stage, target.blockId);
  // 本地态优先：刚点完的这一下要立刻见效，不能等 closure 重取回来才亮。
  const picked = local ?? ackOf(key) ?? null;
  if (!target.conversationId) return null;
  const choose = (accepted: boolean) => {
    const next = accepted ? "accepted" : "rejected";
    if (picked === next) return;
    setLocal(next);
    recordAck(target, accepted);
    notifyWritten();
  };
  return (
    <div className="chatbi-ack">
      <span className="chatbi-ack-label">{picked ? "已留痕" : label}</span>
      <button
        type="button"
        className={`chatbi-ack-btn${picked === "accepted" ? " chatbi-ack-btn--on" : ""}`}
        onClick={() => choose(true)}
      >
        认可
      </button>
      <button
        type="button"
        className={`chatbi-ack-btn${picked === "rejected" ? " chatbi-ack-btn--off" : ""}`}
        onClick={() => choose(false)}
      >
        存疑
      </button>
    </div>
  );
}

function ClarifyBlock({
  clarification,
  question,
  onClarify,
  conversationId,
  messageId,
  blockId,
}: {
  clarification: ChatBiClarification;
  question?: string;
  onClarify?: (text: string) => void;
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  // 澄清反问：正文即问题与候选项，这里把候选项做成可点击的追问，
  // 让用户一步接上，而不是自己再打一遍。
  const onPick = (opt: string) => {
    onClarify?.(`${question ?? ""}（${opt}）`.trim());
    // 留痕：用户选了哪个候选项，是"需求确认"最直接的证据。
    // 同一块换选项算两次决策（那是真实的改主意，应各记一条），故 dedup_key 带选项。
    recordDecisionQuietly(conversationId, {
      node: "requirement",
      stage: "clarify",
      trigger: "clarify_option",
      message_id: messageId,
      block_id: blockId,
      summary: `澄清「${clarification.question}」→ 选择「${opt}」`,
      proposed: { options: clarification.options },
      chosen: { option: opt },
      dedup_key: blockId
        ? `${conversationId}:requirement:clarify:${blockId}:${opt}`
        : undefined,
    });
  };
  return (
    <div className="chatbi-clarify">
      <div className="chatbi-clarify-q">{clarification.question}</div>
      {clarification.reason && <div className="chatbi-clarify-why">{clarification.reason}</div>}
      <div className="chatbi-clarify-options">
        {clarification.options.map((opt) => (
          <button
            key={opt}
            type="button"
            className="chatbi-clarify-option"
            disabled={!onClarify}
            onClick={() => onPick(opt)}
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 把 date 控件的 dayjs 值格式化，其余原样返回（避免为一个 format 引入 dayjs 依赖）。 */
function formatFormValue(v: unknown): string {
  if (v === undefined || v === null || v === "") return "（未填写）";
  if (Array.isArray(v)) return v.length ? v.join("、") : "（未填写）";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v === "object" && typeof (v as { format?: unknown }).format === "function") {
    return (v as { format: (f: string) => string }).format("YYYY-MM-DD");
  }
  return String(v);
}

/** cron 表达式 → 中文描述；空 = 不定时，解析不了则原样回显（与 CronPicker 同口径）。 */
function describeCronValue(expr: string): string {
  if (!expr.trim()) return "不定时（仅手动触发）";
  try {
    return cronstrue.toString(expr, {
      locale: "zh_CN",
      use24HourTimeFormat: true,
      throwExceptionOnParseError: true,
    });
  } catch {
    return expr;
  }
}

/**
 * 一个字段的回填文本。
 *
 * 候选类字段回「名称（取值）」：**名称给人读、取值给模型用**。id 类候选（数据源、对象）
 * 只回名称的话，模型下一轮还得再猜是哪条记录；只回 id 则这条消息在对话里没法读。
 * 自由输入类（text/autocomplete/number）没有这层映射，原样回即可。
 */
function formatFieldValue(field: ChatBiFormField, raw: unknown): string {
  if (field.type === "cron") {
    const expr = typeof raw === "string" ? raw.trim() : "";
    return expr ? `${describeCronValue(expr)}（${expr}）` : "不定时（仅手动触发）";
  }
  const decorate = (v: unknown): string => {
    const text = String(v);
    const hit = (field.options ?? []).find((o) => o.value === text);
    return hit && hit.label !== hit.value ? `${hit.label}（${hit.value}）` : text;
  };
  if (Array.isArray(raw)) {
    return raw.length ? raw.map(decorate).join("、") : "（未填写）";
  }
  if (raw === undefined || raw === null || raw === "") return "（未填写）";
  if (typeof raw === "boolean" || typeof raw === "object") return formatFormValue(raw);
  return decorate(raw);
}

/** 把填好的表单值拼成既可读、又便于 LLM 解析的结构化回填文本。 */
function composeFormReply(form: ChatBiFormRequest, values: Record<string, unknown>): string {
  const lines = form.fields.map((f) => `- ${f.label}：${formatFieldValue(f, values[f.name])}`);
  if (form.confirmation_id) {
    lines.unshift(`- task_confirmation_id：${form.confirmation_id}`);
  }
  return `【已填写：${form.title}】\n${lines.join("\n")}`;
}

/**
 * 单个字段的控件。
 *
 * **`...rest` 必须透传**：Form.Item 是把 value/onChange 注入到它的**直接子节点**上的，
 * 而这里的直接子节点是本组件，不是里面的 Input。不透传就等于把注入的 onChange 吃掉——
 * 界面上照常能打字（DOM 自己的值），但 Form 的 store 一直是空的，提交时每个必填项都报
 * 「请填写…」。整张交互表单因此从未真正提交成功过。
 */
function FormControl({ field, ...rest }: { field: ChatBiFormField } & Record<string, unknown>) {
  const options = field.options ?? [];
  switch (field.type) {
    case "textarea":
      return (
        <Input.TextArea
          {...rest}
          placeholder={field.placeholder}
          autoSize={{ minRows: 2, maxRows: 6 }}
        />
      );
    case "number":
      return <InputNumber {...rest} placeholder={field.placeholder} style={{ width: "100%" }} />;
    case "select":
      // 「某某数据源 → 某某库」这类合并候选可能上百条，恒开搜索；搜的是 label（给人看的
      // 那串），不是藏着 id 的 value。
      return (
        <Select
          {...rest}
          placeholder={field.placeholder}
          options={options}
          showSearch
          optionFilterProp="label"
          allowClear
        />
      );
    case "multiselect":
      return (
        <Select
          {...rest}
          mode="multiple"
          placeholder={field.placeholder}
          options={options}
          optionFilterProp="label"
          allowClear
        />
      );
    case "radio":
      return <Radio.Group {...rest} options={options} />;
    case "boolean":
      return <Switch {...rest} />;
    case "date":
      return <DatePicker {...rest} style={{ width: "100%" }} placeholder={field.placeholder} />;
    case "autocomplete":
      // 候选是**建议**不是闭集（分区键：物理表上可能有本体没建模的列），故是能打字的输入框。
      return (
        <AutoComplete
          {...rest}
          options={options}
          placeholder={field.placeholder}
          filterOption={(input, option) =>
            String(option?.label ?? "")
              .toLowerCase()
              .includes(input.toLowerCase())
          }
        />
      );
    case "cron":
      // 与业务对象详情里点「物化」弹出的那个「定时策略」是同一个控件：任意合法 cron，
      // 不是几个预置项——弹窗里配得出来的频率，对话里也要配得出来。
      return <CronPickerControl {...rest} />;
    default:
      return <Input {...rest} placeholder={field.placeholder} />;
  }
}

/** CronPicker 的受控适配：Form.Item 注入的是 value/onChange，而 CronPicker 要求 value 非空串。 */
function CronPickerControl({
  value,
  onChange,
  disabled,
}: {
  value?: unknown;
  onChange?: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <CronPicker
      value={typeof value === "string" ? value : ""}
      onChange={(v) => onChange?.(v)}
      disabled={disabled}
    />
  );
}

/**
 * 交互表单块（P6）。普通分析表单提交后继续对话；数据任务三步确认完成后直接创建草稿并
 * 生成 dry-run 执行方案，不再把表单文本交给 LLM 二次解释。
 */
function FormBlock({
  form,
  onSubmit,
  conversationId,
  messageId,
  blockId,
  ontologyId,
}: {
  form: ChatBiFormRequest;
  onSubmit?: (text: string) => void;
  conversationId?: string;
  messageId?: string;
  blockId?: string;
  ontologyId?: string | null;
}) {
  const [antForm] = Form.useForm();
  const [submitted, setSubmitted] = useState(false);
  const [submittingTask, setSubmittingTask] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const { notifyWritten } = useDecisionLedger();
  const confirmationSteps = form.confirmation_steps ?? [];
  const staged = confirmationSteps.length > 0;
  const inferredTaskKind = form.fields.some((field) => field.name === "object_type")
    ? "sync"
    : form.fields.some((field) => field.name === "business_logic_id")
      ? "metric"
      : form.fields.some((field) => field.name === "cleansing_rules")
        ? "transform"
        : form.fields.some((field) => field.name === "selected_targets")
          ? "materialize"
          : undefined;
  const taskKind = form.task_kind ?? inferredTaskKind;
  const effectiveOntologyId = form.ontology_id ?? ontologyId ?? undefined;
  const deterministicTaskSubmit = Boolean(
    staged && form.confirmation_id && taskKind && effectiveOntologyId && conversationId,
  );
  const { open: openArtifact, node: artifactDrawer } = useArtifactDrawer(
    undefined,
    conversationId,
    messageId,
    blockId,
  );
  const activeStep = confirmationSteps[currentStep];
  const disabled = submitted || submittingTask || (!onSubmit && !deterministicTaskSubmit);
  const initialValues = useMemo(() => {
    const iv: Record<string, unknown> = {};
    for (const f of form.fields) {
      if (f.default === undefined || f.default === null) continue;
      // date 的默认值经 JSON 过来是字符串，而 DatePicker 要 dayjs——宁可空着让用户自己选，
      // 也不能把一个它读不懂的值塞进去。
      if (f.type === "date") continue;
      iv[f.name] = f.default;
    }
    return iv;
  }, [form]);
  const fieldsOfStep = (node: string) =>
    form.fields.filter(
      (f) => f.confirmation_node === node || (node === "plan" && !f.confirmation_node),
    );
  const visibleFields = staged ? fieldsOfStep(activeStep?.node ?? "") : form.fields;
  const [liveValues, setLiveValues] = useState<Record<string, unknown>>(initialValues);
  // DataSource 是可变设置，不能永久使用消息生成时的静态 options 快照。尤其是用户先收到
  // 空表单、再去设置页配置默认 Doris 后，返回历史消息时应立即看到新目标，无需重开对话。
  const [runtimeOptions, setRuntimeOptions] = useState<Record<string, ChatBiFormField["options"]>>({});
  const [runtimeHelp, setRuntimeHelp] = useState<Record<string, string>>({});
  useEffect(() => {
    const targetField = form.fields.find((field) => field.name === "target_datasource_id");
    if (!targetField) return;
    let cancelled = false;
    api
      .listDataSources()
      .then((sources) => {
        if (cancelled) return;
        const targets = sources
          .filter(
            (source) =>
              source.purpose === "warehouse" &&
              source.kind === "doris" &&
              source.is_default_warehouse === true &&
              source.enabled !== false &&
              source.dsn_set === true,
          )
          .map((source) => ({
            label: `${source.name}（默认 Doris）`,
            value: source.id,
          }));
        setRuntimeOptions((prev) => ({ ...prev, target_datasource_id: targets }));
        setRuntimeHelp((prev) => ({
          ...prev,
          target_datasource_id: targets.length
            ? "候选已按当前设置实时刷新；同步、加工、聚合和物化使用默认 Doris"
            : "当前设置中没有启用且已配置连接的默认 Doris",
        }));
        const current = antForm.getFieldValue("target_datasource_id");
        if (targets.length === 1 && !current) {
          antForm.setFieldValue("target_datasource_id", targets[0].value);
          setLiveValues((prev) => ({ ...prev, target_datasource_id: targets[0].value }));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRuntimeHelp((prev) => ({
            ...prev,
            target_datasource_id: "目标数仓候选刷新失败，请检查设置或稍后重试",
          }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [form.fields, antForm]);
  const resolvedField = (field: ChatBiFormField): ChatBiFormField => {
    const liveOptions = runtimeOptions[field.name];
    if (liveOptions) {
      return {
        ...field,
        type: field.name === "target_datasource_id" ? "select" : field.type,
        options: liveOptions,
        help: runtimeHelp[field.name] ?? field.help,
      };
    }
    if (!field.depends_on || !field.options_by_value) {
      return { ...field, help: runtimeHelp[field.name] ?? field.help };
    }
    const upstream = String(liveValues[field.depends_on] ?? "");
    return {
      ...field,
      options: field.options_by_value[upstream] ?? [],
      help: runtimeHelp[field.name] ?? field.help,
    };
  };
  const handleValuesChange = (
    changed: Record<string, unknown>,
    all: Record<string, unknown>,
  ) => {
    const next = { ...all };
    for (const field of form.fields) {
      if (!field.depends_on || !(field.depends_on in changed)) continue;
      const upstream = String(all[field.depends_on] ?? "");
      const options = field.options_by_value?.[upstream] ?? [];
      const current = all[field.name];
      const stillValid = options.some((option) => option.value === current);
      const replacement = stillValid ? current : options.length === 1 ? options[0].value : undefined;
      antForm.setFieldValue(field.name, replacement);
      next[field.name] = replacement;
    }
    setLiveValues(next);
  };
  const handleFinish = async (values: Record<string, unknown>) => {
    if (deterministicTaskSubmit) {
      setSubmittingTask(true);
      try {
        const context = { ...values };
        const intent = String(
          context.task_requirement ?? context.sync_requirement ?? form.intent ?? form.title,
        ).trim();
        delete context.task_requirement;
        delete context.sync_requirement;
        const artifact = await api.draftConfirmedArtifact({
          conversation_id: conversationId!,
          confirmation_id: form.confirmation_id!,
          kind: taskKind!,
          intent,
          context: toJsonSafe(context) as Record<string, unknown>,
          ontology_id: effectiveOntologyId!,
          message_id: messageId,
          block_id: blockId,
        });
        setSubmitted(true);
        openArtifact(artifact);
        notifyWritten();
        if (artifact.status === "validated") {
          message.success("任务草稿已创建，执行方案已生成，请确认后再执行");
        } else {
          message.warning("任务草稿已创建，执行方案存在阻断项，请查看详情");
        }
      } catch (err) {
        message.error(err instanceof Error ? err.message : "创建任务草稿失败，请重试");
      } finally {
        setSubmittingTask(false);
      }
      return;
    }

    setSubmitted(true);
    onSubmit?.(composeFormReply(form, values));
    if (staged) return;
    // 通用单页表单仍按原逻辑记为需求确认。
    recordDecisionQuietly(conversationId, {
      node: "requirement",
      stage: "form",
      trigger: "form_submit",
      message_id: messageId,
      block_id: blockId,
      summary: `填写了表单「${form.title}」`,
      proposed: formDefaults(form.fields),
      chosen: toJsonSafe(values),
      dedup_key: blockId
        ? `${conversationId}:requirement:form:${messageId ?? ""}:${blockId}`
        : undefined,
    });
    notifyWritten();
  };
  const confirmCurrentStep = async () => {
    if (!activeStep) return;
    const stepFields = fieldsOfStep(activeStep.node);
    const names = stepFields.map((f) => f.name);
    try {
      if (names.length) await antForm.validateFields(names);
    } catch {
      return;
    }
    const allValues = antForm.getFieldsValue(true) as Record<string, unknown>;
    const chosen = {
      ...(names.length
        ? Object.fromEntries(names.map((name) => [name, allValues[name]]))
        : { intent: form.intent ?? form.title }),
      ...(form.confirmation_id ? { task_confirmation_id: form.confirmation_id } : {}),
    };
    const proposed = names.length
      ? Object.fromEntries(
          stepFields
            .filter((f) => f.default !== undefined && f.default !== null)
            .map((f) => [f.name, f.default]),
        )
      : { intent: form.intent ?? form.title };
    const decision = {
      node: activeStep.node,
      stage: `task_${activeStep.node}_confirm`,
      trigger: "step_confirm",
      message_id: messageId,
      block_id: blockId,
      summary: `${activeStep.title}：${form.intent ?? form.title}`,
      proposed: toJsonSafe(proposed),
      chosen: toJsonSafe(chosen),
    };
    // 最后一步确认后会立刻把表单作为下一轮消息提交。这里必须等账本提交完成，
    // 否则 propose_action 的服务端闭环门禁可能先到，看不到刚确认的数据环。
    if (conversationId) {
      const saved = await api.recordChatBiDecision(conversationId, decision).catch(() => null);
      if (!saved?.recorded) {
        message.error("确认记录未保存，请重试；为避免跳过人审，本步不会继续");
        return;
      }
    }
    notifyWritten();
    if (currentStep < confirmationSteps.length - 1) {
      setCurrentStep((i) => i + 1);
    } else {
      antForm.submit();
    }
  };
  return (
    <div className="chatbi-form">
      <div className="chatbi-form-title">{form.title}</div>
      {form.intent && <div className="chatbi-form-intent">{form.intent}</div>}
      {staged && (
        <Steps
          size="small"
          current={currentStep}
          items={confirmationSteps.map((step) => ({ title: step.title }))}
          style={{ marginBottom: 16 }}
        />
      )}
      {staged && activeStep?.description && (
        <div className="chatbi-form-intent">{activeStep.description}</div>
      )}
      <Form
        form={antForm}
        layout="vertical"
        size="small"
        disabled={disabled}
        initialValues={initialValues}
        onFinish={handleFinish}
        onValuesChange={handleValuesChange}
        className="chatbi-form-body"
        requiredMark="optional"
      >
        {visibleFields.map((rawField) => {
          const f = resolvedField(rawField);
          return (
            <Form.Item
              key={f.name}
              name={f.name}
              label={f.label}
              extra={f.help}
              valuePropName={f.type === "boolean" ? "checked" : undefined}
              rules={f.required ? [{ required: true, message: `请填写${f.label}` }] : undefined}
            >
              <FormControl field={f} />
            </Form.Item>
          );
        })}
        {staged && activeStep?.node === "requirement" && visibleFields.length === 0 && form.intent && (
          <div className="chatbi-proposal-param-ro">{form.intent}</div>
        )}
        <div className="chatbi-form-actions">
          {staged && currentStep > 0 && (
            <Button size="small" disabled={disabled} onClick={() => setCurrentStep((i) => i - 1)}>
              上一步
            </Button>
          )}
          <Button
            type="primary"
            size="small"
            htmlType={staged ? "button" : "submit"}
            disabled={disabled}
            loading={submittingTask}
            onClick={staged ? () => void confirmCurrentStep() : undefined}
          >
            {submitted
              ? "已提交"
              : staged
                ? currentStep === confirmationSteps.length - 1
                  ? deterministicTaskSubmit
                    ? "确认数据并生成执行方案"
                    : "确认并提交"
                  : `确认${activeStep?.title.replace(/^确认/, "") ?? "本步"}`
                : form.submit_label || "提交"}
          </Button>
        </div>
      </Form>
      {artifactDrawer}
    </div>
  );
}

function RefsRow({ objects, logics }: { objects: ChatBiReference[]; logics: ChatBiReference[] }) {
  return (
    <div className="chatbi-refs">
      {objects.map((r, i) => (
        <Tag key={`o-${i}`} color="blue" style={{ borderRadius: 6 }}>
          对象：{r.display_name ?? r.name ?? "—"}
        </Tag>
      ))}
      {logics.map((r, i) => (
        <Tag key={`l-${i}`} color="purple" style={{ borderRadius: 6 }}>
          逻辑：{r.display_name ?? r.name ?? "—"}
        </Tag>
      ))}
    </div>
  );
}

const DRAFT_TYPE_LABEL: Record<string, string> = { metric: "指标", tag: "标签", rule: "规则" };

/**
 * 建数提案块（V3 S3）：Data Agent 只出提案（不写库）；点「去确认创建」才由用户动作
 * POST /api/business-logics 建一条**草稿口径**（SUGGESTED），随后跳转到口径详情让用户
 * 补全表达式并走发布。写侧仍是既有 draft→confirm→execute 治理流程，agent 不碰。
 */
function DraftProposalBlock({
  proposal,
  conversationId,
  messageId,
  blockId,
}: {
  proposal: Extract<ChatBiBlock, { type: "draft_proposal" }>["proposal"];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  const navigate = useNavigate();
  const [state, setState] = useState<"idle" | "creating" | "done" | "error">("idle");
  const typeLabel = DRAFT_TYPE_LABEL[proposal.logic_type] ?? proposal.logic_type;
  // 带表达式的提案（propose_expression）：表达式已过编译器与语义证明，人审的是**真 SQL**。
  const formalized = Boolean(proposal.compiled_sql);
  const patching = Boolean(proposal.logic_id && proposal.update_payload);
  // 留痕：此前这个确认完全绕开会话——点完就 navigate 走人，会话里看不出用户点没点。
  const recordOntologyDecision = (logicId: string) =>
    recordDecisionQuietly(conversationId, {
      node: "ontology",
      stage: "draft_proposal",
      trigger: patching ? "logic_updated" : "logic_created",
      message_id: messageId,
      block_id: blockId,
      summary: `${patching ? "补全" : "新建"}${typeLabel}「${proposal.name ?? ""}」`,
      proposed: (proposal.update_payload ?? proposal.create_payload) as unknown,
      ref_kind: "business_logic",
      ref_id: logicId,
      dedup_key: `${conversationId}:ontology:draft:${logicId}`,
    });
  const onConfirm = async () => {
    setState("creating");
    try {
      if (patching) {
        await api.updateBusinessLogic(proposal.logic_id!, proposal.update_payload!);
        setState("done");
        recordOntologyDecision(proposal.logic_id!);
        navigate(`/business-logic/${proposal.logic_id}`);
        return;
      }
      const created = await api.createBusinessLogic(proposal.create_payload!);
      setState("done");
      recordOntologyDecision(created.id);
      navigate(`/business-logic/${created.id}`);
    } catch {
      setState("error");
    }
  };
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="gold" bordered={false}>
          建数提案
        </Tag>
        <span>
          {patching ? `补全${typeLabel}表达式` : `新建${typeLabel}`}
        </span>
        {formalized && (
          <Tag color="green" bordered={false}>
            表达式已编译通过
          </Tag>
        )}
      </div>
      <div className="chatbi-draft-name">{proposal.display_name}</div>
      {proposal.description && <div className="chatbi-draft-desc">{proposal.description}</div>}
      {formalized && (
        <>
          {/* 口径展开轨迹由编译器确定性产出（聚合了谁、按什么分组、走了哪条关联）， */}
          {/* 是人判断「这条口径对不对」的依据，比 SQL 更好读，所以摆在 SQL 前面。 */}
          {proposal.caliber_trace?.length ? (
            <ul className="chatbi-draft-trace">
              {proposal.caliber_trace.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          ) : null}
          <SqlBlock
            sql={proposal.compiled_sql!}
            conversationId={conversationId}
            messageId={messageId}
            blockId={blockId}
          />
        </>
      )}
      <div className="chatbi-draft-note">
        {formalized
          ? patching
            ? "确认后把这份表达式写入该口径（仍是草稿，需你自行发布）；表达式已过编译与语义证明，但口径对不对请你自己看一眼上面的 SQL。"
            : "确认后创建为草稿口径并带上这份表达式（需你自行发布）；表达式已过编译与语义证明，但口径对不对请你自己看一眼上面的 SQL。"
          : "确认后创建为草稿口径（待你补全表达式并发布），不会直接改动本体或数据。"}
      </div>
      <Space>
        <Button
          type="primary"
          size="small"
          loading={state === "creating"}
          disabled={state === "done"}
          onClick={() => void onConfirm()}
        >
          {state === "done" ? "已保存，跳转中…" : patching ? "去确认写入" : "去确认创建"}
        </Button>
        {state === "error" && (
          <span className="chatbi-draft-error">保存失败，请重试或到口径页手动处理。</span>
        )}
      </Space>
    </div>
  );
}

const VIZ_LABEL: Record<string, string> = {
  bar: "柱状图",
  kpi: "指标卡",
  table: "表格",
};

/**
 * 数据应用提案块：Data Agent 主动提出「把这份口径做成面板/看板」。
 *
 * 与页面顶部那条动作条走**同一条路**（generate-widget / generate-app），区别只是由 agent
 * 提出而非用户自己想起来点。口径同样由本条消息的 payload 附上，不重调 LLM——生成出来的
 * 面板与对话里看到的口径一致，这条保证在 ChatBiPage 的 handler 里，不要在这里另起一套。
 */
function AppProposalBlock({
  proposal,
  onProposeApp,
}: {
  proposal: Extract<ChatBiBlock, { type: "app_proposal" }>["proposal"];
  onProposeApp?: (proposal: Extract<ChatBiBlock, { type: "app_proposal" }>["proposal"]) => void;
}) {
  const isDashboard = proposal.kind === "dashboard";
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="cyan" bordered={false}>
          数据应用提案
        </Tag>
        <span>{isDashboard ? "新建看板" : "生成面板"}</span>
      </div>
      <div className="chatbi-draft-name">{isDashboard ? proposal.name : proposal.title}</div>
      <div className="chatbi-draft-desc">
        {isDashboard ? `首个面板：${proposal.title} · ` : ""}
        {VIZ_LABEL[proposal.viz_type] ?? proposal.viz_type}
      </div>
      <div className="chatbi-draft-note">
        {isDashboard
          ? "确认后按本轮口径新建看板，随后可在编辑器里继续加面板。"
          : "确认后按本轮口径生成面板，并由你选择加入哪个看板（可新建）。"}
        口径复用这条回答，不会重新问一次数。
      </div>
      <Space>
        <Button
          type="primary"
          size="small"
          icon={isDashboard ? <DashboardOutlined /> : <AppstoreAddOutlined />}
          disabled={!onProposeApp}
          onClick={() => onProposeApp?.(proposal)}
        >
          {isDashboard ? "去新建看板" : "去生成面板"}
        </Button>
      </Space>
    </div>
  );
}

const DRAFT_SCOPE_LABEL: Record<string, string> = {
  draft: "业务对象 + 业务关系",
  objects: "只补业务对象",
  relations: "只补业务关系",
};

/**
 * 接数据提案块：登记数据源 / 为某域生成本体草稿。
 *
 * **凭据不经 agent**：建源点进去打开的是既有的数据源表单（预填名称/类型/catalog），
 * 连接信息由用户自己填、DSN 由那个表单组装。这里不碰密码，也不留密码。
 * 生成草稿点下去才真正启动 LLM 生成，产出仍是草稿——要在工作区确认、再发布。
 */
function OnboardProposalBlock({
  proposal,
  conversationId,
  messageId,
  blockId,
}: {
  proposal: Extract<ChatBiBlock, { type: "onboard_proposal" }>["proposal"];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  const navigate = useNavigate();
  const [dsOpen, setDsOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const isDatasource = proposal.kind === "datasource";

  const startDraft = async () => {
    const domainId = proposal.domain_id;
    if (!domainId) return;
    setStarting(true);
    try {
      const scope = proposal.scope ?? "draft";
      if (scope === "objects") await api.generateObjects(domainId);
      else if (scope === "relations") await api.generateRelations(domainId);
      else await api.generateDraft(domainId);
      message.success("已启动草稿生成，可在工作区查看进度");
      // 留痕：启动本体草稿生成是「本体确认」环。此前这个确认走 REST 旁路且立刻导航去
      // 工作区，会话里看不出用户点没点、按哪个范围生成的。
      recordDecisionQuietly(conversationId, {
        node: "ontology",
        stage: "onboard_draft",
        trigger: "draft_generation_started",
        message_id: messageId,
        block_id: blockId,
        summary: `为域「${proposal.domain_name ?? domainId}」启动生成：${DRAFT_SCOPE_LABEL[scope] ?? scope}`,
        proposed: { domain_id: domainId, scope: proposal.scope ?? "draft" },
        chosen: { domain_id: domainId, scope },
        ref_kind: "domain",
        ref_id: domainId,
      });
      navigate(`/workspace/${domainId}`);
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 409
          ? "该域已有草稿生成任务在跑，等它跑完再试"
          : err instanceof Error
            ? err.message
            : "启动失败，请重试",
      );
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="green" bordered={false}>
          接数据提案
        </Tag>
        <span>{isDatasource ? "登记数据源" : "生成本体草稿"}</span>
      </div>
      <div className="chatbi-draft-name">
        {isDatasource ? proposal.name : proposal.domain_name}
      </div>
      <div className="chatbi-draft-desc">
        {isDatasource ? (
          <>
            {proposal.datasource_kind}
            {proposal.catalog_name ? ` · catalog：${proposal.catalog_name}` : ""}
            {proposal.note ? ` · ${proposal.note}` : ""}
          </>
        ) : (
          <>
            {DRAFT_SCOPE_LABEL[proposal.scope ?? "draft"]}
            {proposal.reason ? ` · ${proposal.reason}` : ""}
          </>
        )}
      </div>
      <div className="chatbi-draft-note">
        {isDatasource ? (
          <>
            <SafetyOutlined /> 连接信息（账号/密码/连接串）由你在表单里自己填，助手不经手也不留存。
            {proposal.dropped_args?.length ? (
              <>（提案里的 {proposal.dropped_args.join("、")} 已被丢弃）</>
            ) : null}
          </>
        ) : (
          <>
            点击后才启动 LLM 生成，产出的对象/关系仍是**草稿**，要在工作区逐条确认后才能发布。
            {proposal.has_published_ontology
              ? "该域已有已发布本体：重跑会产生新草稿并进入合并流程，不是原地覆盖。"
              : ""}
          </>
        )}
      </div>
      <Space>
        {isDatasource ? (
          <Button
            type="primary"
            size="small"
            icon={<DatabaseOutlined />}
            onClick={() => setDsOpen(true)}
          >
            去填连接信息
          </Button>
        ) : (
          <Button
            type="primary"
            size="small"
            loading={starting}
            disabled={!proposal.domain_id}
            onClick={() => void startDraft()}
          >
            去生成草稿
          </Button>
        )}
      </Space>
      {isDatasource && dsOpen && (
        <DataSourcesModal
          open={dsOpen}
          onClose={() => setDsOpen(false)}
          prefill={{
            name: proposal.name,
            kind: proposal.datasource_kind,
            catalog_name: proposal.catalog_name ?? undefined,
          }}
          // 只在数据源**真的建成**后才留痕（表单打开又取消不算决策）。
          // 回调只带 id/name/kind——凭据不经 agent，也不进账本。
          onCreated={(ds) =>
            recordDecisionQuietly(conversationId, {
              node: "data",
              stage: "onboard_datasource",
              trigger: "datasource_created",
              message_id: messageId,
              block_id: blockId,
              summary: `登记数据源「${ds.name}」（${ds.kind}）`,
              proposed: { name: proposal.name, kind: proposal.datasource_kind },
              chosen: { name: ds.name, kind: ds.kind },
              ref_kind: "datasource",
              ref_id: ds.id,
            })
          }
        />
      )}
    </div>
  );
}

const PLAN_STATUS_ICON: Record<string, string> = {
  pending: "○",
  active: "◔",
  done: "✓",
};

/** 紧凑格式化数值：整数直出，小数保留 2 位。 */
function fmtNum(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

const TREND_ICON: Record<string, string> = { up: "↑", down: "↓", flat: "→" };

/**
 * 记忆提案块（P3.1）：Data Agent 只出提案（不写库）；点「记住」才 POST 落库为本域约定，
 * 后续作为软提示注入。守住「agent 只提案、写在人点击」不变量。
 */
function PreferenceProposalBlock({
  proposal,
  conversationId,
  messageId,
  blockId,
}: {
  proposal: Extract<ChatBiBlock, { type: "preference_proposal" }>["proposal"];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  const [state, setState] = useState<"idle" | "saving" | "done" | "error">("idle");
  const onRemember = async () => {
    if (!proposal.domain_id) {
      setState("error");
      return;
    }
    setState("saving");
    try {
      await api.rememberPreference(proposal.domain_id, proposal.text);
      setState("done");
      // 留痕：约定本身进的是域记忆（无会话溯源），这里补记"是这次对话定的"。
      recordDecisionQuietly(conversationId, {
        node: "data",
        stage: "preference",
        trigger: "preference_remembered",
        message_id: messageId,
        block_id: blockId,
        summary: `记住本域约定：${proposal.text}`,
        chosen: { text: proposal.text, domain_id: proposal.domain_id },
        ref_kind: "preference",
        dedup_key: blockId ? `${conversationId}:data:preference:${blockId}` : undefined,
      });
    } catch {
      setState("error");
    }
  };
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="cyan" bordered={false}>
          记忆提案
        </Tag>
        <span>记住本域约定</span>
      </div>
      <div className="chatbi-draft-name">{proposal.text}</div>
      <div className="chatbi-draft-note">
        点「记住」后作为本域约定长期生效（后续问答默认遵循此口径/范围），不改动本体或数据。
      </div>
      <Space>
        <Button
          type="primary"
          size="small"
          loading={state === "saving"}
          disabled={state === "done"}
          onClick={() => void onRemember()}
        >
          {state === "done" ? "已记住" : "记住"}
        </Button>
        {state === "error" && <span className="chatbi-draft-error">保存失败，请重试。</span>}
      </Space>
    </div>
  );
}

/**
 * 结果分析块（P5）：analyze_result 产出的统计画像 + IQR 离群检测（+ 可选趋势/突变）。让「有没有
 * 异常/趋势如何」这类判断有真实计算支撑，逐数值列展示统计量、离群、趋势方向与突变点。
 */
function InsightBlock({
  analysis,
}: {
  analysis: Extract<ChatBiBlock, { type: "insight" }>["analysis"];
}) {
  const metaBits = [`${analysis.row_count} 行`, `${analysis.total_outliers} 个离群`];
  if (analysis.ordered_by) metaBits.push(`${analysis.total_jumps ?? 0} 处突变`);
  return (
    <div className="chatbi-insight">
      <div className="chatbi-insight-head">
        <Tag color="volcano" bordered={false}>
          结果分析
        </Tag>
        <span className="chatbi-insight-meta">
          {metaBits.join(" · ")}
          {analysis.ordered_by && ` · 按 ${analysis.ordered_by} 排序`}
        </span>
      </div>
      {analysis.columns.map((c) => (
        <div key={c.column} className="chatbi-insight-col">
          <div className="chatbi-insight-colname">
            {c.column}
            {c.trend && (
              <span className={`chatbi-insight-trend chatbi-insight-trend--${c.trend.direction}`}>
                {TREND_ICON[c.trend.direction] ?? ""}
                {c.trend.change_pct != null &&
                  ` ${c.trend.change_pct > 0 ? "+" : ""}${c.trend.change_pct}%`}
              </span>
            )}
          </div>
          <div className="chatbi-insight-stats">
            <span>min {fmtNum(c.min)}</span>
            {c.p25 != null && <span>p25 {fmtNum(c.p25)}</span>}
            <span>均值 {fmtNum(c.mean)}</span>
            {c.median != null && <span>中位 {fmtNum(c.median)}</span>}
            {c.p75 != null && <span>p75 {fmtNum(c.p75)}</span>}
            <span>max {fmtNum(c.max)}</span>
            {c.std != null && <span>σ {fmtNum(c.std)}</span>}
            {c.nulls > 0 && <span className="chatbi-insight-null">空 {c.nulls}</span>}
          </div>
          {!!c.outlier_count && (
            <div className="chatbi-insight-outliers">
              离群 {c.outlier_count} 个
              {c.outliers && c.outliers.length > 0 && (
                <span className="chatbi-insight-outvals">
                  ：{c.outliers.map(fmtNum).join("、")}
                </span>
              )}
            </div>
          )}
          {c.jumps && c.jumps.length > 0 && (
            <div className="chatbi-insight-jumps">
              突变 {c.jumps.length} 处：
              {c.jumps
                .map((j) => `${String(j.at)}（${fmtNum(j.from)}→${fmtNum(j.to)}）`)
                .join("、")}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * 分析计划块（P2）：update_plan 产出的声明式多步路线图，让开放式分析可见、有主线。
 * 与实时执行轨迹（StepTrace）互补——这里是「打算怎么拆」，那里是「实际做了什么」。
 */
function PlanBlock({
  steps,
  note,
}: {
  steps: Extract<ChatBiBlock, { type: "plan" }>["steps"];
  note?: string;
}) {
  const done = steps.filter((s) => s.status === "done").length;
  return (
    <div className="chatbi-plan">
      <div className="chatbi-plan-head">
        <Tag color="purple" bordered={false}>
          分析计划
        </Tag>
        <span className="chatbi-plan-progress">
          {done}/{steps.length}
        </span>
      </div>
      <ol className="chatbi-plan-steps">
        {steps.map((s, i) => (
          <li key={i} className={`chatbi-plan-step chatbi-plan-step--${s.status}`}>
            <span className="chatbi-plan-mark">{PLAN_STATUS_ICON[s.status] ?? "○"}</span>
            <span className="chatbi-plan-title">{s.title}</span>
          </li>
        ))}
      </ol>
      {note && <div className="chatbi-plan-note">{note}</div>}
    </div>
  );
}

/**
 * 任务制品抽屉（P0）：复用治理面板的 ArtifactDetail（已含 dry-run 差异 + 校验/确认/执行 + 回执）。
 * agent 只出提案，人在此抽屉里过既有人审门；写全部落在 publisher 门控之后。
 */
function useArtifactDrawer(
  onClose?: () => void,
  conversationId?: string,
  messageId?: string,
  blockId?: string,
) {
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const { closure, notifyWritten } = useDecisionLedger();
  const resultConfirmed = Boolean(
    detail &&
      closure?.records.some(
        (record) =>
          record.node === "result" &&
          record.ref_kind === "artifact" &&
          record.ref_id === detail.id &&
          ["accepted", "modified"].includes(record.outcome),
      ),
  );
  const STEP_LABEL: Record<string, string> = { validate: "校验", confirm: "确认", execute: "执行" };
  const onStep = async (step: "validate" | "confirm" | "execute", artifact: GovernanceArtifact) => {
    setBusy(true);
    try {
      const next =
        step === "validate"
          ? await api.validateArtifact(artifact.id)
          : step === "confirm"
            ? await api.confirmArtifact(artifact.id)
            : await api.executeArtifact(artifact.id);
      setDetail(next);
      message.success(`${STEP_LABEL[step]}完成：${next.status}`);
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 403
          ? "需要 publisher 角色：写侧任务仅 publisher 可校验/确认/执行"
          : err instanceof Error
            ? err.message
            : "操作失败",
      );
    } finally {
      setBusy(false);
    }
  };
  const confirmResult = (artifact: GovernanceArtifact) => {
    recordDecisionQuietly(conversationId, {
      node: "result",
      stage: "artifact_result_confirm",
      trigger: "result_confirm",
      message_id: messageId,
      block_id: blockId,
      summary: `确认「${artifact.name}」执行结果：${artifact.status}`,
      chosen: {
        status: artifact.status,
        receipt: artifact.execution_receipt ?? null,
      },
      ref_kind: "artifact",
      ref_id: artifact.id,
      dedup_key: `${conversationId}:result:artifact:${artifact.id}`,
    });
    notifyWritten();
  };
  const node = (
    <ArtifactDetail
      artifact={detail}
      busy={busy}
      onClose={() => {
        setDetail(null);
        // 任务链要据此回读链态：抽屉里刚走完的那一步可能已经成功，下一步随之可以起草。
        onClose?.();
      }}
      onStep={onStep}
      onConfirmResult={conversationId ? confirmResult : undefined}
      resultConfirmed={resultConfirmed}
    />
  );
  return { open: setDetail, node };
}

const ACTION_KIND_LABEL: Record<string, string> = {
  materialize: "物化",
  sync: "同步",
  transform: "加工",
  metric: "聚合",
};

const TASK_STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  validated: "blue",
  confirmed: "gold",
  executing: "processing",
  succeeded: "green",
  failed: "red",
};

/** 提案 context 的键 → 中文标签。没收录的键原样显示键名（不猜、不隐藏）。 */
const CONTEXT_LABELS: Record<string, string> = {
  target_datasource_id: "目标数据源",
  target_database: "目标库",
  target_table: "目标表",
  object_type: "目标对象",
  engine: "引擎",
  database_prefix: "库名前缀",
  load_strategy: "装载方式",
  partition_key: "分区键",
  refresh_cron: "调度频率",
  sync_tool: "搬运工具",
  source_ref_alias: "源连接别名",
  selected_targets: "物化范围",
  database_overrides: "各层目标库",
  table_overrides: "表名覆盖",
  overrides: "逐实体覆盖",
  cleansing_rules: "清洗规则",
};

const LOAD_STRATEGY_LABELS: Record<string, string> = {
  full: "全量覆盖",
  incremental: "增量追加",
  cdc: "CDC 变更捕获",
};

/** 数仓分层展示名（与 MaterializeModal 的 LAYER_LABEL 同口径）。 */
const LAYER_LABELS: Record<string, string> = {
  dim: "维度层 DIM",
  dwd: "明细层 DWD",
  ads: "应用层 ADS",
};

/** 逐实体覆盖里各字段的中文说法。 */
const PATCH_LABELS: Record<string, string> = {
  load_strategy: "装载方式",
  partition_key: "分区键",
  refresh_cron: "调度",
  target_layer: "分层",
};

function contextValueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.join("、") : "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 一条逐实体补丁 → 「装载方式 全量覆盖 · 分区键 无」。 */
function patchText(patch: Record<string, unknown>): string {
  const parts = Object.entries(patch).map(([k, v]) => {
    const label = PATCH_LABELS[k] ?? k;
    if (v === null || v === undefined || v === "") {
      return `${label} ${k === "refresh_cron" ? "不定时" : "无"}`;
    }
    if (k === "load_strategy") return `${label} ${LOAD_STRATEGY_LABELS[String(v)] ?? v}`;
    return `${label} ${String(v)}`;
  });
  return parts.join(" · ") || "—";
}

/**
 * 把 `{层/契约id: 值}` 这类嵌套覆盖摊成「谁 → 什么」的行。
 *
 * 契约 id 是内部主键，直接摆出来等于没说——用契约清单换成实体显示名；换不到（清单还没
 * 到、或该契约已不在）就退回原 id，**不隐藏**：宁可露一个 id，也不能让一条覆盖凭空消失。
 */
function overrideRows(
  key: string,
  value: Record<string, unknown>,
  nameOf: (contractId: string) => string,
): Array<{ k: string; v: string }> {
  return Object.entries(value).map(([k, v]) => {
    if (key === "database_overrides") return { k: LAYER_LABELS[k] ?? k, v: String(v) };
    const who = nameOf(k);
    if (key === "overrides" && v && typeof v === "object" && !Array.isArray(v)) {
      return { k: who, v: patchText(v as Record<string, unknown>) };
    }
    return { k: who, v: contextValueText(v) };
  });
}

/**
 * 提案参数表：把 context 摊开成「中文标签 + 可改的值」。
 *
 * 此前这里是一行 `JSON.stringify(context)`——用户既看不懂 Drafter 替他定了什么，也改不了。
 * 而 sync/transform 的 Drafter 在没给对象时会**按意图猜**一个，猜完不回显，等于让人闭着眼
 * 点「去校验并执行」。
 */
function ProposalContextForm({
  kind,
  context,
  ontologyId,
  onChange,
  readOnly = false,
}: {
  kind: string;
  context: Record<string, unknown>;
  ontologyId?: string | null;
  onChange: (key: string, value: unknown) => void;
  readOnly?: boolean;
}) {
  // 契约 id → 实体显示名。只有确实出现了按契约 id 索引的覆盖才去拉清单。
  const [contractNames, setContractNames] = useState<Record<string, string> | null>(null);
  const needsContracts = "table_overrides" in context || "overrides" in context;
  useEffect(() => {
    if (!needsContracts || !ontologyId || contractNames !== null) return;
    api
      .listMaterializationContracts(ontologyId)
      .then((list) =>
        setContractNames(
          Object.fromEntries(
            list.map((c) => [c.id, c.target_display_name ?? c.target_name ?? c.id]),
          ),
        ),
      )
      .catch(() => setContractNames({}));
  }, [needsContracts, ontologyId, contractNames]);
  const nameOf = (contractId: string) => contractNames?.[contractId] ?? contractId;

  // schema 里定义了控件的字段交给 SpecForm 渲染成完整可编辑表单（含 LLM 没填的空字段）；
  // schema 之外、但 LLM 填了的键（selected_targets / *_overrides / sync_tool 等嵌套覆盖）
  // 保留只读展示——它们无标准控件，但不能凭空消失。
  const schemaKeys = new Set((SPEC_FIELDS[kind] ?? []).map((f) => f.key));
  const extraKeys = Object.keys(context).filter(
    (k) =>
      !schemaKeys.has(k) && k !== "ontology_id" && context[k] !== null && context[k] !== undefined,
  );

  return (
    <div className="chatbi-proposal-params">
      <SpecForm
        kind={kind}
        mode="proposal"
        value={context}
        ontologyId={ontologyId}
        onChange={onChange}
        disabled={readOnly}
      />
      {extraKeys.map((key) => {
        const value = context[key];
        const label = CONTEXT_LABELS[key] ?? key;
        const nested =
          value && typeof value === "object" && !Array.isArray(value)
            ? overrideRows(key, value as Record<string, unknown>, nameOf)
            : null;
        return (
          <div className="chatbi-proposal-param" key={key}>
            <span className="chatbi-proposal-param-label">{label}</span>
            {nested ? (
              <div className="chatbi-proposal-nested">
                {nested.length === 0 ? (
                  <span className="chatbi-proposal-param-ro">—</span>
                ) : (
                  nested.map((row) => (
                    <div className="chatbi-proposal-nested-row" key={row.k}>
                      <span className="chatbi-proposal-nested-k">{row.k}</span>
                      <span className="chatbi-proposal-nested-v">{row.v}</span>
                    </div>
                  ))
                )}
              </div>
            ) : (
              <code className="chatbi-proposal-param-ro">{contextValueText(value)}</code>
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * 数据任务提案块（P0）：Data Agent 只出提案（不执行、不写库）；用户已在前一张向导中逐步
 * 确认需求、本体和数据后，这里只创建任务草稿。随后在 ArtifactDetail 中生成并查看 dry-run，
 * 再明确确认执行方案、执行和验收结果。写侧全程 publisher 门控，agent 不碰。
 *
 * P2：参数不再是一行 JSON——摊成可改的表，用户点之前看得见、也改得动。
 */
function ActionProposalBlock({
  proposal,
  conversationId,
  messageId,
  blockId,
}: {
  proposal: Extract<ChatBiBlock, { type: "action_proposal" }>["proposal"];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  const [drafting, setDrafting] = useState(false);
  const [context, setContext] = useState<Record<string, unknown>>(() => ({
    ...(proposal.context ?? {}),
  }));
  const { open, node } = useArtifactDrawer(
    undefined,
    conversationId,
    messageId,
    blockId,
  );
  const kindLabel = ACTION_KIND_LABEL[proposal.kind] ?? proposal.kind;
  const onConfirm = async () => {
    setDrafting(true);
    try {
      // 用户改过的参数为准：draft_payload 的 context 以本地编辑值覆盖后再提交。
      const artifact = await api.draftArtifact({ ...proposal.draft_payload, context });
      // P1：把本会话与该任务关联，后续可免 id 追踪。best-effort，失败不阻断主流程。
      // 顺带带上「提案原样」与「人改后」两份 context——服务端据此留痕出人改了哪些参数。
      // 这个 diff 此前只存在于浏览器内存里，确认完就没了。
      if (conversationId) {
        try {
          // 必须先建立会话→制品关联，再开放校验/确认/执行；后续三个动作靠这条关联
          // 把方案、执行和结果记回同一闭环。此前 fire-and-forget 存在确认先于关联的竞态。
          await api.linkChatBiTask(conversationId, {
            artifact_id: artifact.id,
            kind: proposal.kind,
            intent: proposal.intent,
            proposed_context: (proposal.context ?? {}) as Record<string, unknown>,
            chosen_context: toJsonSafe(context) as Record<string, unknown>,
            message_id: messageId,
            block_id: blockId,
          });
        } catch {
          message.warning("任务草稿已创建，但未能关联当前会话；请重试打开任务后再执行");
          return;
        }
      }
      // 创建草稿后立即运行无副作用校验和 dry-run，直接展示“执行方案预览”。
      // 人工确认与执行仍是后续独立动作，不会因自动校验而越过门禁。
      try {
        const validated = await api.validateArtifact(artifact.id);
        open(validated);
        if (validated.status === "validated") {
          message.success("任务草稿已创建，执行方案已生成，请确认后再执行");
        } else {
          message.warning("任务草稿已创建，执行方案存在阻断项，请在抽屉中查看");
        }
      } catch {
        open(artifact);
        message.warning("任务草稿已创建，执行方案生成失败，请在抽屉中点击“生成执行方案”重试");
      }
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 403
          ? "需要 publisher 角色：写侧任务仅 publisher 可创建"
          : err instanceof Error
            ? err.message
            : "起草失败，请重试",
      );
    } finally {
      setDrafting(false);
    }
  };
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="geekblue" bordered={false}>
          数据任务提案
        </Tag>
        <span>新建{kindLabel}任务</span>
      </div>
      <div className="chatbi-draft-name">{proposal.intent}</div>
      <ProposalContextForm
        kind={proposal.kind}
        context={context}
        ontologyId={proposal.ontology_id}
        onChange={(key, value) => setContext((prev) => ({ ...prev, [key]: value }))}
        readOnly
      />
      <div className="chatbi-draft-note">
        参数来自刚完成的确认向导，已锁定；如需修改请重新发起任务确认。点击后只创建任务草稿；
        接下来需「生成执行方案 → 查看 dry-run → 确认执行方案 →
        执行 → 确认结果」，不会自动执行或直接改动数据。
      </div>
      <Space>
        <Button type="primary" size="small" loading={drafting} onClick={() => void onConfirm()}>
          创建任务草稿
        </Button>
      </Space>
      {node}
    </div>
  );
}

/** 链上一步的制品状态 → 中文标签与色。未起草的如实显示「待起草」，不冒充 drafted。 */
const STEP_STATUS_LABEL: Record<string, { label: string; color: string }> = {
  drafted: { label: "待校验", color: "default" },
  validated: { label: "待确认", color: "blue" },
  confirmed: { label: "待执行", color: "gold" },
  executing: { label: "执行中", color: "processing" },
  succeeded: { label: "已完成", color: "green" },
  failed: { label: "失败", color: "red" },
};

const PIPELINE_STATUS_LABEL: Record<string, { label: string; color: string }> = {
  drafted: { label: "未开始", color: "default" },
  running: { label: "进行中", color: "processing" },
  succeeded: { label: "已完成", color: "green" },
  failed: { label: "有失败", color: "red" },
};

/**
 * 任务链提案块：Data Agent 出的**一条链**（如 物化 → 清洗 → 聚合）。
 *
 * 链只管两件此前只能靠人肉完成的事：记住下一步是什么，以及把上游定下的落点（目标数据源/
 * 库/引擎）接给下游。**它不替谁确认**——每一步仍是一条独立制品，点「起草第 N 步」后照旧
 * 在复用的 ArtifactDetail 抽屉里过「校验 → dry-run → 人工确认 → 执行」。
 *
 * 故这里没有「一键跑完整条链」的按钮：那必然绕过逐制品的人工确认，而「未确认不得执行」是
 * 这条流水线的硬不变量。
 */
function PipelineProposalBlock({
  proposal,
  conversationId,
}: {
  proposal: Extract<ChatBiBlock, { type: "pipeline_proposal" }>["proposal"];
  conversationId?: string;
}) {
  // 建链前：可就地改各步参数。建链后：以服务端的链态为准（本地草稿不再有意义）。
  const [drafts, setDrafts] = useState<Record<string, unknown>[]>(() =>
    proposal.steps.map((s) => ({ ...(s.context ?? {}) })),
  );
  const [pipeline, setPipeline] = useState<TaskPipeline | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (id: string) => {
    try {
      setPipeline(await api.getPipeline(id));
    } catch {
      /* 回读失败不打断主流程：下次操作还会再拉一次 */
    }
  }, []);
  // 抽屉里刚走完的那一步可能已经成功，下一步随之解锁——关掉抽屉就回读一次链态。
  const { open, node } = useArtifactDrawer(
    () => {
      if (pipeline) void refresh(pipeline.id);
    },
    conversationId,
  );

  const create = async () => {
    setBusy(true);
    try {
      setPipeline(
        await api.createPipeline({
          ...proposal.create_payload,
          // 用户改过的参数为准
          steps: proposal.create_payload.steps.map((s, i) => ({ ...s, context: drafts[i] })),
        }),
      );
      message.success("任务链已创建，可逐步起草");
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 403
          ? "需要 publisher 角色：写侧任务仅 publisher 可创建"
          : err instanceof Error
            ? err.message
            : "创建任务链失败",
      );
    } finally {
      setBusy(false);
    }
  };

  const advance = async () => {
    if (!pipeline) return;
    setBusy(true);
    try {
      const result = await api.advancePipeline(pipeline.id);
      setPipeline(result.pipeline);
      if (conversationId) {
        // 与单发提案同构：把本会话与该任务关联，后续可免 id 追踪。best-effort。
        void api
          .linkChatBiTask(conversationId, {
            artifact_id: result.artifact.id,
            kind: result.artifact.kind,
            intent: result.artifact.intent ?? undefined,
          })
          .catch(() => {});
      }
      open(result.artifact);
    } catch (err) {
      // 409 = 上游还没跑成功，后端已说清卡在哪一步——原样透出，别糊成「操作失败」。
      message.warning(err instanceof Error ? err.message : "无法推进到下一步");
      void refresh(pipeline.id);
    } finally {
      setBusy(false);
    }
  };

  // C2：一键起草全部步骤（血缘驱动，起草阶段不阻塞）。所有制品先落地，
  // 人再逐个校验/确认/执行；执行顺序由血缘决定。「未确认不得执行」不变。
  const draftAll = async () => {
    if (!pipeline) return;
    setBusy(true);
    try {
      const result = await api.draftAllPipeline(pipeline.id);
      setPipeline(result.pipeline);
      if (conversationId) {
        for (const artifact of result.artifacts) {
          void api
            .linkChatBiTask(conversationId, {
              artifact_id: artifact.id,
              kind: artifact.kind,
              intent: artifact.intent ?? undefined,
            })
            .catch(() => {});
        }
      }
      // 打开第一个制品的抽屉（其余在链态里逐个查看）
      if (result.artifacts.length > 0) open(result.artifacts[0]);
      message.success(`已起草全部 ${result.artifacts.length} 步，请逐个校验/确认/执行`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "一键起草失败");
      void refresh(pipeline.id);
    } finally {
      setBusy(false);
    }
  };

  const openStep = async (artifactId: string) => {
    try {
      open(await api.getArtifact(artifactId));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "读取任务失败");
    }
  };

  const steps = pipeline?.steps ?? null;
  const overall = pipeline ? PIPELINE_STATUS_LABEL[pipeline.status] : null;

  return (
    <div className="chatbi-draft chatbi-pipeline">
      <div className="chatbi-draft-head">
        <Tag color="geekblue" bordered={false}>
          任务链提案
        </Tag>
        <span>{proposal.name}</span>
        {overall && (
          <Tag color={overall.color} bordered={false}>
            {overall.label}
          </Tag>
        )}
      </div>
      {proposal.intent && <div className="chatbi-draft-name">{proposal.intent}</div>}

      {/* C2：一键起草全部步骤（血缘驱动，起草阶段不阻塞）。链创建后、尚未全部起草时显示。 */}
      {pipeline && pipeline.next_step_index !== null && pipeline.next_step_index !== undefined && (
        <div style={{ margin: "8px 0 4px" }}>
          <Button
            size="small"
            type="primary"
            ghost
            loading={busy}
            onClick={() => void draftAll()}
          >
            一键起草全部步骤
          </Button>
          <span style={{ marginLeft: 8, fontSize: 12, color: "#999" }}>
            所有步骤一次起草，逐个校验/确认/执行（顺序由血缘决定）
          </span>
        </div>
      )}

      <div className="chatbi-pipeline-steps">
        {proposal.steps.map((step, i) => {
          const live = steps?.[i];
          const status = live?.artifact_status
            ? (STEP_STATUS_LABEL[live.artifact_status] ?? {
                label: live.artifact_status,
                color: "default",
              })
            : null;
          const isNext = pipeline?.next_step_index === i;
          return (
            <div className="chatbi-pipeline-step" key={step.kind + i}>
              <div className="chatbi-pipeline-step-head">
                <span className="chatbi-pipeline-step-no">{i + 1}</span>
                <Tag bordered={false}>{ACTION_KIND_LABEL[step.kind] ?? step.kind}</Tag>
                <span className="chatbi-pipeline-step-intent">{step.intent}</span>
                {/* 还没起草到这一步就如实说「待起草」——不拿 drafted 冒充「已经建了制品」。 */}
                <Tag color={status?.color ?? "default"} bordered={false}>
                  {status?.label ?? "待起草"}
                </Tag>
              </div>
              {!pipeline && (
                <ProposalContextForm
                  kind={step.kind}
                  context={drafts[i] ?? {}}
                  ontologyId={proposal.ontology_id}
                  onChange={(key, value) =>
                    setDrafts((prev) => prev.map((c, j) => (j === i ? { ...c, [key]: value } : c)))
                  }
                />
              )}
              {pipeline && (
                <div className="chatbi-pipeline-step-actions">
                  {live?.artifact_id ? (
                    <Button size="small" onClick={() => void openStep(live.artifact_id!)}>
                      {live.artifact_status === "succeeded" ? "查看" : "继续校验/确认/执行"}
                    </Button>
                  ) : isNext ? (
                    <Button
                      size="small"
                      type="primary"
                      loading={busy}
                      disabled={Boolean(pipeline.next_blocked_reason)}
                      onClick={() => void advance()}
                    >
                      起草第 {i + 1} 步
                    </Button>
                  ) : (
                    <span className="chatbi-pipeline-step-wait">等前一步完成</span>
                  )}
                  {isNext && pipeline.next_blocked_reason && (
                    <span className="chatbi-pipeline-step-wait">
                      {pipeline.next_blocked_reason}
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* P2-4：周期任务控件——链走通（status=succeeded）后显示 */}
      {pipeline && pipeline.status === "succeeded" && (
        <div
          className="chatbi-pipeline-schedule"
          style={{ marginTop: 16, padding: "12px 16px", background: "#f5f5f5", borderRadius: 4 }}
        >
          {!pipeline.compiled_dag_id ? (
            <>
              <div style={{ marginBottom: 8, fontSize: 13, fontWeight: 500 }}>挂成周期任务</div>
              <div style={{ color: "#666", marginBottom: 12, fontSize: 12 }}>
                整条链已走通，可编译成一条 Airflow DAG、挂上调度周期后自动反复执行。
              </div>
              <Space>
                <span style={{ fontSize: 12 }}>调度周期</span>
                <CronPicker
                  value={pipeline.schedule_cron ?? "0 2 * * *"}
                  onChange={async (cron) => {
                    try {
                      setPipeline(await api.setPipelineSchedule(pipeline.id, cron));
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : "设置失败");
                    }
                  }}
                  size="small"
                />
                <Button
                  type="primary"
                  size="small"
                  loading={busy}
                  disabled={!pipeline.schedule_cron}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await api.compilePipeline(pipeline.id);
                      await refresh(pipeline.id);
                      message.success("已编译成周期 DAG");
                    } catch (err) {
                      message.error(
                        err instanceof Error
                          ? err.message
                          : "编译失败（检查所有步骤是否已确认且执行过）",
                      );
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  编译并挂起
                </Button>
              </Space>
            </>
          ) : (
            <>
              <div style={{ marginBottom: 8, fontSize: 13, fontWeight: 500 }}>周期任务已挂起</div>
              <div style={{ color: "#666", marginBottom: 8, fontSize: 12 }}>
                DAG ID: <code>{pipeline.compiled_dag_id}</code>
              </div>
              <div style={{ color: "#666", marginBottom: 12, fontSize: 12 }}>
                调度周期:{" "}
                {pipeline.schedule_cron
                  ? cronstrue.toString(pipeline.schedule_cron, { locale: "zh_CN" })
                  : "无"}
              </div>
              <Space>
                <Button
                  size="small"
                  danger
                  loading={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      setPipeline(await api.unschedulePipeline(pipeline.id));
                      message.success("已下线周期任务");
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : "下线失败");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  下线
                </Button>
                <span style={{ fontSize: 12, color: "#999" }}>
                  （下线只清 ontoMeta 记录，DAG 文件需另行从 Airflow dags_dir 删除）
                </span>
              </Space>
            </>
          )}
        </div>
      )}

      <div className="chatbi-draft-note">
        {pipeline
          ? "每一步都要各自过「校验 → dry-run 差异 → 人工确认 → 执行」；上一步执行成功后，下一步才可起草，届时目标数据源/库会自动接过去。"
          : "点击后只创建这条链，不会起草或执行任何任务；随后逐步起草，每步仍需人工确认才执行。"}
      </div>
      {!pipeline && (
        <Space>
          <Button type="primary" size="small" loading={busy} onClick={() => void create()}>
            创建任务链
          </Button>
        </Space>
      )}
      {node}
    </div>
  );
}

/**
 * 任务状态块（P0）：get_task_status 回读的数据任务态与回执摘要，列表展示；
 * 「查看」拉取完整制品并在复用抽屉里看 dry-run/回执（只读或续走人审门）。
 */
function TaskStatusBlock({
  status,
  conversationId,
  messageId,
  blockId,
}: {
  status: Extract<ChatBiBlock, { type: "task_status" }>["status"];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const { open, node } = useArtifactDrawer(
    undefined,
    conversationId,
    messageId,
    blockId,
  );
  const tasks = status.tasks ?? [];
  // L4 血缘：status.lineage = { tasks, dependencies }（谁产出谁消费）。
  // dependencies: [{ upstream: task_id, downstream: task_id }]
  const lineageDeps = status.lineage?.dependencies ?? [];
  const onView = async (id: string) => {
    setLoadingId(id);
    try {
      open(await api.getArtifact(id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoadingId(null);
    }
  };
  if (!tasks.length) {
    return <div className="chatbi-draft-note">当前数据域暂无数据任务。</div>;
  }
  return (
    <div className="chatbi-tasks">
      {tasks.map((t) => (
        <div key={t.id} className="chatbi-task-row">
          <Tag color={TASK_STATUS_COLOR[t.status] ?? "default"}>{t.status}</Tag>
          <span className="chatbi-task-name">
            {ACTION_KIND_LABEL[t.kind] ?? t.kind} · {t.name}
          </span>
          {t.receipt_summary && <span className="chatbi-task-receipt">{t.receipt_summary}</span>}
          <Button
            type="link"
            size="small"
            loading={loadingId === t.id}
            onClick={() => void onView(t.id)}
          >
            查看
          </Button>
        </div>
      ))}
      {/* L4 血缘：任务间依赖（上游产出 → 下游消费）。有边才画。 */}
      {lineageDeps.length > 0 && (
        <div className="chatbi-task-lineage">
          {lineageDeps.map((dep, i) => {
            const up = tasks.find((t) => t.id === dep.upstream);
            const down = tasks.find((t) => t.id === dep.downstream);
            return (
              <div key={i} className="chatbi-task-lineage-edge">
                <span className="chatbi-task-lineage-node">
                  {up ? (ACTION_KIND_LABEL[up.kind] ?? "") + " · " + up.name : dep.upstream}
                </span>
                <span className="chatbi-task-lineage-arrow">→</span>
                <span className="chatbi-task-lineage-node">
                  {down ? (ACTION_KIND_LABEL[down.kind] ?? "") + " · " + down.name : dep.downstream}
                </span>
              </div>
            );
          })}
        </div>
      )}
      <AckControl
        target={{
          conversationId,
          messageId,
          blockId,
          // 任务回执的验收就是六环里的**结果确认**——挂错环（曾记成 data）会让
          // result 恒不可达，闭环永远差最后一格、且恒报「已执行但结果未确认」。
          node: "result",
          stage: "task_status",
          summary: `数据任务（${tasks.length} 项）`,
          chosen: tasks.map((t) => t.id),
        }}
        label="任务状态是否符合预期？"
      />
      {node}
    </div>
  );
}

/**
 * 本体映射块（V3 S0 治死板核心）：
 * - `caliber`：完整口径卡（编译指标/多步映射）——复用原 CaliberDecomposition。
 * - `inline`：一行 chip（平凡单步映射）——不再对每个答案套整张报表模板。
 */
function MappingBlock({
  variant,
  items,
  references,
  conversationId,
  messageId,
  blockId,
}: {
  variant: "inline" | "caliber";
  items: ChatBiCaliberItem[];
  references: ChatBiCaliberReference[];
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
  if (variant === "inline") {
    // 无口径展开——只有「命中本体」，收成一行内联 chip。
    if (!references.length) return null;
    return (
      <div className="chatbi-mapping-inline">
        <span className="chatbi-mapping-inline-label">命中本体</span>
        {references.map((reference, ri) => (
          <CaliberRefChip key={ri} reference={reference} />
        ))}
        <AckControl
          target={{
            conversationId,
            messageId,
            blockId,
            node: "ontology",
            stage: "mapping",
            summary: `映射本体（${references.length} 项）`,
            chosen: references.map((r) => r.id),
          }}
          label="映射是否准确？"
        />
      </div>
    );
  }
  return (
    <>
      <CaliberDecomposition items={items} references={references} />
      <AckControl
        target={{
          conversationId,
          messageId,
          blockId,
          node: "ontology",
          stage: "mapping",
          summary: `映射本体（${references.length} 项）`,
          chosen: references.map((r) => r.id),
        }}
        label="映射是否准确？"
      />
    </>
  );
}

function colTitle(columns: ChatBiDataResult["columns"], key: string): string {
  const col = columns?.find((c) => String(c.key ?? c.title ?? "") === key);
  return String(col?.title ?? key);
}

function truncLabel(s: string): string {
  return s.length > 8 ? `${s.slice(0, 8)}…` : s;
}

/**
 * 图表块（V3 S1）：轻量 SVG，无第三方依赖，沿用 DataAppRenderer 的手绘风格。
 * 支持 bar / line / area 三型（决策 3 的自研精简 spec）；pie/scatter 留待 S1.x。
 * 数据自带（columns/rows），x/y 已在后端 render_chart 校验为真实结果列。
 */
function ChatBiChart({
  spec,
  columns,
  rows,
}: {
  spec: { kind: "bar" | "line" | "area"; x: string; y: string; title?: string };
  columns: ChatBiDataResult["columns"];
  rows: ChatBiDataResult["rows"];
}) {
  const points = (rows ?? []).slice(0, 60).map((r) => ({
    label: String(r[spec.x] ?? ""),
    value: Number(r[spec.y] ?? 0) || 0,
  }));
  if (!points.length) {
    return <div className="chatbi-chart chatbi-chart--empty">无可视化数据</div>;
  }
  const max = Math.max(...points.map((p) => p.value), 1);
  const H = 220;
  const padT = 12;
  const padB = 28;
  const stepX = spec.kind === "bar" ? 52 : 56;
  const barW = 36;
  const width = Math.max(points.length * stepX + 40, 320);
  const px = (i: number) => 24 + i * stepX + (spec.kind === "bar" ? 0 : stepX / 2);
  const py = (v: number) => padT + (H - (v / max) * H);
  const linePts = points.map((p, i) => `${px(i)},${py(p.value)}`).join(" ");
  const areaPts = `${px(0)},${padT + H} ${linePts} ${px(points.length - 1)},${padT + H}`;

  return (
    <div className="chatbi-chart">
      {spec.title && <div className="chatbi-chart-title">{spec.title}</div>}
      <div style={{ overflowX: "auto" }}>
        <svg width={width} height={H + padT + padB}>
          {spec.kind === "bar" ? (
            points.map((p, i) => {
              const h = Math.round((p.value / max) * H);
              const bx = px(i);
              const by = padT + H - h;
              return (
                <g key={i}>
                  <rect x={bx} y={by} width={barW} height={h} rx={4} fill="var(--om-primary)" />
                  <text
                    x={bx + barW / 2}
                    y={by - 4}
                    textAnchor="middle"
                    fontSize={11}
                    fill="var(--om-text-secondary)"
                  >
                    {p.value}
                  </text>
                  <text
                    x={bx + barW / 2}
                    y={H + padT + 18}
                    textAnchor="middle"
                    fontSize={11}
                    fill="var(--om-text-tertiary)"
                  >
                    {truncLabel(p.label)}
                  </text>
                </g>
              );
            })
          ) : (
            <>
              {spec.kind === "area" && (
                <polygon points={areaPts} fill="var(--om-primary)" opacity={0.15} />
              )}
              <polyline points={linePts} fill="none" stroke="var(--om-primary)" strokeWidth={2} />
              {points.map((p, i) => (
                <g key={i}>
                  <circle cx={px(i)} cy={py(p.value)} r={3} fill="var(--om-primary)" />
                  <text
                    x={px(i)}
                    y={H + padT + 18}
                    textAnchor="middle"
                    fontSize={11}
                    fill="var(--om-text-tertiary)"
                  >
                    {truncLabel(p.label)}
                  </text>
                </g>
              ))}
            </>
          )}
        </svg>
      </div>
      <div className="chatbi-chart-axis">
        x：{colTitle(columns, spec.x)} ・ y：{colTitle(columns, spec.y)}（max {max}）
      </div>
    </div>
  );
}

// 关系结构类型 → 颜色（与 ClusterMatrixView 保持一致；derivation=血缘）。
const LINEAGE_EDGE_COLOR: Record<string, string> = {
  foreign_key: "#2563eb",
  derivation: "#7c3aed",
  bridge_table: "#0d9488",
  fact_table: "#d97706",
  other: "#94a3b8",
};
const LINEAGE_EDGE_LABEL: Record<string, string> = {
  foreign_key: "外键",
  derivation: "血缘/加工",
  bridge_table: "桥接",
  fact_table: "事实",
  other: "关系",
};
const edgeColor = (t?: string) => LINEAGE_EDGE_COLOR[t ?? "other"] ?? LINEAGE_EDGE_COLOR.other;

/**
 * 血缘块（V3 S2）：轻量 SVG，无第三方依赖（沿用项目手绘 SVG 惯例，不引 g6）。
 * 三列布局——上游（指向中心的边源）在左，中心居中，下游/其余在右；1 跳邻域最贴切。
 * 边按 structure_type 着色，derivation=数据加工血缘。
 */
function ChatBiLineage({
  centerId,
  nodes,
  edges,
  truncated,
}: {
  centerId?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated?: boolean;
}) {
  if (!nodes.length) {
    return <div className="chatbi-lineage chatbi-lineage--empty">无血缘数据</div>;
  }
  const MAX_COL = 8;
  const center = nodes.find((n) => n.id === centerId) ?? nodes[0];
  const others = nodes.filter((n) => n.id !== center.id);
  const upIds = new Set(edges.filter((e) => e.target === center.id).map((e) => e.source));
  const up = others.filter((n) => upIds.has(n.id)).slice(0, MAX_COL);
  const down = others.filter((n) => !upIds.has(n.id)).slice(0, MAX_COL);

  const rowH = 46;
  const nodeW = 116;
  const nodeH = 30;
  const cols = Math.max(up.length, down.length, 1);
  const H = cols * rowH + 20;
  const width = 620;
  const leftX = 20;
  const centerX = width / 2 - nodeW / 2;
  const rightX = width - nodeW - 20;
  const colStartY = (len: number) => (H - len * rowH) / 2 + 8;

  const pos = new Map<string, { x: number; y: number }>();
  pos.set(center.id, { x: centerX, y: H / 2 - nodeH / 2 });
  up.forEach((n, i) => pos.set(n.id, { x: leftX, y: colStartY(up.length) + i * rowH }));
  down.forEach((n, i) => pos.set(n.id, { x: rightX, y: colStartY(down.length) + i * rowH }));

  const anchor = (id: string, side: "l" | "r") => {
    const p = pos.get(id);
    if (!p) return null;
    return { x: p.x + (side === "r" ? nodeW : 0), y: p.y + nodeH / 2 };
  };

  const drawn = edges.filter((e) => pos.has(e.source) && pos.has(e.target));

  const NodeBox = ({ n, isCenter }: { n: GraphNode; isCenter?: boolean }) => {
    const p = pos.get(n.id)!;
    const label = n.display_name || n.label || n.id;
    return (
      <g>
        <rect
          x={p.x}
          y={p.y}
          width={nodeW}
          height={nodeH}
          rx={6}
          fill={isCenter ? "var(--om-primary)" : "#ffffff"}
          stroke={isCenter ? "var(--om-primary)" : "var(--om-border)"}
        />
        <text
          x={p.x + nodeW / 2}
          y={p.y + nodeH / 2 + 4}
          textAnchor="middle"
          fontSize={12}
          fill={isCenter ? "#ffffff" : "var(--om-text-secondary)"}
        >
          {label.length > 9 ? `${label.slice(0, 9)}…` : label}
        </text>
      </g>
    );
  };

  return (
    <div className="chatbi-lineage">
      <div className="chatbi-lineage-head">
        血缘 · 上游 {up.length} · 下游 {down.length}
        {truncated ? "（已截断，仅展示部分邻域）" : ""}
      </div>
      <div style={{ overflowX: "auto" }}>
        <svg width={width} height={H}>
          {drawn.map((e) => {
            const srcSide = pos.get(e.source)!.x <= centerX ? "r" : "l";
            const tgtSide = pos.get(e.target)!.x < centerX ? "r" : "l";
            const a = anchor(e.source, srcSide);
            const b = anchor(e.target, tgtSide);
            if (!a || !b) return null;
            return (
              <g key={e.id}>
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={edgeColor(e.structure_type)}
                  strokeWidth={1.5}
                />
                <title>
                  {e.label}（{LINEAGE_EDGE_LABEL[e.structure_type ?? "other"] ?? "关系"}）
                </title>
              </g>
            );
          })}
          {up.map((n) => (
            <NodeBox key={n.id} n={n} />
          ))}
          {down.map((n) => (
            <NodeBox key={n.id} n={n} />
          ))}
          <NodeBox n={center} isCenter />
        </svg>
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
  select_skill: { icon: "🎯", verb: "选择技能" },
  update_plan: { icon: "📋", verb: "制定计划" },
  render_chart: { icon: "📊", verb: "生成图表" },
  analyze_result: { icon: "🔬", verb: "分析结果" },
  get_lineage: { icon: "🌊", verb: "查看血缘" },
  propose_draft: { icon: "🧩", verb: "拟建数提案" },
  propose_preference: { icon: "📌", verb: "拟记忆约定" },
  propose_action: { icon: "🛠️", verb: "拟数据任务提案" },
  get_task_status: { icon: "📶", verb: "查任务状态" },
};

/** 把 (工具 + 入参) 翻译成一句人话动作 + 图标。 */
function stepAction(step: ChatBiAgentStep): { icon: string; text: string } {
  const meta = STEP_TOOL_META[step.tool] ?? { icon: "•", verb: step.tool };
  const args = step.arguments as Record<string, unknown> | undefined;
  if (step.tool === "run_sql") {
    const sql = String(args?.sql ?? "")
      .replace(/\s+/g, " ")
      .trim();
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
  const toolCount = steps.filter((s) => s.kind !== "thought" && s.kind !== "repair").length;
  // 进行中/失败自动展开，其余默认折叠；用户手动点击后固定
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? (running || hasFailed);
  const headText = running
    ? `思考中 · 已执行 ${toolCount} 步`
    : `已执行 ${toolCount} 步工具编排${hasFailed ? " · 含失败" : ""}`;
  return (
    <div className="chatbi-steps">
      <button type="button" className="chatbi-steps-toggle" onClick={() => setManualOpen(!open)}>
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

function ResultTable({
  result,
  conversationId,
  messageId,
  blockId,
}: {
  result: ChatBiDataResult;
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
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
      <AckControl
        target={{
          conversationId,
          messageId,
          blockId,
          node: "data",
          // stage 叫 data_result 而不是 result——"result" 是**环名**（任务回执验收），
          // 拿它当数据环的场景名，日后按 stage 下钻分析时两者会混作一谈。
          stage: "data_result",
          summary: `查询结果（${rows.length} 行）`,
          chosen: { row_count: rows.length, truncated: result.truncated },
        }}
        label="数据结果是否符合预期？"
      />
    </div>
  );
}

function CaliberDecomposition({
  items,
  references,
}: {
  items: ChatBiCaliberItem[];
  references: ChatBiCaliberReference[];
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
                <div className="chatbi-caliber-item-desc">{item.description}</div>
              )}
            </div>
          </div>
        ))}
      </div>
      {references.length > 0 && (
        <div className="chatbi-caliber-hits">
          <span className="chatbi-caliber-hits-label">命中本体</span>
          <div className="chatbi-caliber-hits-row">
            {references.map((reference, ri) => (
              <CaliberRefChip key={ri} reference={reference} />
            ))}
          </div>
        </div>
      )}
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

function SqlBlock({
  sql,
  conversationId,
  messageId,
  blockId,
}: {
  sql: string;
  conversationId?: string;
  messageId?: string;
  blockId?: string;
}) {
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
        <button className="chatbi-sql-copy" onClick={() => void handleCopy()} type="button">
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="chatbi-sql-pre">
        <code>{highlightSql(sql)}</code>
      </pre>
      <AckControl
        target={{
          conversationId,
          messageId,
          blockId,
          node: "data",
          stage: "sql",
          summary: "生成的 SQL",
          chosen: { sql_preview: sql.slice(0, 200) },
        }}
        label="SQL 是否符合预期？"
      />
    </div>
  );
}
