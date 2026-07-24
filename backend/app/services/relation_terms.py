import re

RELATION_TERM_MAX_LENGTH = 8

_VERB_PATTERN = re.compile(
    r"(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成"
    r"|汇总|对账|结算|统计|清洗|加工|标准化|报表|核对|刻画|度量|支撑)"
)


def compact_relation_term(value: str) -> str:
    """将句子式关系描述压缩为简短语义词。"""
    text = value.strip()
    if not text:
        return text

    # 已是简短、不含句子/连接词的干净谓词(如「对账为」「汇总为」「属于」)，
    # 直接保留，避免被 _VERB_PATTERN 抽成单动词而丢掉方向后缀。
    if len(text) <= RELATION_TERM_MAX_LENGTH and not re.search(
        r"关联\s|加工至|->|至|通过|血缘", text
    ):
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


# 血缘/派生关系的默认谓词（对齐 PROV-O 溯源语义）。
#
# 本项目的血缘边方向为 source→target（源加工产出 target），因此默认词必须是
# 能读成「源 [谓词] 目标」三元组的**前向谓词**（如「订单明细 汇总为 结算表」），
# 而非「结算生成」这类「产出物+动作」名词。按目标对象语义细分变换类型，
# 保留区分度；无法匹配时退回 PROV 风格的「派生出」（wasDerivedFrom 的前向形），
# 仍比统一的「生成/加工」更能表达「由源派生而来」这一溯源含义。
_LINEAGE_TARGET_TERMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("对账", "reconcil"), "对账为"),
    (("结算", "settle"), "结算为"),
    (("汇总", "summary"), "汇总为"),
    (("统计", "stat", "指标", "metric", "报表", "report", "看板", "dashboard"), "统计为"),
    (("标签", "tag", "画像", "profile"), "刻画"),
    (("清洗", "clean", "标准化", "standard", "dwd"), "清洗为"),
)


def infer_relation_term(
    kind: str, field_name: str | None = None, target_label: str | None = None
) -> str:
    """根据关系类型推断默认关系语义词。

    仅在 LLM 未给出(或给出的)业务命名未通过校验时使用，因此这里只是保底：
    血缘属于溯源/派生范畴(PROV-O)，默认按目标对象的业务展示名做关键词匹配，
    给出方向正确、能读成三元组的前向谓词(如「汇总为」「对账为」)，而非笼统的
    「派生」或「…生成」；无法匹配任何关键词时退回 PROV 风格的「派生出」。
    """
    if kind == "lineage":
        label = (target_label or "").lower()
        for keywords, term in _LINEAGE_TARGET_TERMS:
            if any(kw in label for kw in keywords):
                return term
        return "派生出"

    if kind == "foreign_key":
        lowered = (field_name or "").lower()
        if any(token in lowered for token in ("parent", "owner", "dept", "department", "部门", "上级")):
            return "属于"
        if any(token in lowered for token in ("contain", "item", "detail", "line", "明细", "子")):
            return "包含"
        if any(token in lowered for token in ("order", "订单")):
            return "下单"
        if any(token in lowered for token in ("ref", "引用")):
            return "引用"
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
