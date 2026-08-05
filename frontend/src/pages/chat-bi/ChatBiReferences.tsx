import { Button, Space, Tag, message } from "antd";
import { AppstoreAddOutlined, AppstoreOutlined, DashboardOutlined, SafetyOutlined } from "@ant-design/icons";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "../../api";
import { ArtifactDetail } from "../../components/AgentsPanel";
import type {
  ChatBiAgentStep,
  ChatBiBlock,
  ChatBiCaliberItem,
  ChatBiCaliberKind,
  ChatBiCaliberReference,
  ChatBiClarification,
  ChatBiDataResult,
  ChatBiReference,
  GovernanceArtifact,
  GraphEdge,
  GraphNode,
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
  conversationId,
  onGenerateApp,
  onAddToDashboard,
  onClarify,
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
  // V3 S0：回答由渲染块序列投影而来。后端双写 blocks 优先；流式/旧消息由 answerToBlocks
  // 从扁平字段兜底（流式时正文取实时 content）。用户气泡仍是纯文本。
  const blocks: ChatBiBlock[] = isUser
    ? []
    : message.payload?.blocks?.length
      ? message.payload.blocks
      : answerToBlocks(message.payload, message.content);
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
        ) : isUser ? (
          <div className="chatbi-answer-wrap">
            <MarkdownLite content={message.content} />
          </div>
        ) : (
          <>
            {blocks.map((block) => (
              <BlockRenderer
                key={block.id}
                block={block}
                streaming={message.streaming}
                question={question}
                conversationId={conversationId}
                onClarify={onClarify}
              />
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
  onClarify,
}: {
  block: ChatBiBlock;
  streaming?: boolean;
  question?: string;
  conversationId?: string;
  onClarify?: (text: string) => void;
}) {
  switch (block.type) {
    case "steps":
      return <StepTrace steps={block.steps} />;
    case "markdown":
      return <MarkdownBlock content={block.content} streaming={streaming} />;
    case "mapping":
      return <MappingBlock variant={block.variant} items={block.items} references={block.references} />;
    case "sql":
      return <SqlBlock sql={block.sql} />;
    case "table":
      return (
        <ResultTable
          result={{
            columns: block.columns,
            rows: block.rows,
            truncated: block.truncated,
          }}
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
      return <DraftProposalBlock proposal={block.proposal} />;
    case "action_proposal":
      return <ActionProposalBlock proposal={block.proposal} conversationId={conversationId} />;
    case "preference_proposal":
      return <PreferenceProposalBlock proposal={block.proposal} />;
    case "task_status":
      return <TaskStatusBlock status={block.status} />;
    case "notice":
      return block.variant === "refused" ? <RefusedNotice /> : <MockNotice />;
    case "clarify":
      return (
        <ClarifyBlock
          clarification={block.clarification}
          question={question}
          onClarify={onClarify}
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
        <span className="chatbi-notice-desc">回答仅基于已发布本体可证实的内容；无法由本体证明的结论未作答。</span>
      </div>
    </div>
  );
}

function MockNotice() {
  return (
    <div className="chatbi-mock-hint">
      <Tag color="warning" style={{ borderRadius: 6 }}>Mock 模式</Tag>
      <span>未接入真实 LLM，已使用规则匹配回答。</span>
    </div>
  );
}

function ClarifyBlock({
  clarification,
  question,
  onClarify,
}: {
  clarification: ChatBiClarification;
  question?: string;
  onClarify?: (text: string) => void;
}) {
  // 澄清反问：正文即问题与候选项，这里把候选项做成可点击的追问，
  // 让用户一步接上，而不是自己再打一遍。
  return (
    <div className="chatbi-clarify">
      <div className="chatbi-clarify-q">{clarification.question}</div>
      {clarification.reason && (
        <div className="chatbi-clarify-why">{clarification.reason}</div>
      )}
      <div className="chatbi-clarify-options">
        {clarification.options.map((opt) => (
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
  );
}

function RefsRow({
  objects,
  logics,
}: {
  objects: ChatBiReference[];
  logics: ChatBiReference[];
}) {
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
}: {
  proposal: Extract<ChatBiBlock, { type: "draft_proposal" }>["proposal"];
}) {
  const navigate = useNavigate();
  const [state, setState] = useState<"idle" | "creating" | "done" | "error">("idle");
  const typeLabel = DRAFT_TYPE_LABEL[proposal.logic_type] ?? proposal.logic_type;
  const onConfirm = async () => {
    setState("creating");
    try {
      const created = await api.createBusinessLogic(proposal.create_payload);
      setState("done");
      navigate(`/business-logic/${created.id}`);
    } catch {
      setState("error");
    }
  };
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="gold" bordered={false}>建数提案</Tag>
        <span>新建{typeLabel}</span>
      </div>
      <div className="chatbi-draft-name">{proposal.display_name}</div>
      {proposal.description && <div className="chatbi-draft-desc">{proposal.description}</div>}
      <div className="chatbi-draft-note">
        确认后创建为草稿口径（待你补全表达式并发布），不会直接改动本体或数据。
      </div>
      <Space>
        <Button
          type="primary"
          size="small"
          loading={state === "creating"}
          disabled={state === "done"}
          onClick={() => void onConfirm()}
        >
          {state === "done" ? "已创建，跳转中…" : "去确认创建"}
        </Button>
        {state === "error" && (
          <span className="chatbi-draft-error">创建失败，请重试或到口径页手动创建。</span>
        )}
      </Space>
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
}: {
  proposal: Extract<ChatBiBlock, { type: "preference_proposal" }>["proposal"];
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
    } catch {
      setState("error");
    }
  };
  return (
    <div className="chatbi-draft">
      <div className="chatbi-draft-head">
        <Tag color="cyan" bordered={false}>记忆提案</Tag>
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
        <Tag color="volcano" bordered={false}>结果分析</Tag>
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
                {c.trend.change_pct != null && ` ${c.trend.change_pct > 0 ? "+" : ""}${c.trend.change_pct}%`}
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
        <Tag color="purple" bordered={false}>分析计划</Tag>
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
function useArtifactDrawer() {
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const STEP_LABEL: Record<string, string> = { validate: "校验", confirm: "确认", execute: "执行" };
  const onStep = async (
    step: "validate" | "confirm" | "execute",
    artifact: GovernanceArtifact,
  ) => {
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
  const node = (
    <ArtifactDetail artifact={detail} busy={busy} onClose={() => setDetail(null)} onStep={onStep} />
  );
  return { open: setDetail, node };
}

const ACTION_KIND_LABEL: Record<string, string> = {
  materialize: "物化",
  sync: "同步",
  transform: "加工",
};

const TASK_STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  validated: "blue",
  confirmed: "gold",
  executing: "processing",
  succeeded: "green",
  failed: "red",
};

/**
 * 数据任务提案块（P0）：Data Agent 只出提案（不执行、不写库）；点「去校验并执行」才由用户动作
 * POST /api/agents/draft 建一条治理制品，随后在复用的 ArtifactDetail 抽屉里走
 * 校验→看 dry-run 差异→确认→执行。写侧全程 publisher 门控 + 人工确认，agent 不碰。
 */
function ActionProposalBlock({
  proposal,
  conversationId,
}: {
  proposal: Extract<ChatBiBlock, { type: "action_proposal" }>["proposal"];
  conversationId?: string;
}) {
  const [drafting, setDrafting] = useState(false);
  const { open, node } = useArtifactDrawer();
  const kindLabel = ACTION_KIND_LABEL[proposal.kind] ?? proposal.kind;
  const onConfirm = async () => {
    setDrafting(true);
    try {
      const artifact = await api.draftArtifact(proposal.draft_payload);
      // P1：把本会话与该任务关联，后续可免 id 追踪。best-effort，失败不阻断主流程。
      if (conversationId) {
        void api
          .linkChatBiTask(conversationId, {
            artifact_id: artifact.id,
            kind: proposal.kind,
            intent: proposal.intent,
          })
          .catch(() => {});
      }
      open(artifact);
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
        <Tag color="geekblue" bordered={false}>数据任务提案</Tag>
        <span>新建{kindLabel}任务</span>
      </div>
      <div className="chatbi-draft-name">{proposal.intent}</div>
      {proposal.context && Object.keys(proposal.context).length > 0 && (
        <div className="chatbi-draft-desc">
          <code>{JSON.stringify(proposal.context)}</code>
        </div>
      )}
      <div className="chatbi-draft-note">
        点击后创建治理制品，并在弹窗里过「校验 → dry-run 差异 → 人工确认 → 执行」；不会自动执行，也不直接改动数据。
      </div>
      <Space>
        <Button type="primary" size="small" loading={drafting} onClick={() => void onConfirm()}>
          去校验并执行
        </Button>
      </Space>
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
}: {
  status: Extract<ChatBiBlock, { type: "task_status" }>["status"];
}) {
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const { open, node } = useArtifactDrawer();
  const tasks = status.tasks ?? [];
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
          {t.receipt_summary && (
            <span className="chatbi-task-receipt">{t.receipt_summary}</span>
          )}
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
}: {
  variant: "inline" | "caliber";
  items: ChatBiCaliberItem[];
  references: ChatBiCaliberReference[];
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
      </div>
    );
  }
  return <CaliberDecomposition items={items} references={references} />;
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
                  <text x={bx + barW / 2} y={by - 4} textAnchor="middle" fontSize={11} fill="var(--om-text-secondary)">
                    {p.value}
                  </text>
                  <text x={bx + barW / 2} y={H + padT + 18} textAnchor="middle" fontSize={11} fill="var(--om-text-tertiary)">
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
                  <text x={px(i)} y={H + padT + 18} textAnchor="middle" fontSize={11} fill="var(--om-text-tertiary)">
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
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={edgeColor(e.structure_type)} strokeWidth={1.5} />
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
                <div className="chatbi-caliber-item-desc">
                  {item.description}
                </div>
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
