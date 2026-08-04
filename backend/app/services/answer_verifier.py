"""答案可靠性校验器（F4）：把答案正文拆成原子断言，逐条对事实账本核验。

「宁可拒答不错答」的直接执行点。只要答案里出现**账本外的具名实体**、或**未经查询
证实的数值**，就判该答案不可靠 → 触发拒答。抽取采用保守策略（宁可多判为「需凭证」
而触发拒答，也不放过一处幻觉）。

设计见 FORMAL_VALIDATION_IMPL.md 第三部分 §3.2。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.agent_grounding import FactLedger


@dataclass
class Verdict:
    ok: bool
    unverified: list[str] = field(default_factory=list)  # 不可证片段（面向用户解释）


# 反引号标识符：`order_amount`
_BACKTICK = re.compile(r"`([^`\n]{2,60})`")
# 中文书名号具名实体：「毛利率」
_BOOKNAME = re.compile(r"「([^」\n]{2,60})」")

# 数值断言：抽取带业务单位的数字（纯序号/年份/百分号窗口噪声后续再滤）
_NUMERIC = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(万|亿|元|单|个|条|笔|次|人|件)")

# 数值噪声白名单：这些上下文里的数字不是「结论数值」，不要求 run_sql 凭证。
_NUM_NOISE_CONTEXT = re.compile(
    r"(近|最近|过去|前)\s*\d|第\s*\d+\s*步|top\s*\d+|\d+\s*天|\d{4}\s*年",
    re.IGNORECASE,
)

# 口径断言线索词：命中后尝试把附近的指标名对到账本 AST。
_CALIBER_HINT = re.compile(r"(口径|计算为|定义为|等于|统计口径)")


def _strip_code_blocks(text: str) -> str:
    """移除 ```...``` 代码块（SQL/示例）——其中的标识符不算正文断言。"""
    return re.sub(r"```.*?```", " ", text or "", flags=re.S)


def verify_answer(
    answer: str, ledger: FactLedger, *, strict_numbers: bool = True
) -> Verdict:
    """校验答案每条断言是否被账本蕴含。返回 Verdict（ok=False 时附不可证片段）。"""
    if not answer or not answer.strip():
        return Verdict(ok=True)  # 空答由上层其它逻辑处理

    body = _strip_code_blocks(answer)
    unverified: list[str] = []

    # 1) 具名实体：反引号标识符 + 书名号名词，必须在账本
    named: set[str] = set()
    for m in _BACKTICK.finditer(body):
        named.add(m.group(1).strip())
    for m in _BOOKNAME.finditer(body):
        named.add(m.group(1).strip())
    for token in named:
        # 去掉常见修饰（对象/字段/表/口径 后缀）后再比对，容纳「订单表」这类
        base = re.sub(r"(表|字段|对象|口径|指标|关系)$", "", token).strip() or token
        if not (ledger.has_entity_named(token) or ledger.has_entity_named(base)):
            unverified.append(f"提及了本体中未证实的「{token}」")

    # 2) 数值断言：带业务单位的数字须对得到 run_sql 单元格（strict 模式）
    if strict_numbers:
        for m in _NUMERIC.finditer(body):
            # 噪声上下文（近 7 天 / 第 2 步 / Top 10 / 2024 年）跳过
            ctx = body[max(0, m.start() - 6): m.end() + 2]
            if _NUM_NOISE_CONTEXT.search(ctx):
                continue
            num = m.group(1)
            if not ledger.has_numeric(num):
                unverified.append(f"给出了未经查询证实的数值 {num}{m.group(2)}")

    # 去重、限量（拒答解释不宜过长）
    seen: set[str] = set()
    deduped: list[str] = []
    for u in unverified:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return Verdict(ok=not deduped, unverified=deduped[:6])


__all__ = ["Verdict", "verify_answer"]
