import type { RoleSignals } from "../types";

export type { RoleSignals };

// 对象角色（table_role）与「判定依据」展示辅助。
//
// 后端 object_classifier 不依赖表名，用结构/内容/拓扑/语义信号给每张表打分并分类，
// 产出两份可展示的东西：
//   - role_reason：人类可读理由，子句以「；」分隔（**不再**编码复核状态——
//     复核状态是 object_types.needs_review 独立列，读它，别去 role_reason 里找前缀）；
//   - role_signals：结构化证据 JSON（score / needs_review / signals{...}）。
// 这里把它们规整成前端可直接渲染的结构，供角色徽章与详情「判定依据」面板复用，
// 避免在多处重复散落映射。阈值与 backend/app/services/object_classifier.py 对齐。

/** 存量 role_reason 里可能还残留的旧前缀，仅用于展示时剥除。 */
export const REVIEW_MARK = "待复核";

/** 业务环节最小成员数（与后端 object_classifier._MIN_SEGMENT_SIZE 对齐）。 */
export const MIN_SEGMENT_SIZE = 3;

export interface RoleMeta {
  label: string;
  color: string; // antd Tag color
  short: string;
  cls: string;
}

/** 四类对象角色的展示元数据（与后端 ROLE_* 常量一一对应）。 */
export const ROLE_META: Record<string, RoleMeta> = {
  business_object: { label: "业务对象", color: "green", short: "业务", cls: "business" },
  data_table: { label: "数据表", color: "blue", short: "数据", cls: "data" },
  bridge: { label: "关系表", color: "purple", short: "关系", cls: "bridge" },
  technical: { label: "技术/系统表", color: "default", short: "技术", cls: "technical" },
};

/** 角色下拉/筛选选项（顺序与 ROLE_META 一致）。 */
export const ROLE_OPTIONS = [
  { label: "业务对象", value: "business_object" },
  { label: "数据表", value: "data_table" },
  { label: "关系表", value: "bridge" },
  { label: "技术/系统表", value: "technical" },
];

export function getRoleMeta(role?: string): RoleMeta {
  return ROLE_META[role || "business_object"] ?? ROLE_META.business_object;
}

/**
 * 按「；」分句，但**括号内的分号不算分隔符**。
 *
 * 分歧理由是嵌套的：`LLM 判为X（理由；理由）；启发式判为Y（理由；理由）`——
 * 裸 split 会把括号里的半句切出来单独成条，读者看到的是「启发式判为业务对象（关系表未能
 * 塌缩…单列业务主键」和「描述性属性占比 100%」两条互不成句的碎片。
 */
function splitTopLevel(text: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let buffer = "";
  for (const ch of text) {
    if (ch === "（" || ch === "(" || ch === "【" || ch === "[") depth += 1;
    else if (ch === "）" || ch === ")" || ch === "】" || ch === "]") depth = Math.max(0, depth - 1);
    if ((ch === "；" || ch === ";") && depth === 0) {
      parts.push(buffer);
      buffer = "";
      continue;
    }
    buffer += ch;
  }
  parts.push(buffer);
  return parts.map((s) => s.trim()).filter(Boolean);
}

/**
 * 把 role_reason 拆成可扫读的证据条目：剥离 [待复核] 前缀，按「；」/「;」分条去空。
 * 让复核者不用读一整段 prose，而是逐条看判据。
 */
export function reasonClauses(reason?: string | null): string[] {
  if (!reason) return [];
  const stripped = reason.replace(/^\s*\[?待复核\]?\s*/, "").trim();
  if (!stripped) return [];
  return splitTopLevel(stripped);
}

/** 单条信号推向哪个结论。 */
export type SignalDirection = "business" | "nonbusiness" | "neutral";

export interface SignalItem {
  key: string;
  label: string;
  value: string;
  direction: SignalDirection;
  /**
   * 这条信号有没有判别力。
   *
   * 中性 **且** 取零值的信号（度量占比 0%、技术字段 0%、技术信号分 0.0）既不支持也不
   * 反对当前判定，却占着判据栏三分之一的行。它们不是没意义——「没有反面信号」本身是
   * 一种信息——但那句话说一次就够，不值三行。这里标出来，界面折成一行摘要。
   */
  notable: boolean;
}

