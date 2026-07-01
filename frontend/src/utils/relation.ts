export const RELATION_TERM_MAX_LENGTH = 8;

/** 关系结构类型（SSOT §5.3） */
export const RELATION_STRUCTURE_OPTIONS = [
  { label: "外键关系", value: "foreign_key" },
  { label: "桥表", value: "bridge_table" },
  { label: "事实表", value: "fact_table" },
  { label: "其他", value: "other" },
] as const;

export type RelationStructureType = (typeof RELATION_STRUCTURE_OPTIONS)[number]["value"];

const RELATION_STRUCTURE_LABELS: Record<RelationStructureType, string> = {
  foreign_key: "外键关系",
  bridge_table: "桥表",
  fact_table: "事实表",
  other: "其他",
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
  /(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成)/;

export function compactRelationTerm(value: string): string {
  const text = value.trim();
  if (!text) return text;

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
