import type {
  ChatBiAnswer,
  ChatBiBlock,
  ChatBiCaliberItem,
  ChatBiCaliberReference,
  ChatBiConversation,
  ChatBiReference,
} from "../../types";

export interface ChatMessage {
  /**
   * 消息的库 id。决策留痕用它把「人点了什么」锚到具体某一轮。
   * 仅在从历史加载时有值——流式刚产出的消息尚未落库，此时留痕只锚到会话。
   */
  id?: string;
  role: "user" | "assistant";
  content: string;
  payload?: ChatBiAnswer;
  pending?: boolean;
  /** 正在逐字流式产出最终答案（用于末尾闪烁光标）。 */
  streaming?: boolean;
  error?: boolean;
}

/**
 * 口径卡形态（须与后端 chat_bi_blocks._mapping_variant 一致）：
 * 有口径展开轨迹 → 完整口径卡；只有「命中本体」→ 内联 chip。
 */
function mappingVariant(items: ChatBiCaliberItem[]): "inline" | "caliber" {
  return items.length ? "caliber" : "inline";
}

/**
 * 「命中本体」——去重的可跳转引用（须与后端 chat_bi_blocks._caliber_hits 一致）。
 * 顺序=口径展开引用的口径 → 命中对象 → 命中逻辑；按 (kind, id|name) 去重。
 */
function caliberHits(
  caliber: ChatBiCaliberItem[],
  objects: ChatBiReference[],
  logics: ChatBiReference[],
): ChatBiCaliberReference[] {
  const hits: ChatBiCaliberReference[] = [];
  const seen = new Set<string>();
  const add = (kind: ChatBiCaliberReference["kind"], ref: Partial<ChatBiCaliberReference>) => {
    const key = `${kind}:${ref.id ?? ref.name ?? ""}`;
    if (key === `${kind}:` || seen.has(key)) return;
    seen.add(key);
    hits.push({ ...ref, kind });
  };
  for (const item of caliber) {
    for (const ref of item.references ?? []) add(ref.kind ?? "business_logic", ref);
  }
  for (const obj of objects) add("object_type", obj);
  for (const logic of logics) add("business_logic", logic);
  return hits;
}

/**
 * 把（可能是旧的、无 blocks 的）终态 payload 投影成渲染块序列，供块渲染器兜底。
 * 与后端 `answer_to_blocks` 规则一致——改一处记得同步另一处。
 * `content` 用于流式：此时 payload.answer 仍为空，正文取实时流式的 content。
 */
export function answerToBlocks(payload: ChatBiAnswer | undefined, content?: string): ChatBiBlock[] {
  const blocks: ChatBiBlock[] = [];
  let n = 0;
  const id = () => `b${n++}`;

  if (!payload) {
    if (content) blocks.push({ id: id(), type: "markdown", content });
    return blocks;
  }

  const steps = payload.steps ?? [];
  if (steps.length) blocks.push({ id: id(), type: "steps", steps });

  if (payload.grounding_refused) {
    blocks.push({ id: id(), type: "notice", level: "warning", variant: "refused" });
  }

  if (payload.clarification) {
    blocks.push({ id: id(), type: "clarify", clarification: payload.clarification });
  } else if (payload.form_request) {
    // P6：交互表单出口——出可填写表单块，替代正文（与后端 answer_to_blocks 一致）。
    blocks.push({ id: id(), type: "form", form: payload.form_request });
  } else {
    const md = content ?? payload.answer ?? "";
    if (md) blocks.push({ id: id(), type: "markdown", content: md });
  }

  const caliber = payload.caliber_decomposition ?? [];
  const objects = payload.referenced_objects ?? [];
  const logics = payload.referenced_logics ?? [];
  // 口径映射块：口径展开轨迹（items）+ 一行去重的「命中本体」（references）。
  // 「命中本体」已并入本块，不再另发底部 refs 块——消除对象列举的重复。
  const hits = caliberHits(caliber, objects, logics);
  if (caliber.length || hits.length) {
    blocks.push({
      id: id(),
      type: "mapping",
      variant: mappingVariant(caliber),
      items: caliber,
      references: hits,
    });
  }

  if (payload.suggested_sql) blocks.push({ id: id(), type: "sql", sql: payload.suggested_sql });

  const dr = payload.data_result;
  if (dr && (dr.rows?.length ?? 0) > 0) {
    blocks.push({
      id: id(),
      type: "table",
      columns: dr.columns ?? [],
      rows: dr.rows ?? [],
      truncated: dr.truncated,
    });
  }

  return blocks;
}

