import type { ReviewFlag } from "./role";
import { LOW_CONFIDENCE } from "./role";

export const RELATION_TERM_MAX_LENGTH = 8;

/** 关系结构类型（SSOT §5.3） */
export const RELATION_STRUCTURE_OPTIONS = [
  { label: "外键关系", value: "foreign_key" },
  { label: "桥表", value: "bridge_table" },
  { label: "事实表", value: "fact_table" },
  { label: "派生/溯源", value: "derivation" },
  { label: "外键映射", value: "other" },
] as const;

export type RelationStructureType = (typeof RELATION_STRUCTURE_OPTIONS)[number]["value"];

const RELATION_STRUCTURE_LABELS: Record<RelationStructureType, string> = {
  foreign_key: "外键关系",
  bridge_table: "桥表",
  fact_table: "事实表",
  derivation: "派生/溯源",
  other: "外键映射",
};

export function getRelationStructureLabel(value?: string | null): string {
  if (!value) return "-";
  return RELATION_STRUCTURE_LABELS[value as RelationStructureType] ?? value;
}

/** 根据描述与证据推断关系结构类型 */
export function inferRelationStructureType(
  description?: string | null,
  sourceEvidence?: string | null,
): RelationStructureType {
  const text = `${description || ""} ${sourceEvidence || ""}`.toLowerCase();
  if (text.includes("外键") || text.includes("foreign")) return "foreign_key";
  if (text.includes("桥") || text.includes("bridge")) return "bridge_table";
  if (
    text.includes("血缘") ||
    text.includes("lineage") ||
    text.includes("加工至") ||
    text.includes("派生")
  ) {
    return "derivation";
  }
  if (text.includes("事实") || text.includes("fact_") || text.includes("fact table")) {
    return "fact_table";
  }
  return "other";
}

/** 根据来源证据文本推断 DataHub 证据类型（SSOT §7.3.3） */
export function inferRelationEvidenceType(evidence?: string): string {
  if (!evidence) return "人工补充";
  const text = evidence.toLowerCase();
  if (text.includes("外键") || text.includes("foreign")) return "外键关系";
  if (text.includes("血缘") || text.includes("lineage")) return "血缘加工";
  if (text.startsWith("urn:li:dataset")) return "DataHub 元数据";
  return "结构推断";
}

export const CARDINALITY_OPTIONS = [
  { label: "一对一 (1:1)", value: "1:1" },
  { label: "一对多 (1:N)", value: "1:N" },
  { label: "多对一 (N:1)", value: "N:1" },
  { label: "多对多 (N:M)", value: "N:M" },
] as const;

const CARDINALITY_ALIASES: Record<string, (typeof CARDINALITY_OPTIONS)[number]["value"]> = {
  one_to_one: "1:1",
  one_to_many: "1:N",
  many_to_one: "N:1",
  many_to_many: "N:M",
};

/** 将后端多种基数表示统一为表单选项值 */
export function normalizeCardinality(value?: string | null): string | undefined {
  if (!value) return undefined;
  return CARDINALITY_ALIASES[value] ?? value;
}

const VERB_PATTERN =
  /(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成|汇总|对账|结算|统计|清洗|加工|标准化|报表|核对|刻画|度量|支撑)/;

export function compactRelationTerm(value: string): string {
  const text = value.trim();
  if (!text) return text;

  // 已是简短、不含句子/连接词的干净谓词(如「对账为」「汇总为」「属于」)，
  // 直接保留，避免被 VERB_PATTERN 抽成单动词而丢掉方向后缀。
  if (text.length <= RELATION_TERM_MAX_LENGTH && !/关联\s|加工至|->|至|通过|血缘/.test(text)) {
    return text;
  }

  const verbMatch = text.match(VERB_PATTERN);
  if (verbMatch) return verbMatch[1];

  if (text.length > RELATION_TERM_MAX_LENGTH) {
    return text.slice(0, RELATION_TERM_MAX_LENGTH);
  }
  return text;
}

