"""
S2 空动词细化服务：根据外键列名规则推断精确动词，剩余批量送 LLM 重命名。

设计要点（ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md S2）：
- 规则推断：supplier_id → "下给"，company_id → "隶属于"，parent_* → "上级"
- LLM 批量：规则覆盖不到的，构建 prompt 批量请求 LLM
- 待复核队列：结果不直接改 display_name，而是生成建议进待复核
- 三元组展示：改完之前，界面显示三元组短语而非裸动词
"""
import re
from typing import Optional

from app.models.ontology import RelationType


# 空泛动词：说不出业务语义的那几个。既是全本体扫描的候选口径，
# 也是建议的**下限**——把「引用」换成「属于」不叫细化，队列里那条告警一个字都不会少。
EMPTY_VERBS = frozenset({"属于", "引用", "关联", "关系", "连接"})


# S2 规则：外键列名 -> 精确动词映射
FOREIGN_KEY_VERB_RULES = {
    # 供应链
    r"supplier(_id)?$": "下给",
    r"vendor(_id)?$": "采购自",
    r"customer(_id)?$": "服务",
    r"buyer(_id)?$": "卖给",

    # 组织归属
    r"company(_id)?$": "隶属于",
    r"dept(_id)?$": "隶属于",
    r"department(_id)?$": "隶属于",
    r"org(_id)?$": "隶属于",
    r"organization(_id)?$": "隶属于",

    # 层级
    r"parent(_id|_\w+)?$": "上级",
    r"superior(_id)?$": "上级",
    r"manager(_id)?$": "汇报给",
    r"leader(_id)?$": "汇报给",

    # 产品/商品
    r"product(_id)?$": "涉及",
    r"sku(_id)?$": "涉及",
    r"item(_id)?$": "涉及",
    r"goods(_id)?$": "涉及",

    # 订单/交易
    r"order(_id)?$": "包含",
    r"transaction(_id)?$": "产生",
    r"trade(_id)?$": "产生",

    # 地理/区域
    r"region(_id)?$": "位于",
    r"area(_id)?$": "位于",
    r"city(_id)?$": "位于",
    r"province(_id)?$": "位于",
    r"country(_id)?$": "位于",

    # 分类/类型
    r"category(_id)?$": "属于",
    r"type(_id)?$": "属于",
    r"class(_id)?$": "属于",

    # 状态/标签
    r"status(_id)?$": "处于",
    r"state(_id)?$": "处于",
    r"tag(_id)?$": "标记为",
    r"label(_id)?$": "标记为",
}


def infer_verb_from_foreign_key(
    source_obj_name: str,
    target_obj_name: str,
    foreign_key_column: Optional[str],
    relation_name: str,
) -> Optional[str]:
    """根据外键列名和对象名推断精确动词（S2 规则）。

    Args:
        source_obj_name: 源对象名称（如 purchase_order）
        target_obj_name: 目标对象名称（如 supplier）
        foreign_key_column: 外键列名（如 supplier_id）
        relation_name: 关系名称（如 purchase_order_supplier）

    Returns:
        推断的动词，如 "下给"；规则未覆盖返回 None
    """
    if not foreign_key_column:
        return None

    fk_lower = foreign_key_column.lower()

    # 遍历规则匹配
    for pattern, verb in FOREIGN_KEY_VERB_RULES.items():
        if re.search(pattern, fk_lower):
            return verb

    # 规则未覆盖
    return None


def compact_relation_term(raw_term: Optional[str]) -> str:
    """压缩关系术语：抽取核心动词或截断过长文本。

    这是后备规则，用于 LLM 未覆盖或规则推断失败的情况。
    """
    if not raw_term:
        return "关联"

    text = raw_term.strip()

    # 已是简短动词
    if len(text) <= 8 and not re.search(r"关联\s|加工至|->|至|通过|血缘", text):
        return text

    # 抽取常见动词
    verb_match = re.search(
        r"(属于|包含|下单|引用|派生|关联|归属|拥有|参与|产生|组成|依赖|影响|生成|"
        r"汇总|对账|结算|统计|清洗|加工|标准化|报表|核对|刻画|度量|支撑|下给|"
        r"采购自|服务|卖给|隶属于|上级|汇报给|涉及|位于|处于|标记为)",
        text,
    )
    if verb_match:
        return verb_match.group(1)

    # 截断过长
    if len(text) > 8:
        return text[:8]

    return text


