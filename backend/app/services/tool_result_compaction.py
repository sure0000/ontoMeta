"""工具结果压缩（P2.2）：超预算时**按语义降级**，而不是按字符砍。

原实现是 ``json.dumps(result)[:8000] + "…(结果过长已截断)"``——一刀切在字符位置上，
几乎必然把 JSON 截在半个键名或半个中文字里。模型收到的是**语法都不成立**的片段，
它要么当没看见，要么照着残片瞎猜；而我们还在 prompt 里要求它「严禁把样本当全集」。

这里改成一条降级阶梯，每一级都产出**合法 JSON**：

    完整 → 丢 description 等长文本 → 丢次要字段 → 列表采样（保留 total 与 facets）
         → 仅摘要

**不变式：回灌给模型的永远是合法 JSON。** 宁可信息少，不可结构烂。
"""

from __future__ import annotations

import json
from typing import Any

# 第 1 级丢弃：纯说明性长文本，去掉不影响模型定位实体
_VERBOSE_KEYS = ("description", "note", "values_note", "role_reason", "expression_draft")
# 第 2 级丢弃：辅助元数据，保留后仍能作答
_SECONDARY_KEYS = (
    "data_type", "semantic_type", "structure_type", "table_role",
    "property_count", "expression_summary", "caliber_trace", "certificate",
)
# 采样时每个列表保留几条
_SAMPLE_SIZE = 5


def compact_tool_result(result: Any, budget: int) -> tuple[str, bool]:
    """把工具结果压到 ``budget`` 字符内。返回 (JSON 文本, 是否压缩过)。"""
    text = _dumps(result)
    if len(text) <= budget:
        return text, False

    for stage in (_drop_verbose, _drop_secondary, _sample_lists):
        result = stage(result)
        text = _dumps(result)
        if len(text) <= budget:
            return text, True

    # 兜底：只留标量摘要。仍是合法 JSON，模型至少知道「有东西但太大」
    return _dumps(_scalar_digest(result, budget)), True


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _walk(node: Any, fn) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, fn) for k, v in fn(node).items()}
    if isinstance(node, list):
        return [_walk(v, fn) for v in node]
    return node


def _drop_verbose(node: Any) -> Any:
    return _walk(node, lambda d: {k: v for k, v in d.items() if k not in _VERBOSE_KEYS})


def _drop_secondary(node: Any) -> Any:
    return _walk(node, lambda d: {k: v for k, v in d.items() if k not in _SECONDARY_KEYS})


def _sample_lists(node: Any) -> Any:
    """长列表只留前几条，并**就地标注**这是采样——结构自己说明自己是样本。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if isinstance(v, list) and len(v) > _SAMPLE_SIZE:
                out[k] = [_sample_lists(x) for x in v[:_SAMPLE_SIZE]]
                out[f"{k}_total"] = len(v)
                out[f"{k}_is_sample"] = True
            else:
                out[k] = _sample_lists(v)
        return out
    if isinstance(node, list):
        if len(node) > _SAMPLE_SIZE:
            return [_sample_lists(x) for x in node[:_SAMPLE_SIZE]]
        return [_sample_lists(x) for x in node]
    return node


def _scalar_digest(node: Any, budget: int) -> dict:
    """最后一级：只保留顶层标量 + 各列表长度。"""
    digest: dict[str, Any] = {"_compacted": True}
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                digest[k] = v if not isinstance(v, str) else v[:200]
            elif isinstance(v, list):
                digest[f"{k}_count"] = len(v)
    digest["_note"] = "结果过大，仅返回摘要；请缩小检索范围或分批获取。"
    # 极端情况下摘要本身也可能超预算（键极多）：按键裁剪，仍保持合法 JSON
    while len(_dumps(digest)) > budget and len(digest) > 3:
        for k in list(digest):
            if k not in ("_compacted", "_note"):
                digest.pop(k)
                break
    return digest


__all__ = ["compact_tool_result"]