export function validateRelationTerm(value: string): string | null {
  const text = value.trim();
  if (!text) return "请输入关系语义词";
  if (text.length > RELATION_TERM_MAX_LENGTH) {
    return `关系语义应为简短词语（不超过 ${RELATION_TERM_MAX_LENGTH} 字）`;
  }
  if (/[。；！？]/.test(text)) {
    return "请使用词语而非完整句子，详细说明写在语义描述中";
  }
  if (/\s{2,}|关联\s|加工至|表/.test(text)) {
    return "请只填写关系动词，如「属于」「包含」「下单」";
  }
  return null;
}

export const RELATION_TERM_RULES = [
  { required: true, message: "请输入关系语义词" },
  { max: RELATION_TERM_MAX_LENGTH, message: `不超过 ${RELATION_TERM_MAX_LENGTH} 字` },
  {
    validator: (_: unknown, value?: string) => {
      const error = validateRelationTerm(value || "");
      return error ? Promise.reject(new Error(error)) : Promise.resolve();
    },
  },
] as const;

// ---------------------------------------------------------------------------
// 关系侧的「机器为什么要你看」——与对象侧 utils/role.ts 的 reviewFlags 同一套口径
// ---------------------------------------------------------------------------

/**
 * 空泛动词：出现它就等于这条关系还没说出业务语义。
 * 与后端 `api/ontology.py::suggest_verb_refinements` 里的 `empty_verbs` 保持一致。
 */
export const EMPTY_VERBS = new Set(["属于", "引用", "关联", "关系", "连接"]);

export function isEmptyVerb(verb?: string | null): boolean {
  const text = (verb || "").trim();
  return !text || EMPTY_VERBS.has(text);
}

/**
 * 从证据散文里读回连接键。
 *
 * 证据句由 backend/app/services/evidence_builder.py 生成：
 * `A 通过引用字段 x 关联 B`。正则与后端 ontology_projection._FK_IN_PROSE 同源——
 * 那边靠它推 ON 条件，这边靠它告诉复核者「机器是凭哪一列认定这条关系的」。
 */
export function parseJoinKey(evidence?: string | null): string | null {
  if (!evidence) return null;
  const match = evidence.match(/引用字段\s*[`"']?([A-Za-z_][A-Za-z0-9_]*)[`"']?\s*关联/);
  return match ? match[1] : null;
}

/** 证据是实测的还是推断的：推断出来的关系，人得自己认一遍。 */
export function isInferredEvidence(evidence?: string | null): boolean {
  return Boolean(evidence && /推断/.test(evidence));
}

/** 关系的复核旗标：与对象侧同一套轻重口径（alert=机器拿不准，warn=证据弱，info=提示）。 */
export function relationReviewFlags(rel: {
  display_name?: string;
  source_evidence?: string;
  description?: string;
  source_confidence?: number;
  source_object_type_id?: string;
  target_object_type_id?: string;
}): ReviewFlag[] {
  const flags: ReviewFlag[] = [];
  const evidence = rel.source_evidence || rel.description;
  if (isEmptyVerb(rel.display_name)) {
    flags.push({
      key: "empty_verb",
      label: "空动词",
      tone: "alert",
      detail: `「${(rel.display_name || "").trim() || "未命名"}」没有说出业务语义，可用「动词建议」批量细化`,
    });
  }
  if (isInferredEvidence(evidence)) {
    const key = parseJoinKey(evidence);
    flags.push({
      key: "inferred",
      label: "结构推断",
      tone: "warn",
      detail: key
        ? `源库没有真外键，机器凭字段 ${key} 推断出这条关系`
        : "源库没有真外键，这条关系由结构推断得出",
    });
  }
  if (typeof rel.source_confidence === "number" && rel.source_confidence < LOW_CONFIDENCE) {
    flags.push({
      key: "low_confidence",
      label: `低置信 ${Math.round(rel.source_confidence * 100)}%`,
      tone: "warn",
    });
  }
  if (rel.source_object_type_id && rel.source_object_type_id === rel.target_object_type_id) {
    flags.push({ key: "self_loop", label: "自反关系", tone: "info", detail: "两端是同一个对象" });
  }
  return flags;
}
