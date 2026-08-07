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

# --------------------------------------------------------------------------
# V5 F5：抽取精度。以下三条都不是「放宽守卫」，是**把不属于实体断言的东西排除在外**——
# 守卫要拦的始终是「模型编了本体里没有的实体/数值」，这三类都不是编造。
# 实测（ERP 域 24 轮）里的 4 次拒答**全部**出自这三类误判，无一是真幻觉。
# --------------------------------------------------------------------------

# 1) 复合串：列表与路径。模型爱把一串实体写进同一对括号里——
#    「文档状态、序号、成品物料、物料编码」（字段清单）
#    「供应商 → 采购订单 → 采购收货 → 采购发票」（血缘路径）
#    整串当一个名字去核必然核不到，可每一段都在账本里。拆开逐段核：**全段可证才算证**，
#    有一段不可证仍然拒答——精度提高了，强度没降。
_COMPOSITE_SEP = re.compile(r"\s*(?:、|，|,|;|；|/|／|→|->|＞|>|\||｜)\s*")

# 2) SQL 片段：`_strip_code_blocks` 只清 ``` 围栏，模型也会把 SQL 写进「」里
#    （实测：「SELECT COUNT(*) FROM sales_order」）。SQL 的正确性由 F3 语义证明器管，
#    不该在实体接地这里再判一次——那里判的是「表存不存在」，比字符串比对准得多。
_SQL_LIKE = re.compile(r"^\s*(select|with|insert|update|delete|create)\b", re.IGNORECASE)

# 3) 从句 / 任务转述：**根因在这**——中文的「」本来就是强调与引用任意短语的括号，
#    不只用来标实体名。把每一段 「」 都当成「断言某实体存在」去核，才是误判的来源。
#    实测里模型反复用自己的话转述任务：
#      「各订单状态分别有多少条记录」「按客户汇总销售订单金额」「销售订单一共有多少条记录」
#    这些按任何读法都不是实体断言，核不到就拒答只会让用户看见「你问的东西本体里没有」。
#
#    判别只用**最强**的语法信号——疑问词、领起介词、「分别」，以及离谱的长度。
#    这些在本体的显示名里近乎不可能出现（显示名是短名词短语），所以不会把真幻觉放过去；
#    刻意留窄，宁可漏豁免（多拒一次）也不误豁免（放过一次幻觉）。
_CLAUSE_LIKE = re.compile(
    r"(多少|哪些|哪个|哪一|什么|为什么|如何|怎样|怎么|是否|吗[？?]?$|[？?])"
    r"|^(按|根据|沿着|关于|针对|基于|统计出|列出)"
    r"|分别"
)
_MAX_ENTITY_NAME_CHARS = 24  # 超过这个长度的「」内容不可能是一个本体显示名


def _looks_like_clause(token: str) -> bool:
    """``token`` 读起来是个从句/任务转述，而不是一个实体名。"""
    t = (token or "").strip()
    return len(t) > _MAX_ENTITY_NAME_CHARS or bool(_CLAUSE_LIKE.search(t))


# 4) 逐字复述：模型也会把用户问句原样引一遍。这类由「是否出现在用户说过的话里」
#    判定（见 _echoes_question）——转述由上面的从句判别兜，逐字引用由它兜。


def _strip_code_blocks(text: str) -> str:
    """移除 ```...``` 代码块（SQL/示例）——其中的标识符不算正文断言。"""
    return re.sub(r"```.*?```", " ", text or "", flags=re.S)


def _provable_name(ledger: FactLedger, token: str) -> bool:
    """账本里能否证得到这个名字。去掉「表/字段/对象/口径」这类后缀再比一次。"""
    token = token.strip()
    if not token:
        return False
    base = re.sub(r"(表|字段|对象|口径|指标|关系)$", "", token).strip() or token
    return ledger.has_entity_named(token) or ledger.has_entity_named(base)


def _normalize_echo(text: str) -> str:
    """归一化到「只剩内容字符」，用于判断答案里的片段是否照抄自用户问句。

    去空白与常见标点，是因为模型复述时经常改标点（问号丢掉、逗号换顿号），
    但内容字符一模一样。
    """
    return re.sub(r"[\s，。、,.;；:：!！?？（）()「」『』\"'`]+", "", (text or "").lower())


def _echoes_question(token: str, question_norm: str) -> bool:
    """``token`` 是否只是把用户问句里的话引了一遍。

    **这不是放宽守卫**：F4 拦的是「模型编了本体里没有的实体」，而用户自己写在问句里的
    字眼不是模型编的。实测里 `「按客户统计销售订单总金额、取前 30 名」`、
    `「销售订单一共有多少条记录」` 都是整句复述被当成实体名核，核不到就拒答，
    用户看到的是「你问的东西本体里没有」——问的明明就是他自己刚打的字。

    仍然安全：即便用户问了个不存在的对象、模型跟着复述，本轮**没有工具命中**就过不了
    `grounded` 那道闸；模型若据此断言具体数字，数值校验照样拦。
    """
    if not question_norm:
        return False
    t = _normalize_echo(token)
    # 太短的片段（如「订单」）可能碰巧是问句子串，那类本就该按实体核，不走这条豁免。
    return len(t) >= 6 and t in question_norm


def verify_answer(
    answer: str,
    ledger: FactLedger,
    *,
    strict_numbers: bool = True,
    question: str = "",
) -> Verdict:
    """校验答案每条断言是否被账本蕴含。返回 Verdict（ok=False 时附不可证片段）。

    ``question`` 供「复述豁免」使用（见 :func:`_echoes_question`）；不传即退回旧行为。
    """
    if not answer or not answer.strip():
        return Verdict(ok=True)  # 空答由上层其它逻辑处理

    body = _strip_code_blocks(answer)
    question_norm = _normalize_echo(question)
    unverified: list[str] = []

    # 1) 具名实体：反引号标识符 + 书名号名词，必须在账本
    named: set[str] = set()
    for m in _BACKTICK.finditer(body):
        named.add(m.group(1).strip())
    for m in _BOOKNAME.finditer(body):
        named.add(m.group(1).strip())
    for token in named:
        if (
            _SQL_LIKE.match(token)
            or _looks_like_clause(token)
            or _echoes_question(token, question_norm)
        ):
            continue
        # 复合串（字段清单 / 血缘路径）拆开逐段核：全段可证才算证。
        parts = [p for p in _COMPOSITE_SEP.split(token) if p.strip()]
        if len(parts) > 1 and all(_provable_name(ledger, p) for p in parts):
            continue
        if not _provable_name(ledger, token):
            unverified.append(f"提及了本体中未证实的「{token}」")

    # 2) 数值断言：带业务单位的数字须对得到 run_sql 单元格（strict 模式）
    if strict_numbers:
        for m in _NUMERIC.finditer(body):
            # 噪声上下文（近 7 天 / 第 2 步 / Top 10 / 2024 年）跳过
            ctx = body[max(0, m.start() - 6): m.end() + 2]
            if _NUM_NOISE_CONTEXT.search(ctx):
                continue
            num = m.group(1)
            # 用户问句里就写着的数字（「取前 50 个科目」→ 答案复述「50 个科目」）
            # 不是模型算出来的结论值，不该要 run_sql 凭证。实测里 `50个` 就栽在这。
            if question_norm and _normalize_echo(num) in question_norm:
                continue
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