def build_llm_renaming_prompt(relations: list[RelationType]) -> str:
    """构建 LLM 批量重命名 prompt（S2 批量处理）。

    Args:
        relations: 待重命名的关系列表（规则未覆盖的空动词关系）

    Returns:
        LLM prompt，要求输出 JSON 数组
    """
    relation_items = []
    for rel in relations[:50]:  # 限制一次最多 50 条
        source_name = rel.source_object_type.display_name or rel.source_object_type.name
        target_name = rel.target_object_type.display_name or rel.target_object_type.name
        current_verb = rel.display_name or "关联"
        desc = rel.description or ""

        relation_items.append(
            f"  - 源: {source_name}, 目标: {target_name}, "
            f"当前动词: {current_verb}, 描述: {desc}"
        )

    prompt = f"""你是数据治理专家，需要为以下业务关系推断精确的关系动词（2-4字）。

要求：
1. 动词应准确反映两个对象之间的业务语义（如：下给、隶属于、服务、包含、位于）
2. 避免空泛词（如：关联、引用）
3. 考虑业务常识和描述证据
4. 输出 JSON 数组，格式：[{{"index": 0, "verb": "下给"}}, ...]

待重命名关系（共 {len(relations)} 条）：
{chr(10).join(relation_items)}

输出 JSON："""

    return prompt


def suggest_verb_refinements(
    relations: list[RelationType],
) -> list[dict]:
    """为空动词关系生成精化建议（S2 入口）。

    Args:
        relations: 待处理的关系列表（已过滤出 display_name 空泛的关系）

    Returns:
        建议列表，每项包含：
        - relation_id: 关系 ID
        - current_verb: 当前动词
        - suggested_verb: 建议动词
        - method: 推断方法（rule / llm / fallback）
        - confidence: 置信度（rule=0.9, llm=0.7, fallback=0.3）

    注：LLM 调用需要在外层实现，此函数仅返回规则推断结果
    """
    suggestions = []

    for rel in relations:
        current_verb = rel.display_name or "关联"
        source_name = rel.source_object_type.name if rel.source_object_type else ""
        target_name = rel.target_object_type.name if rel.target_object_type else ""

        # 尝试规则推断
        fk_column = _extract_foreign_key_column(rel)
        suggested_verb = infer_verb_from_foreign_key(
            source_name, target_name, fk_column, rel.name
        )

        if suggested_verb:
            suggestions.append({
                "relation_id": rel.id,
                "current_verb": current_verb,
                "suggested_verb": suggested_verb,
                "method": "rule",
                "confidence": 0.9,
            })
        else:
            # 规则未覆盖，标记为需要 LLM 处理
            fallback_verb = compact_relation_term(current_verb)
            suggestions.append({
                "relation_id": rel.id,
                "current_verb": current_verb,
                "suggested_verb": fallback_verb,
                "method": "fallback",  # 外层应送 LLM 批量处理
                "confidence": 0.3,
            })

    return suggestions


def _extract_foreign_key_column(rel: RelationType) -> Optional[str]:
    """从关系证据中提取外键列名。"""
    if not rel.source_evidence:
        return None

    # 尝试从证据文本中提取列名
    # 格式示例："外键: supplier_id" 或 "foreign_key: supplier_id"
    fk_match = re.search(r"(?:外键|foreign[_ ]?key)[:：]\s*(\w+)", rel.source_evidence, re.I)
    if fk_match:
        return fk_match.group(1)

    # 本项目的生成器实际写的是这一句：「A 通过引用字段 company 关联 B（推断…）」。
    # 只认「外键:」的话，规则这一路在真实数据上等于没开——1279 条关系里只有 1 条能命中，
    # 其余全落到 LLM 兜底，模型一挂就一条建议都给不出。与前端 parseJoinKey 同一口径。
    ref_match = re.search(
        r"引用字段\s*[`\"']?([A-Za-z_][A-Za-z0-9_]*)[`\"']?\s*关联", rel.source_evidence
    )
    if ref_match:
        return ref_match.group(1)

    # 从关系名称推断（如 order_supplier -> supplier）
    parts = rel.name.split("_")
    if len(parts) >= 2:
        return parts[-1]  # 取最后一个部分作为外键列名

    return None