export interface DecisionEvidence {
  score?: number;
  items: SignalItem[];
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const num = (v: unknown) => (typeof v === "number" ? v : Number(v ?? 0));

/**
 * 把 role_signals 规整为带方向的信号清单，供「判定依据」面板逐条渲染。
 * 只输出存在的 key；阈值与 object_classifier 打分规则对齐，方向标注帮助复核者
 * 一眼看出每个信号在把这张表推向「业务对象」还是「非业务对象」。
 */
export function describeSignals(
  rs?: RoleSignals | null,
  /** 展示上下文：有真实板块归属时，用它替掉容易误读的第一遍聚类簇规模。 */
  ctx?: { segmentName?: string | null },
): DecisionEvidence {
  const s = rs?.signals ?? {};
  const items: SignalItem[] = [];
  const has = (k: string) => s[k] !== undefined && s[k] !== null;

  if (has("pk_columns")) {
    const n = num(s.pk_columns);
    items.push({
      key: "pk_columns",
      label: "主键列数",
      value: String(n),
      direction: n === 1 ? "business" : n >= 2 ? "nonbusiness" : "neutral",
      notable: true,
    });
  }
  if (has("fk_in_degree")) {
    const n = num(s.fk_in_degree);
    items.push({
      key: "fk_in_degree",
      label: "被引用（外键入度）",
      value: n >= 3 ? `${n}（枢纽）` : String(n),
      direction: n >= 1 ? "business" : "neutral",
      notable: true,
    });
  }
  if (has("distinct_fk_targets")) {
    const n = num(s.distinct_fk_targets);
    items.push({
      key: "distinct_fk_targets",
      label: "引用不同实体数",
      value: String(n),
      direction: n >= 2 ? "nonbusiness" : "neutral",
      notable: n > 0,
    });
  }
  if (has("own_attr_count")) {
    const n = num(s.own_attr_count);
    items.push({
      key: "own_attr_count",
      label: "自有属性数",
      value: String(n),
      direction: n >= 1 ? "business" : "nonbusiness",
      notable: true,
    });
  }
  if (has("descriptive_ratio")) {
    const v = num(s.descriptive_ratio);
    items.push({
      key: "descriptive_ratio",
      label: "描述性字段占比",
      value: pct(v),
      direction: v >= 0.4 ? "business" : "neutral",
      notable: v > 0,
    });
  }
  if (has("measure_ratio")) {
    const v = num(s.measure_ratio);
    items.push({
      key: "measure_ratio",
      label: "度量字段占比",
      value: pct(v),
      direction: v >= 0.3 ? "nonbusiness" : "neutral",
      notable: v > 0,
    });
  }
  if (has("technical_ratio")) {
    const v = num(s.technical_ratio);
    items.push({
      key: "technical_ratio",
      label: "技术字段占比",
      value: pct(v),
      direction: v >= 0.25 ? "nonbusiness" : "neutral",
      notable: v > 0,
    });
  }
  if (has("tech_score")) {
    const v = num(s.tech_score);
    items.push({
      key: "tech_score",
      label: "技术信号分",
      value: v.toFixed(1),
      direction: v >= 2 ? "nonbusiness" : "neutral",
      notable: v > 0,
    });
  }
  if (has("connected") || has("isolated")) {
    const connected = has("connected") ? Boolean(s.connected) : !s.isolated;
    items.push({
      key: "connected",
      label: "图连通性",
      value: connected ? "连通" : "孤立",
      direction: connected ? "business" : "nonbusiness",
      // 连通是常态，说了等于没说；孤立才是要看的那一条。
      notable: !connected,
    });
  }
  if (has("is_child_table") && Boolean(s.is_child_table)) {
    items.push({
      key: "is_child_table",
      label: "明细/子表",
      value: "是",
      direction: "nonbusiness",
      notable: true,
    });
  }
  if (has("fact_name_token")) {
    items.push({
      key: "fact_name_token",
      label: "事件动词命名",
      value: `含「${String(s.fact_name_token)}」`,
      direction: "nonbusiness",
      notable: true,
    });
  }
  if (has("segment_size")) {
    const n = num(s.segment_size);
    const inSegment = n >= MIN_SEGMENT_SIZE;
    // 这个数是**第一遍聚类**的簇规模（分类器的输入），不是落库板块的成员数。
    // 直接显示「业务环节成员数 125」会和界面上写着 11 个成员的板块公然打架，
    // 所以有真实板块名时就显示板块名，把这条信号还原成它真正的意思：
    // 「这张表长在一个成形的业务簇里」。
    items.push({
      key: "segment_size",
      label: ctx?.segmentName ? "所属板块" : "所在关系簇",
      value: ctx?.segmentName
        ? ctx.segmentName
        : inSegment
          ? `${n} 个成员`
          : `${n} 个成员（未成簇）`,
      direction: inSegment ? "business" : "nonbusiness",
      notable: true,
    });
  }

  return { score: typeof rs?.score === "number" ? rs.score : undefined, items };
}

/**
 * 按「反证优先」重排信号：与当前判定相反的排最前。
 *
 * 决定复核者要不要改判的是反证，不是又一个支持它的数字。默认的插入顺序
 * （主键→入度→占比…）是采集顺序，跟「该先看哪条」没有关系。
 */
export function orderSignalsForVerdict(items: SignalItem[], role?: string | null): SignalItem[] {
  const counter: SignalDirection = role === "business_object" ? "nonbusiness" : "business";
  const rank = (d: SignalDirection) => (d === counter ? 0 : d === "neutral" ? 2 : 1);
  return [...items].sort((a, b) => rank(a.direction) - rank(b.direction));
}

/** 综合得分对阈值的定性说明（阈值 2.0 与后端一致）。 */
export const ROLE_SCORE_THRESHOLD = 2.0;

// ---------------------------------------------------------------------------
// 「机器为什么把这条推给我看」——审核界面的第一信息
//
// 队列里的每一条都待复核，所以「待复核」本身没有信息量；有信息量的是**机器自己
// 报告的不确定性**：两个判定源打架、证据本身缺失、分类器推翻了自己。这些事实今天
// 全都压在 role_reason 那一大段散文里（实测 866 条待复核中 69% 是分歧、65% 带证据
// 缺口），复核者得逐条点开读到末尾才能发现该看哪一条。
//
// 下面把这些事实从散文里结构化出来，供列表、组头、判据栏共用同一套口径。
// ---------------------------------------------------------------------------

/** 得分「证据充分」的下界（与后端 review_queue._BAND_STRONG 对齐）。 */
export const SCORE_BAND_STRONG = 3.0;

/** 低于此置信度视为「机器自己也没把握」（分歧被固定写成 0.5，重判 0.6）。 */
export const LOW_CONFIDENCE = 0.7;

export type ScoreBand = "strong" | "near" | "weak" | "unknown";

/** 得分分带（与后端 review_queue.score_band 同规则）。 */
export function scoreBand(score?: number | null): ScoreBand {
  if (typeof score !== "number") return "unknown";
  if (score >= SCORE_BAND_STRONG) return "strong";
  if (score >= ROLE_SCORE_THRESHOLD) return "near";
  return "weak";
}

/** 分带的展示元数据（标签与后端 BAND_LABELS 一致）。 */
export const BAND_META: Record<ScoreBand, { label: string; color: string }> = {
  strong: { label: "证据充分", color: "green" },
  near: { label: "刚过线", color: "gold" },
  weak: { label: "未过线", color: "orange" },
  unknown: { label: "无评分", color: "default" },
};

/** 复核旗标的轻重：alert = 机器自己拿不准，warn = 证据不足，info = 单条反向信号。 */
export type ReviewFlagTone = "alert" | "warn" | "info";

export interface ReviewFlag {
  key: string;
  /** chip 上的短标签，控制在 6 字以内，扫读用。 */
  label: string;
  tone: ReviewFlagTone;
  /** 悬停/展开时的完整说明。 */
  detail?: string;
}

/** 启发式与 LLM 分歧时，两方各自的判定与 LLM 报告的证据缺口。 */
export interface RoleDisagreement {
  llmRole: string;
  llmReason?: string;
  heuristicRole: string;
  heuristicReason?: string;
  evidenceGap?: string;
}

const DISAGREE_PREFIX = "启发式↔LLM 角色分歧";

/** 后端 _resolve_role 写进 role_reason 的分歧句式里出现的角色词，转成本地标签。 */
function normalizeRoleLabel(text: string): string {
  const raw = text.trim();
  return ROLE_META[raw]?.label ?? raw;
}

/**
 * 从 role_reason 还原分歧结构。
 *
 * 句式由 backend/app/services/draft_generator.py `_resolve_role` 生成：
 * `启发式↔LLM 角色分歧：LLM 判为X（理由）；启发式判为Y（理由）；证据缺口：Z`。
 * 解析失败就返回 null——宁可退回原文展示，也不猜。
 */
export function parseDisagreement(reason?: string | null): RoleDisagreement | null {
  if (!reason || !reason.includes(DISAGREE_PREFIX)) return null;
  const clauses = reasonClauses(reason);
  let llmRole = "";
  let llmReason: string | undefined;
  let heuristicRole = "";
  let heuristicReason: string | undefined;
  let evidenceGap: string | undefined;

  const readVerdict = (text: string): [string, string | undefined] => {
    const match = text.match(/^([^（(]+)(?:[（(]([\s\S]*)[)）])?$/);
    if (!match) return [text.trim(), undefined];
    return [normalizeRoleLabel(match[1]), match[2]?.trim() || undefined];
  };

  for (const clause of clauses) {
    const body = clause.replace(new RegExp(`^${DISAGREE_PREFIX}[：:]\\s*`), "");
    if (body.startsWith("LLM 判为")) {
      [llmRole, llmReason] = readVerdict(body.slice("LLM 判为".length));
    } else if (body.startsWith("启发式判为")) {
      [heuristicRole, heuristicReason] = readVerdict(body.slice("启发式判为".length));
    } else if (/^证据缺口[：:]/.test(body)) {
      evidenceGap = body.replace(/^证据缺口[：:]\s*/, "").trim() || undefined;
    }
  }
  if (!llmRole && !heuristicRole) return null;
  return { llmRole, llmReason, heuristicRole, heuristicReason, evidenceGap };
}

// ---------------------------------------------------------------------------
// role_reason 的完整解析
//
// role_reason 是一段拼起来的散文，里面混着三种价值完全不同的东西：
//
// 1. **LLM 的语义判读**——「MySQL sys schema 锁等待诊断视图，记录线程间锁阻塞详情，
//    非业务实体」。这是全屏唯一一句说清「这张表到底是干什么的」的话，是复核者最想读的。
//    它出现在两种句式里：分歧时的 `LLM 判为X（…）`，一致时的 `采纳 LLM 语义判定：X（…）`。
//    早先只解析了前者，后者被埋在判定说明的项目符号里，跟流程记账混在一起。
// 2. **结构判据**——主键/入度/占比这些启发式子句，与信号表同源。
// 3. **流程记账**——「与 LLM 同属非业务对象，差异不影响发布」「信号不足，暂按业务对象
//    保留，待人工确认」「启发式证据不足未作判定」。它们描述的是流水线状态，不是这张表的
//    事实，对「判成什么」零贡献，占着行还把前两类挤下去。
//
// 下面把三者分开，界面只呈现前两类。
// ---------------------------------------------------------------------------

/** 纯流程记账的子句：讲流水线怎么走的，不讲这张表是什么。 */
const BOOKKEEPING_PATTERNS = [
  /差异不影响发布/,
  /信号不足，暂按.*保留/,
  /^启发式证据不足未作判定$/,
  /待人工确认$/,
];

function isBookkeeping(clause: string): boolean {
  return BOOKKEEPING_PATTERNS.some((re) => re.test(clause));
}

export interface RoleReasonBreakdown {
  /** LLM 读出的业务含义——判据栏的头条。 */
  llmReading?: string;
  /** LLM 给出的角色（本地标签）。 */
  llmRole?: string;
  /** 启发式给出的角色（本地标签）。 */
  heuristicRole?: string;
  /** 结构判据子句（已剔除流程记账）。 */
  heuristicClauses: string[];
  /** LLM 报告的证据缺口：告诉复核者机器为什么心虚、该去补看什么。 */
  evidenceGap?: string;
  /** 两个判定源打架。 */
  disagreement: boolean;
}

/**
 * `X（Y）` → [X, Y]；没有括号时把逗号后的话也切掉。
 *
 * 后者是必须的：`启发式判为data_table，与 LLM 同属非业务对象，差异不影响发布` 这句里
 * 角色只到第一个逗号，整串拿去当角色名会把一行流程记账当成判定结果显示出来。
 */
function splitParenthetical(text: string): [string, string | undefined] {
  const match = text.match(/^([^（(]+)(?:[（(]([\s\S]*)[)）])?$/);
  if (!match) return [text.trim(), undefined];
  const head = match[1].split(/[，,]/)[0].trim();
  return [head, match[2]?.trim() || undefined];
}

/**
 * 把 role_reason 拆成「LLM 语义判读 / 结构判据 / 证据缺口」三份。
 *
 * 句式来自 backend/app/services/draft_generator.py 的 `_resolve_role`。解析不出来的
 * 子句一律落进 heuristicClauses 原样展示——宁可多显示一条，也不猜、不丢。
 */
export function parseRoleReason(reason?: string | null): RoleReasonBreakdown {
  const out: RoleReasonBreakdown = { heuristicClauses: [], disagreement: false };
  if (!reason) return out;
  out.disagreement = reason.includes(DISAGREE_PREFIX);

  for (const raw of reasonClauses(reason)) {
    const body = raw.replace(new RegExp(`^${DISAGREE_PREFIX}[：:]\\s*`), "").trim();
    if (!body) continue;

    if (/^证据缺口[：:]/.test(body)) {
      out.evidenceGap = body.replace(/^证据缺口[：:]\s*/, "").trim() || undefined;
      continue;
    }
    if (body.startsWith("采纳 LLM 语义判定")) {
      const [role, reading] = splitParenthetical(
        body.replace(/^采纳 LLM 语义判定[：:]\s*/, ""),
      );
      out.llmRole = normalizeRoleLabel(role);
      out.llmReading = reading;
      continue;
    }
    if (body.startsWith("LLM 判为")) {
      const [role, reading] = splitParenthetical(body.slice("LLM 判为".length));
      out.llmRole = normalizeRoleLabel(role);
      out.llmReading = reading;
      continue;
    }
    if (body.startsWith("启发式判为")) {
      const [role, inner] = splitParenthetical(body.slice("启发式判为".length));
      out.heuristicRole = normalizeRoleLabel(role);
      // 括号里往往嵌着完整的结构判据，拆开后与顶层子句一视同仁。
      if (inner) out.heuristicClauses.push(...splitTopLevel(inner).filter((c) => !isBookkeeping(c)));
      continue;
    }
    if (body.startsWith("启发式证据不足未作判定")) {
      const [, inner] = splitParenthetical(body);
      if (inner) out.heuristicClauses.push(...splitTopLevel(inner).filter((c) => !isBookkeeping(c)));
      continue;
    }
    if (!isBookkeeping(body)) out.heuristicClauses.push(body);
  }
  return out;
}

/** 反向信号 → chip 短语。只给 direction=nonbusiness 的信号，正向信号不进旗标。 */
const COUNTER_CHIP: Record<string, (item: SignalItem) => string> = {
  pk_columns: (i) => `复合主键 ${i.value}`,
  distinct_fk_targets: (i) => `引用 ${i.value} 实体`,
  own_attr_count: () => "无自有属性",
  measure_ratio: (i) => `度量 ${i.value}`,
  technical_ratio: (i) => `技术字段 ${i.value}`,
  tech_score: () => "技术信号强",
  connected: () => "图上孤立",
  is_child_table: () => "明细子表",
  fact_name_token: (i) => `事件动词${i.value.replace(/^含/, "")}`,
  segment_size: () => "未成环节",
};

/** 判定摘要：一句话说清「机器判成什么、有多少把握」。 */
export interface RoleVerdict {
  role: string;
  meta: RoleMeta;
  confidence?: number;
  score?: number;
  band: ScoreBand;
  bandLabel: string;
}

export interface ReviewSubject {
  table_role?: string;
  role_confidence?: number;
  role_reason?: string;
  role_signals?: RoleSignals | null;
}

export function roleVerdict(obj: ReviewSubject): RoleVerdict {
  const score = typeof obj.role_signals?.score === "number" ? obj.role_signals.score : undefined;
  const band = scoreBand(score);
  return {
    role: obj.table_role || "business_object",
    meta: getRoleMeta(obj.table_role),
    confidence: obj.role_confidence,
    score,
    band,
    bandLabel: BAND_META[band].label,
  };
}

/**
 * 这条为什么需要人看——按「机器有多不确定」从重到轻排列。
 *
 * 顺序即优先级：分歧/自我推翻（机器承认自己拿不准）→ 证据缺口/低置信 → 单条反向信号。
 * 列表里只展示前几个，所以顺序不是装饰。
 */
export function reviewFlags(obj: ReviewSubject): ReviewFlag[] {
  const flags: ReviewFlag[] = [];
  const disagreement = parseDisagreement(obj.role_reason);
  if (disagreement) {
    flags.push({
      key: "disagree",
      label: "机器分歧",
      tone: "alert",
      detail: `LLM 判为${disagreement.llmRole || "?"}，启发式判为${disagreement.heuristicRole || "?"}`,
    });
  }
  const from = obj.role_signals?.reclassified_from;
  if (typeof from === "string" && from) {
    flags.push({
      key: "reclassified",
      label: "机器改判",
      tone: "alert",
      detail: `原判${getRoleMeta(from).label} → 改判${
        getRoleMeta(obj.role_signals?.role || obj.table_role).label
      }`,
    });
  }
  // 证据缺口不只在分歧时出现：LLM 判定被直接采纳时同样会写「证据缺口：无列注释…」，
  // 而那正是复核者最该知道的一句——机器是靠表名猜的。原来只在分歧分支里取，
  // 于是大量「采纳 LLM」的条目把这条警示整个丢了。
  const evidenceGap = disagreement?.evidenceGap ?? parseRoleReason(obj.role_reason).evidenceGap;
  if (evidenceGap) {
    flags.push({
      key: "evidence_gap",
      label: "证据缺口",
      tone: "warn",
      detail: evidenceGap,
    });
  }
  // 分歧的置信度是后端固定写死的 0.5，再报一次「低置信」是同一件事说两遍。
  if (
    !disagreement &&
    typeof obj.role_confidence === "number" &&
    obj.role_confidence < LOW_CONFIDENCE
  ) {
    flags.push({
      key: "low_confidence",
      label: `低置信 ${Math.round(obj.role_confidence * 100)}%`,
      tone: "warn",
    });
  }
  for (const item of describeSignals(obj.role_signals).items) {
    if (item.direction !== "nonbusiness") continue;
    const chip = COUNTER_CHIP[item.key];
    if (!chip) continue;
    flags.push({
      key: item.key,
      label: chip(item),
      tone: "info",
      detail: `${item.label}：${item.value}`,
    });
  }
  return flags;
}

const TONE_RANK: Record<ReviewFlagTone, number> = { alert: 3, warn: 2, info: 1 };

export interface FlagSummary {
  /** 组内绝大多数成员共有的旗标（连同覆盖数），组头一次说清，行内不必重复强调。 */
  common: Array<{ flag: ReviewFlag; count: number }>;
  /**
   * 共性旗标的 key 集合。行内**整条略去**——它对「这组里该看谁」零信息量
   * （组头已经写了 26/29），留在行里只会把真正的差异淹掉。
   */
  commonKeys: Set<string>;
  /** 例外：带有**非共性**的 alert/warn 旗标——这才是组里真正要单独看的那几条。 */
  exceptionIds: string[];
  size: number;
}

/**
 * 把一组成员的旗标压成「共性 + 例外」。
 *
 * 成组审核的价值在于共性：29 张 InnoDB 系统表全是同一种分歧，说一次即可整组处置；
 * 而 11 张销售对象里只有 1 张是机器自我推翻——那一张必须自己跳出来。
 * 全组同旗时逐行标红等于没标，所以共性交给组头，行内只强调差异。
 */
export function summarizeFlags(entries: Array<{ id: string; flags: ReviewFlag[] }>): FlagSummary {
  const size = entries.length;
  const counts = new Map<string, { flag: ReviewFlag; count: number }>();
  for (const entry of entries) {
    // 同一条里同 key 只计一次。
    const seen = new Set<string>();
    for (const flag of entry.flags) {
      if (seen.has(flag.key)) continue;
      seen.add(flag.key);
      const hit = counts.get(flag.key);
      if (hit) hit.count += 1;
      else counts.set(flag.key, { flag, count: 1 });
    }
  }
  // 六成以上成员都有 = 这组的共同底色，归组头。单成员组同理：它自己就是「全组」，
  // 与其把同一句话在组头和行内各写一遍，不如行内留白、组头说全。
  const threshold = Math.max(1, Math.ceil(size * 0.6));
  const common = [...counts.values()]
    .filter((entry) => entry.count >= threshold)
    .sort((a, b) => b.count - a.count || TONE_RANK[b.flag.tone] - TONE_RANK[a.flag.tone]);
  const commonKeys = new Set(common.map((entry) => entry.flag.key));
  const exceptionIds = entries
    .filter((entry) => entry.flags.some((f) => f.tone !== "info" && !commonKeys.has(f.key)))
    .map((entry) => entry.id);
  return { common, commonKeys, exceptionIds, size };
}

/** 例外排前面：本组要单独看的那几条不该藏在第 11 行。 */
export function riskRank(flags: ReviewFlag[], commonKeys: Set<string>): number {
  let rank = 0;
  for (const flag of flags) {
    const weight = TONE_RANK[flag.tone];
    rank += commonKeys.has(flag.key) ? 0 : weight * 10;
  }
  return rank;
}
