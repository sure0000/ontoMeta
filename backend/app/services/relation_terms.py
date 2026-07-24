import re

RELATION_TERM_MAX_LENGTH = 8

_VERB_PATTERN = re.compile(
    r"(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成"
    r"|汇总|对账|结算|统计|清洗|加工|标准化|报表)"
)


def compact_relation_term(value: str) -> str:
    """将句子式关系描述压缩为简短语义词。"""
    text = value.strip()
    if not text:
        return text

    for pattern in (
        r"^.+?\s*关联\s*(.+)$",
        r"^.+?\s*加工至\s*(.+)$",
        r"^.+?\s*->\s*(.+)$",
    ):
        if re.search(pattern, text):
            match = _VERB_PATTERN.search(text)
            if match:
                return match.group(1)

    match = _VERB_PATTERN.search(text)
    if match:
        return match.group(1)

    if len(text) > RELATION_TERM_MAX_LENGTH:
        return text[:RELATION_TERM_MAX_LENGTH]

    return text


_LINEAGE_TARGET_TERMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("对账", "reconcil"), "对账生成"),
    (("汇总", "summary", "统计", "stat"), "统计汇总"),
    (("报表", "report"), "生成报表"),
    (("结算", "settle"), "结算生成"),
    (("清洗", "clean", "标准化", "standard"), "清洗加工"),
)


def infer_relation_term(
    kind: str, field_name: str | None = None, target_label: str | None = None
) -> str:
    """根据关系类型推断默认关系语义词。

    仅在 LLM 未给出(或给出的)业务命名未通过校验时使用，因此这里只是保底：
    lineage 关系没有真实变换逻辑可用，退而其次按目标对象的业务展示名做关键词
    匹配(如「汇总」「对账」「报表」)，给出比笼统「派生」更贴近业务的默认词；
    无法匹配任何关键词时退回「加工生成」，仍比「派生」更能表达"由源加工产出"
    这一含义。
    """
    if kind == "lineage":
        label = (target_label or "").lower()
        for keywords, term in _LINEAGE_TARGET_TERMS:
            if any(kw in label for kw in keywords):
                return term
        return "加工生成"

    if kind == "foreign_key":
        lowered = (field_name or "").lower()
        if any(token in lowered for token in ("parent", "owner", "dept", "department", "部门")):
            return "属于"
        if any(token in lowered for token in ("contain", "item", "detail", "line", "明细")):
            return "包含"
        if any(token in lowered for token in ("order", "订单")):
            return "下单"
        return "属于"

    return "关联"


def validate_relation_term(value: str) -> str | None:
    text = value.strip()
    if not text:
        return "关系语义不能为空"
    if len(text) > RELATION_TERM_MAX_LENGTH:
        return f"关系语义应为简短词语（不超过 {RELATION_TERM_MAX_LENGTH} 字）"
    if re.search(r"[。；！？]", text):
        return "请使用词语而非完整句子，详细说明写在语义描述中"
    if re.search(r"\s{2,}|关联\s|加工至|表", text):
        return "请只填写关系动词，如「属于」「包含」「下单」"
    return None
