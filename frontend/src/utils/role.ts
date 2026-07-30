import type { RoleSignals } from "../types";

export type { RoleSignals };

// 对象角色（table_role）与「判定依据」展示辅助。
//
// 后端 object_classifier 不依赖表名，用结构/内容/拓扑/语义信号给每张表打分并分类，
// 产出两份可展示的东西：
//   - role_reason：人类可读理由，子句以「；」分隔，待复核前缀 [待复核]；
//   - role_signals：结构化证据 JSON（score / needs_review / signals{...}）。
// 这里把它们规整成前端可直接渲染的结构，供角色徽章与详情「判定依据」面板复用，
// 避免在多处重复散落映射。阈值与 backend/app/services/object_classifier.py 对齐。

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

/** 复核状态以 role_reason 是否含 [待复核] 为唯一真源（与后端一致）。 */
export function isNeedsReview(reason?: string | null): boolean {
  return (reason ?? "").includes(REVIEW_MARK);
}

/**
 * 把 role_reason 拆成可扫读的证据条目：剥离 [待复核] 前缀，按「；」/「;」分条去空。
 * 让复核者不用读一整段 prose，而是逐条看判据。
 */
export function reasonClauses(reason?: string | null): string[] {
  if (!reason) return [];
  const stripped = reason.replace(/^\s*\[?待复核\]?\s*/, "").trim();
  if (!stripped) return [];
  return stripped
    .split(/[；;]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 单条信号推向哪个结论。 */
export type SignalDirection = "business" | "nonbusiness" | "neutral";

export interface SignalItem {
  key: string;
  label: string;
  value: string;
  direction: SignalDirection;
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
export function describeSignals(rs?: RoleSignals | null): DecisionEvidence {
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
    });
  }
  if (has("fk_in_degree")) {
    const n = num(s.fk_in_degree);
    items.push({
      key: "fk_in_degree",
      label: "被引用（外键入度）",
      value: n >= 3 ? `${n}（枢纽）` : String(n),
      direction: n >= 1 ? "business" : "neutral",
    });
  }
  if (has("distinct_fk_targets")) {
    const n = num(s.distinct_fk_targets);
    items.push({
      key: "distinct_fk_targets",
      label: "引用不同实体数",
      value: String(n),
      direction: n >= 2 ? "nonbusiness" : "neutral",
    });
  }
  if (has("own_attr_count")) {
    const n = num(s.own_attr_count);
    items.push({
      key: "own_attr_count",
      label: "自有属性数",
      value: String(n),
      direction: n >= 1 ? "business" : "nonbusiness",
    });
  }
  if (has("descriptive_ratio")) {
    const v = num(s.descriptive_ratio);
    items.push({
      key: "descriptive_ratio",
      label: "描述性字段占比",
      value: pct(v),
      direction: v >= 0.4 ? "business" : "neutral",
    });
  }
  if (has("measure_ratio")) {
    const v = num(s.measure_ratio);
    items.push({
      key: "measure_ratio",
      label: "度量字段占比",
      value: pct(v),
      direction: v >= 0.3 ? "nonbusiness" : "neutral",
    });
  }
  if (has("technical_ratio")) {
    const v = num(s.technical_ratio);
    items.push({
      key: "technical_ratio",
      label: "技术字段占比",
      value: pct(v),
      direction: v >= 0.25 ? "nonbusiness" : "neutral",
    });
  }
  if (has("tech_score")) {
    const v = num(s.tech_score);
    items.push({
      key: "tech_score",
      label: "技术信号分",
      value: v.toFixed(1),
      direction: v >= 2 ? "nonbusiness" : "neutral",
    });
  }
  if (has("connected") || has("isolated")) {
    const connected = has("connected")
      ? Boolean(s.connected)
      : !s.isolated;
    items.push({
      key: "connected",
      label: "图连通性",
      value: connected ? "连通" : "孤立",
      direction: connected ? "business" : "nonbusiness",
    });
  }
  if (has("is_child_table") && Boolean(s.is_child_table)) {
    items.push({
      key: "is_child_table",
      label: "明细/子表",
      value: "是",
      direction: "nonbusiness",
    });
  }
  if (has("fact_name_token")) {
    items.push({
      key: "fact_name_token",
      label: "事件动词命名",
      value: `含「${String(s.fact_name_token)}」`,
      direction: "nonbusiness",
    });
  }
  if (has("segment_size")) {
    const n = num(s.segment_size);
    const inSegment = n >= MIN_SEGMENT_SIZE;
    items.push({
      key: "segment_size",
      label: "业务环节成员数",
      value: inSegment ? `${n}（隶属环节）` : `${n}（未成环节）`,
      direction: inSegment ? "business" : "nonbusiness",
    });
  }

  return { score: typeof rs?.score === "number" ? rs.score : undefined, items };
}

/** 综合得分对阈值的定性说明（阈值 2.0 与后端一致）。 */
export const ROLE_SCORE_THRESHOLD = 2.0;