export type TimeGroup = "pinned" | "today" | "yesterday" | "thisWeek" | "thisMonth" | "older";

export function getTimeGroup(conv: ChatBiConversation): TimeGroup {
  if (conv.is_pinned) return "pinned";
  const now = new Date();
  const d = new Date(conv.updated_at);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
  const startOfWeek = new Date(startOfToday.getTime() - startOfToday.getDay() * 86400000);
  const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);

  if (d >= startOfToday) return "today";
  if (d >= startOfYesterday) return "yesterday";
  if (d >= startOfWeek) return "thisWeek";
  if (d >= startOfMonth) return "thisMonth";
  return "older";
}

export const TIME_GROUP_LABEL: Record<TimeGroup, string> = {
  pinned: "置顶",
  today: "今天",
  yesterday: "昨天",
  thisWeek: "本周",
  thisMonth: "本月",
  older: "更早",
};

export const TIME_GROUP_ORDER: TimeGroup[] = [
  "pinned",
  "today",
  "yesterday",
  "thisWeek",
  "thisMonth",
  "older",
];

export function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

export const EMPTY_DEPS: unknown[] = [];

const SQL_KEYWORDS = new Set([
  "SELECT",
  "FROM",
  "WHERE",
  "GROUP BY",
  "ORDER BY",
  "HAVING",
  "LIMIT",
  "OFFSET",
  "JOIN",
  "LEFT JOIN",
  "RIGHT JOIN",
  "INNER JOIN",
  "OUTER JOIN",
  "FULL JOIN",
  "ON",
  "AS",
  "AND",
  "OR",
  "NOT",
  "IN",
  "NOT IN",
  "EXISTS",
  "BETWEEN",
  "LIKE",
  "IS NULL",
  "IS NOT NULL",
  "DISTINCT",
  "UNION",
  "UNION ALL",
  "INSERT INTO",
  "VALUES",
  "UPDATE",
  "SET",
  "DELETE",
  "CASE",
  "WHEN",
  "THEN",
  "ELSE",
  "END",
  "WITH",
  "OVER",
  "PARTITION BY",
  "DATE_SUB",
  "DATE_ADD",
  "CURDATE",
  "NOW",
  "CURRENT_DATE",
  "CURRENT_TIMESTAMP",
  "INTERVAL",
  "DAY",
  "MONTH",
  "YEAR",
  "COUNT",
  "SUM",
  "AVG",
  "MIN",
  "MAX",
  "ASC",
  "DESC",
  "TRUE",
  "FALSE",
  "NULL",
]);

function isSqlKeyword(token: string): boolean {
  return SQL_KEYWORDS.has(token.toUpperCase());
}

export function tokenizeSqlLine(
  line: string,
): Array<{ text: string; kind: "comment" | "string" | "number" | "punct" | "keyword" | "plain" }> {
  const tokens: Array<{
    text: string;
    kind: "comment" | "string" | "number" | "punct" | "keyword" | "plain";
  }> = [];
  const rest = line;
  const tokenRegex =
    /(--[^\n]*|'[^']*'|"[^"]*"|\b\d+(?:\.\d+)?\b|[(),.;]|\b[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:BY|JOIN|ALL|INTO|NOT|NULL))?\b)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenRegex.exec(rest)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ text: rest.slice(lastIndex, match.index), kind: "plain" });
    }
    const tok = match[0];
    if (tok.startsWith("--")) {
      tokens.push({ text: tok, kind: "comment" });
    } else if (tok.startsWith("'") || tok.startsWith('"')) {
      tokens.push({ text: tok, kind: "string" });
    } else if (/^\d/.test(tok)) {
      tokens.push({ text: tok, kind: "number" });
    } else if (/^[(),.;]$/.test(tok)) {
      tokens.push({ text: tok, kind: "punct" });
    } else if (isSqlKeyword(tok)) {
      tokens.push({ text: tok, kind: "keyword" });
    } else {
      tokens.push({ text: tok, kind: "plain" });
    }
    lastIndex = match.index + tok.length;
  }
  if (lastIndex < rest.length) {
    tokens.push({ text: rest.slice(lastIndex), kind: "plain" });
  }
  return tokens;
}

