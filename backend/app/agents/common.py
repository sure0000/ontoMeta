"""Drafter/Executor 共用的意图解析工具。

**Spec 只能引用真实存在的实体。** 这里的匹配一律在库中已有的对象/字段/指标上做，
而不是让模型凭空生成名字——从源头上就不给幻觉留出口，Validation Gate 是第二道防线。

LLM 接入点：``select_by_intent`` 是唯一需要「理解自然语言」的地方。当前实现是
确定性的分词包含匹配；接 LLM 时只需替换本函数，其余结构仍由证据推导（沿用
``services/draft_generator.py`` 确立的「结构确定性推导、LLM 只做语义提升」原则）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, TypeVar

T = TypeVar("T")


def _tokens(text: str) -> list[str]:
    """中英文混合分词：英文按词，中文按 2-gram。"""
    lowered = (text or "").lower()
    latin = re.findall(r"[a-z0-9_]+", lowered)
    han = re.findall(r"[一-鿿]+", lowered)
    grams: list[str] = []
    for run in han:
        grams.extend(run[i : i + 2] for i in range(max(len(run) - 1, 1)))
    return latin + grams


def score_match(intent: str, *candidates: str | None) -> int:
    """候选名称与意图的重合度。0 表示完全不相关。"""
    intent_tokens = set(_tokens(intent))
    if not intent_tokens:
        return 0
    score = 0
    for candidate in candidates:
        if not candidate:
            continue
        cand_tokens = set(_tokens(candidate))
        score += len(intent_tokens & cand_tokens)
        # 整名出现在意图里 → 强信号
        if candidate.lower() in (intent or "").lower():
            score += 5
    return score


def select_by_intent(
    intent: str, items: Iterable[T], key: "callable[[T], tuple[str | None, ...]]"
) -> T | None:
    """从候选中挑与意图最贴合的一个；无任何重合时返回 None（宁缺毋滥）。"""
    best: T | None = None
    best_score = 0
    for item in items:
        score = score_match(intent, *key(item))
        if score > best_score:
            best, best_score = item, score
    return best


def require_context(context: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if not context.get(k)]
    if missing:
        raise ValueError(f"缺少必要上下文：{', '.join(missing)}")
