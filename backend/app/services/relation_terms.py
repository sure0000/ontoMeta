import re

RELATION_TERM_MAX_LENGTH = 8

_VERB_PATTERN = re.compile(
    r"(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成"
    r"|汇总|对账|结算|统计|清洗|加工|标准化|报表|核对|刻画|度量|支撑)"
)

# 校验禁止的内容（与 validate_relation_term 保持一致）：句子标点、连续空白、
# 「关联 」「加工至」这类连接短语、以及名词后缀「表」。compact 的产出必须避开这一集合，
# 否则生成的关系词能落库、却在人工编辑时被 validate 打回（生成/限制不一致）。
_FORBIDDEN_TERM = re.compile(r"[。；！？]|\s{2,}|关联\s|加工至|表")
# 句子/连接标记：命中说明是描述句而非干净谓词，应抽取其中动词而非原样保留。
_SENTENCE_MARKERS = re.compile(r"->|至|通过|血缘|加工至|关联\s")


def _is_clean_term(text: str) -> bool:
    """是否为可直接作关系语义词的干净短谓词（长度达标且不含禁止内容）。"""
    return bool(text) and len(text) <= RELATION_TERM_MAX_LENGTH and not _FORBIDDEN_TERM.search(text)


def compact_relation_term(value: str) -> str:
    """将句子式关系描述压缩为简短语义词，且**保证**产出能通过 validate_relation_term。

    优先级：已是干净短谓词→原样保留；描述句→抽取其中动词；仍不达标→剥离禁止字符后
    截断；最终兜底动词「关联」。避免生成期落库的关系词在编辑期被校验打回。
    """
    text = value.strip()
    if not text:
        return text

    # 已是干净短谓词(如「对账为」「属于」)且非描述句 → 原样保留。
    if _is_clean_term(text) and not _SENTENCE_MARKERS.search(text):
        return text

    # 描述句 → 抽取其中的动词。
    match = _VERB_PATTERN.search(text)
    if match:
        return match.group(1)

    # 兜底：剥离禁止字符 + 截断；仍不达标则退回通用动词「关联」。
    cleaned = re.sub(r"[。；！？]|表|\s+", "", text)[:RELATION_TERM_MAX_LENGTH]
    return cleaned if _is_clean_term(cleaned) else "关联"



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

# 生命周期阶段前缀：同一业务实体在流程中的阶段限定词。去掉后核心业务名相等，
# 说明两张表是**同一实体的不同阶段**（潜客商机 vs 商机、客户 vs 潜在客户），
# 其间的血缘不是「派生」而是业务上的「转化」。
_STAGE_PREFIXES: tuple[str, ...] = (
    "潜客", "潜在", "意向", "预", "待", "临时", "草稿", "新", "老", "历史", "初", "拟",
)

# CRM 漏斗跨名相邻阶段：核心名不同但业务上是同一条转化链的相邻环节
# （线索/潜客 → 商机）。命中即判「转化」，补齐去前缀核心名法覆盖不到的跨名转化。
_FUNNEL_LEAD_TOKENS: tuple[str, ...] = ("线索", "潜客", "潜在客户")
_FUNNEL_OPP_TOKENS: tuple[str, ...] = ("商机",)

# 父子明细后缀：target 以 source 打头且以这些词收尾 → source 「包含」其明细行。
_DETAIL_SUFFIXES: tuple[str, ...] = ("明细", "明细行", "行", "项", "条目", "子项")

# 引用类目标关键词：反向血缘翻转为「单据引用主数据」时，按主数据目标语义给出
# 方向正确的引用谓词；无匹配时退回通用「引用」。
_REFERENCE_TARGET_TERMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("地址", "国家", "地区", "区域", "位置", "城市", "省"), "位于"),
    (("公司", "部门", "组织", "机构", "科目", "成本中心"), "属于"),
    (("币种", "货币", "汇率"), "采用"),
)


def _strip_stage_prefix(label: str) -> str:
    """去掉生命周期阶段前缀，返回核心业务名（去掉后过短则退回原名）。"""
    core = label.strip()
    for prefix in _STAGE_PREFIXES:
        if core.startswith(prefix) and len(core) - len(prefix) >= 2:
            core = core[len(prefix) :]
            break
    return core


def _is_lifecycle_transition(source_label: str | None, target_label: str | None) -> bool:
    """两张表是否为同一业务实体的生命周期阶段（其间血缘应命名为「转化」）。

    - 去阶段前缀后核心业务名**相等**（潜客商机↔商机、客户↔潜在客户）；或
    - 命中 CRM 漏斗跨名相邻阶段（线索/潜客 ↔ 商机）。

    需排除仅共享一个词但并非同一实体阶段的情形（如 客户 vs 客户分组：核心名
    「客户」≠「客户分组」，且不在漏斗里 → 不判转化）。
    """
    s = (source_label or "").strip()
    t = (target_label or "").strip()
    if not s or not t or s == t:
        return False

    core_s = _strip_stage_prefix(s)
    core_t = _strip_stage_prefix(t)
    if core_s and core_t and core_s == core_t:
        return True

    s_lead = any(tok in s for tok in _FUNNEL_LEAD_TOKENS)
    t_lead = any(tok in t for tok in _FUNNEL_LEAD_TOKENS)
    s_opp = any(tok in s for tok in _FUNNEL_OPP_TOKENS)
    t_opp = any(tok in t for tok in _FUNNEL_OPP_TOKENS)
    # 一端在「线索/潜客」环节、另一端在「商机」环节 → 漏斗相邻转化（方向无关）。
    return (s_lead and t_opp) or (s_opp and t_lead)


def _is_parent_detail(source_label: str | None, target_label: str | None) -> bool:
    """target 是否为 source 的明细/子表（source 「包含」target）。"""
    s = (source_label or "").strip()
    t = (target_label or "").strip()
    if not s or not t or s == t:
        return False
    return t.startswith(s) and any(t.endswith(suf) for suf in _DETAIL_SUFFIXES)


def reference_term(target_label: str | None = None) -> str:
    """反向血缘翻转为「单据引用主数据」后，按主数据目标语义给出引用谓词。

    主数据目标（地址/公司/币种…）→ 位于/属于/采用；无匹配退回通用「引用」。
    """
    label = (target_label or "").lower()
    orig = target_label or ""
    for keywords, term in _REFERENCE_TARGET_TERMS:
        if any(kw in orig or kw in label for kw in keywords):
            return term
    return "引用"


def infer_relation_term(
    kind: str,
    field_name: str | None = None,
    target_label: str | None = None,
    source_label: str | None = None,
) -> str:
    """根据关系类型推断默认关系语义词。

    仅在 LLM 未给出(或给出的)业务命名未通过校验时使用，因此这里只是保底：
    血缘属于溯源/派生范畴(PROV-O)。命名按序判定——先看是否为同一实体的生命周期
    「转化」(潜客商机→商机)、父子明细「包含」(商机→商机明细)，再按目标对象展示名
    做加工类关键词匹配(汇总为/对账为)，给出方向正确、能读成三元组的前向谓词；
    都不命中时退回「派生出」，由上层(evidence_builder)决定翻转为引用或保留。
    """
    if kind == "lineage":
        if _is_lifecycle_transition(source_label, target_label):
            return "转化"
        if _is_parent_detail(source_label, target_label):
            return "包含"
        label = (target_label or "").lower()
        orig = target_label or ""
        for keywords, term in _LINEAGE_TARGET_TERMS:
            if any(kw in orig or kw in label for kw in keywords):
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
