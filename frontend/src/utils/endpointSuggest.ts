// 转换「对象→业务关系」时的端点键建议：真实源常零 FK，事实表引用的实体只能从
// 列名反推。这里用事实表自身的列（properties）去匹配本体内已有对象的名字——中文按
// 子串、英文（snake_case）按词元词干——给出候选端点，复核者点选即可，免在数百个
// 对象里手动搜。仅作建议：命中弱也照常人工确认，不做硬判定。

import type { ObjectTypeSummary, Property } from "../types";

export interface EndpointSuggestion {
  object: ObjectTypeSummary;
  /** 命中的列展示名，供 UI 说明「凭哪一列建议的」。 */
  matchedColumn: string;
  score: number;
}

// 列名尾缀去除后视为「引用键」（counterparty_account_id → counterparty_account）。
const KEY_SUFFIX = /[_-]?(id|no|code|key|ref|fk|num|编号|编码|号)$/i;
// 明显非引用的技术/审计列，直接跳过，避免拿「创建时间」之类去撞对象名。
const NOISE_COLUMN =
  /(^|[_-])(id|pk|guid|uuid|seq|idx|index|created|updated|create_time|update_time|时间|日期|序号|排序|索引|备注|状态|金额|数量|标识)([_-]|$)/i;

function latinTokens(s: string): string[] {
  return s
    .toLowerCase()
    .replace(KEY_SUFFIX, "")
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length >= 2);
}

function hasCjk(s: string): boolean {
  return /[一-鿿]/.test(s);
}

/** 单个（列 × 对象）匹配打分，0 表示不相关。分数越高越像该列引用了该对象。 */
function matchScore(prop: Property, peer: ObjectTypeSummary): number {
  const colDisp = (prop.display_name || "").trim();
  const colName = (prop.name || "").trim();
  const objDisp = (peer.display_name || "").trim();
  const objName = (peer.name || "").trim();

  let score = 0;

  // 中文：对象名（>=2 字）作为列名子串 → 强；反向包含 → 弱；完全相等 → 最强。
  if (hasCjk(objDisp) && objDisp.length >= 2 && hasCjk(colDisp)) {
    if (colDisp === objDisp) score = Math.max(score, 5);
    else if (colDisp.includes(objDisp)) score = Math.max(score, 3);
    else if (objDisp.includes(colDisp) && colDisp.length >= 2) score = Math.max(score, 1);
  }

  // 英文/拼音：列名去尾缀后的词元与对象名词元的词干重合。
  const colTokens = latinTokens(colName);
  const objTokens = latinTokens(objName);
  if (colTokens.length && objTokens.length) {
    const objStr = objTokens.join("_");
    const colStr = colTokens.join("_");
    if (colStr === objStr) score = Math.max(score, 5);
    else if (colStr.endsWith(objStr) || colStr.includes(objStr)) score = Math.max(score, 3);
  }

  // 明确的引用键列（xxx_id / xxx编号）额外加权，优先浮上来。
  if (score > 0 && KEY_SUFFIX.test(colName)) score += 1;
  return score;
}

/**
 * 为一张事实表推荐候选端点对象。
 *
 * @param properties 事实表自身的列
 * @param peers 本体内其它对象（调用方应已排除自身）
 * @param limit 返回条数上限
 */
export function suggestEndpoints(
  properties: Property[],
  peers: ObjectTypeSummary[],
  limit = 6,
): EndpointSuggestion[] {
  const best = new Map<string, EndpointSuggestion>();
  for (const prop of properties) {
    const col = `${prop.name || ""} ${prop.display_name || ""}`;
    if (NOISE_COLUMN.test(col) && !KEY_SUFFIX.test(prop.name || "")) continue;
    for (const peer of peers) {
      const score = matchScore(prop, peer);
      if (score <= 0) continue;
      const prev = best.get(peer.id);
      if (!prev || score > prev.score) {
        best.set(peer.id, {
          object: peer,
          matchedColumn: prop.display_name || prop.name,
          score,
        });
      }
    }
  }
  return [...best.values()].sort((a, b) => b.score - a.score).slice(0, limit);
}