export type MarkdownBlock =
  | { type: "code"; lang: string; code: string }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "hr" }
  | { type: "line"; raw: string };

function parseTableRow(line: string): string[] {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => c.trim());
}

function isTableSeparator(line: string): boolean {
  const t = line.trim();
  return t.includes("-") && /^\|?[\s:|-]+\|?$/.test(t);
}

/** GFM 主题分隔线（thematic break）：仅由 3 个及以上同类字符 `-`/`*`/`_` 与空白组成。
 * LLM 常用 `---` 分节，若不当成分隔线就会把字面 "---" 渲染出来。 */
function isThematicBreak(line: string): boolean {
  const t = line.trim();
  return /^(?:-{3,}|\*{3,}|_{3,})[\s-*_]*$/.test(t) && !t.includes("|");
}

export function splitMarkdownBlocks(content: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = content.split("\n");
  let i = 0;
  // 连续空白/分隔线折叠：避免 LLM 输出里成串的空行与 `---` 把版面撑开、
  // 或多个相邻分隔线叠成一组粗线。push 前用下面的 helper 做去重。
  const push = (b: MarkdownBlock) => {
    const prev = blocks[blocks.length - 1];
    if (b.type === "hr") {
      // 相邻 hr 合并；hr 紧跟空行/空行紧跟 hr 也跳过。
      if (prev && prev.type === "hr") return;
    }
    if (b.type === "line" && b.raw.trim() === "") {
      // 连续空行只保留一条；hr 后不接空行。
      if (prev && ((prev.type === "line" && prev.raw.trim() === "") || prev?.type === "hr")) return;
    }
    blocks.push(b);
  };
  while (i < lines.length) {
    const line = lines[i];
    const fenceMatch = line.trim().match(/^```(\w*)$/);
    if (fenceMatch) {
      const lang = fenceMatch[1].toLowerCase();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      if (lang !== "sql") {
        push({ type: "code", lang, code: codeLines.join("\n") });
      }
      continue;
    }
    // 主题分隔线：先于表格判定，避免把 `---` 误当表格分隔行。
    if (isThematicBreak(line)) {
      push({ type: "hr" });
      i++;
      continue;
    }
    // GFM 表格：当前行是 | 开头的表头，紧接一行分隔线 |---|---|
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const header = parseTableRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(parseTableRow(lines[i]));
        i++;
      }
      push({ type: "table", header, rows });
      continue;
    }
    push({ type: "line", raw: line });
    i++;
  }
  // 去掉首尾空行块，避免气泡上下出现多余间距。
  while (
    blocks.length &&
    blocks[0].type === "line" &&
    (blocks[0] as { raw: string }).raw.trim() === ""
  ) {
    blocks.shift();
  }
  while (
    blocks.length &&
    blocks[blocks.length - 1].type === "line" &&
    (blocks[blocks.length - 1] as { raw: string }).raw.trim() === ""
  ) {
    blocks.pop();
  }
  return blocks;
}

export function splitInlineTokens(
  text: string,
): Array<{ type: "text" | "bold" | "code" | "link"; value: string; href?: string }> {
  const parts: Array<{ type: "text" | "bold" | "code" | "link"; value: string; href?: string }> =
    [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: "text", value: text.slice(lastIndex, match.index) });
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push({ type: "bold", value: token.slice(2, -2) });
    } else if (token.startsWith("`")) {
      parts.push({ type: "code", value: token.slice(1, -1) });
    } else if (token.startsWith("[")) {
      const m = /^\[([^\]]+)\]\(([^\s)]+)\)$/.exec(token);
      if (m) parts.push({ type: "link", value: m[1], href: m[2] });
      else parts.push({ type: "text", value: token });
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) {
    parts.push({ type: "text", value: text.slice(lastIndex) });
  }
  return parts;
}
