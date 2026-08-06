"""跨轮上下文 compaction（V4 O1）：对齐 pi 的「结构化摘要 + 近轮保留」范式。

**为什么存在**：原实现用 ``history[-6:]`` 硬截断（chat_bi.py 旧代码）——第 7 轮之前的
对话被整块丢弃，多步探索（「先看 A，再对比 B，现在按 A 的口径下钻」）一旦超过 6 轮，
模型就再也看不到自己前面定了什么口径、拒了什么方案，只能重问或重错。

pi 的 compaction（docs/compaction.md）做法：超 token 阈值时，把**旧轮摘要成结构化文本**
（Goal / Progress / Key facts / Decisions），只保留**近若干轮原文**。本模块做同一件事，
但**不额外调 LLM**（护住 V2 的 avg_llm_calls=2.6）——摘要是**抽取式**的、确定性的、可单测：

    近轮（预算内）：原样保留 user/assistant 消息
    旧轮（超预算）：抽取每轮的用户诉求 + 助手结论要点 → 一段结构化 summary system note

**接地不变式**：摘要里出现的具名实体（对象/口径显示名）必须能回喂给 FactLedger，
否则模型引用「摘要里看到的旧实体」会被 F4 当幻觉误拒答。故 ``carried_names`` 一并带出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def estimate_chars(text: str) -> int:
    """字符预算的估算。CJK 场景 token≈字符，故直接按字符数计，简单且偏保守。"""
    return len(text or "")


@dataclass
class CompactionResult:
    """compaction 的全部产出。``recent`` 直接拼进 messages；``summary`` 作为一段
    system 备注置于近轮之前；``carried_names`` 回喂 FactLedger 防误拒答。"""

    recent: list[dict] = field(default_factory=list)
    summary: str | None = None
    carried_names: list[str] = field(default_factory=list)
    # V5 T3：从被摘要旧轮里抢救出来的关键 SQL（完整保留、不截断）——口径的具体实现。
    key_sql: list[str] = field(default_factory=list)
    # 度量：被摘要掉的轮数与摘要前后的字符数（进遥测，看收益）
    summarized_turns: int = 0
    chars_before: int = 0
    chars_after: int = 0

    @property
    def triggered(self) -> bool:
        return self.summary is not None


# 从助手正文里抽名词性要点：中文引号 / 书名号里的实体名，以及「口径/指标/对象」邻近词。
_QUOTED = re.compile(r"[「『\"']([^「『\"'』]{2,40})[」』\"']")

# V5 T3：从一轮正文里抽出 ```sql 围栏块（保原文，不截断）——口径的具体实现在 SQL 里，
# 被 160 字首句截断后，模型“沿上一轮口径继续下钻”就只能重算。把它完整保下来。
_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.+?)```", re.IGNORECASE | re.DOTALL)


def _extract_sql_blocks(text: str) -> list[str]:
    """抽出正文里的 ```sql 围栏块（去围栏、去首尾空白），仅保留看着像 SELECT 的。"""
    out: list[str] = []
    for m in _SQL_FENCE.findall(text or ""):
        sql = m.strip()
        # 只保留只读查询——摘要里没必要带上非 SELECT 片段（也不会有，执行层只读）。
        if sql and re.match(r"\s*(select|with)\b", sql, re.IGNORECASE):
            out.append(sql)
    return out


def _turn_text(item: dict) -> str:
    content = item.get("content")
    return str(content or "").strip()


def _summarize_turn(role: str, text: str, *, max_chars: int = 160) -> str:
    """把一轮压成一行。用户轮保留诉求首句，助手轮保留结论首句。"""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    tag = "问" if role == "user" else "答"
    return f"- [{tag}] {text}"


def _extract_names(text: str) -> list[str]:
    """抽取引号/书名号内的候选实体名，去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _QUOTED.findall(text or ""):
        name = m.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def compact_conversation(
    history: list[dict] | None,
    *,
    char_budget: int,
    enabled: bool = True,
) -> CompactionResult:
    """把历史对话压到 ``char_budget`` 字符内。

    近轮（从新到旧累计不超预算）原样保留；更早的轮抽取式摘要成一段结构化 note。
    ``enabled=False`` 或历史很短时，退化为等价于 ``history[-6:]`` 的近轮保留（零行为变化）。
    """
    items = [
        it for it in (history or [])
        if it.get("role") in {"user", "assistant"} and str(it.get("content") or "").strip()
    ]
    result = CompactionResult()
    if not items:
        return result

    result.chars_before = sum(estimate_chars(_turn_text(it)) for it in items)

    if not enabled:
        # 关闭：沿用旧行为（最近 6 条），不摘要
        result.recent = [
            {"role": it["role"], "content": str(it["content"])} for it in items[-6:]
        ]
        result.chars_after = sum(estimate_chars(_turn_text(it)) for it in result.recent)
        return result

    # 从最新一轮往回累计，够预算就停——这些是「近轮原文」
    recent_rev: list[dict] = []
    used = 0
    cut = 0  # items[:cut] 是要被摘要的旧轮
    for i in range(len(items) - 1, -1, -1):
        t = estimate_chars(_turn_text(items[i]))
        # 至少保留最新一轮；其余按预算收
        if recent_rev and used + t > char_budget:
            cut = i + 1
            break
        recent_rev.append({"role": items[i]["role"], "content": str(items[i]["content"])})
        used += t
    else:
        cut = 0

    recent_rev.reverse()
    result.recent = recent_rev

    older = items[:cut]
    if older:
        lines: list[str] = []
        names: list[str] = []
        sqls: list[str] = []
        for it in older:
            text = _turn_text(it)
            lines.append(_summarize_turn(it["role"], text))
            names.extend(_extract_names(text))
            if it["role"] == "assistant":
                sqls.extend(_extract_sql_blocks(text))
        result.summarized_turns = len(older)
        # 去重保序
        seen: set[str] = set()
        result.carried_names = [n for n in names if not (n in seen or seen.add(n))]
        # V5 T3：关键 SQL 去重保序（只留最后几条，避免摘要自己臃肿）
        seen_sql: set[str] = set()
        uniq_sql = [s for s in sqls if not (s in seen_sql or seen_sql.add(s))]
        result.key_sql = uniq_sql[-3:]
        summary = (
            "【早前对话摘要】以下是本次会话更早轮次的要点（原文已折叠以省上下文），"
            "延续对话时请沿用其中已确定的口径/对象/结论，不要重复追问：\n"
            + "\n".join(lines)
        )
        # 关键 SQL 完整附在摘要末尾（不进首句截断），让“沿上一轮口径下钻”有可复用的基底。
        if result.key_sql:
            summary += (
                "\n\n【已确定的口径 SQL（可直接复用/微调，勿重新推导）】：\n"
                + "\n".join(f"```sql\n{s}\n```" for s in result.key_sql)
            )
        result.summary = summary

    result.chars_after = (
        estimate_chars(result.summary or "")
        + sum(estimate_chars(_turn_text(it)) for it in result.recent)
    )
    return result


__all__ = ["CompactionResult", "compact_conversation", "estimate_chars"]
