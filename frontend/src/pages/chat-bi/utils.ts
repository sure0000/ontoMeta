import type { ChatBiAnswer, ChatBiConversation } from "../../types";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  payload?: ChatBiAnswer;
  pending?: boolean;
  error?: boolean;
}

export type TimeGroup =
  | "pinned"
  | "today"
  | "yesterday"
  | "thisWeek"
  | "thisMonth"
  | "older";

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
  "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING",
  "LIMIT", "OFFSET", "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN",
  "OUTER JOIN", "FULL JOIN", "ON", "AS", "AND", "OR", "NOT",
  "IN", "NOT IN", "EXISTS", "BETWEEN", "LIKE", "IS NULL", "IS NOT NULL",
  "DISTINCT", "UNION", "UNION ALL", "INSERT INTO", "VALUES",
  "UPDATE", "SET", "DELETE", "CASE", "WHEN", "THEN", "ELSE", "END",
  "WITH", "OVER", "PARTITION BY", "DATE_SUB", "DATE_ADD",
  "CURDATE", "NOW", "CURRENT_DATE", "CURRENT_TIMESTAMP",
  "INTERVAL", "DAY", "MONTH", "YEAR",
  "COUNT", "SUM", "AVG", "MIN", "MAX",
  "ASC", "DESC", "TRUE", "FALSE", "NULL",
]);

function isSqlKeyword(token: string): boolean {
  return SQL_KEYWORDS.has(token.toUpperCase());
}

export function tokenizeSqlLine(line: string): Array<{ text: string; kind: "comment" | "string" | "number" | "punct" | "keyword" | "plain" }> {
  const tokens: Array<{ text: string; kind: "comment" | "string" | "number" | "punct" | "keyword" | "plain" }> = [];
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

export function splitMarkdownBlocks(content: string): Array<
  | { type: "code"; lang: string; code: string }
  | { type: "line"; raw: string }
> {
  const blocks: Array<
    | { type: "code"; lang: string; code: string }
    | { type: "line"; raw: string }
  > = [];
  const lines = content.split("\n");
  let i = 0;
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
        blocks.push({ type: "code", lang, code: codeLines.join("\n") });
      }
      continue;
    }
    blocks.push({ type: "line", raw: line });
    i++;
  }
  return blocks;
}

export function splitInlineTokens(text: string): Array<{ type: "text" | "bold" | "code"; value: string }> {
  const parts: Array<{ type: "text" | "bold" | "code"; value: string }> = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
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
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) {
    parts.push({ type: "text", value: text.slice(lastIndex) });
  }
  return parts;
}
