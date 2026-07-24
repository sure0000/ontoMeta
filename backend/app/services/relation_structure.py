"""Relation Type 结构类型（SSOT §5.3：外键、桥表、事实表、派生/溯源等）。

结构类型把关系分到不同的**语义范畴**，其中：
- foreign_key / bridge_table / fact_table 是**业务关联关系**（真实业务事实，ObjectProperty）；
- derivation 是**溯源/派生关系**（数据血缘，对齐 W3C PROV-O 的 wasDerivedFrom）——
  它描述「这份数据由那份数据加工而来」，属于 provenance 范畴，本身不是业务事实，
  因此与业务关联关系分开分类，命名也用「派生出/汇总为/对账为」这类前向谓词。
"""

RELATION_STRUCTURE_TYPES = frozenset(
    {"foreign_key", "bridge_table", "fact_table", "derivation", "other"}
)

RELATION_STRUCTURE_LABELS: dict[str, str] = {
    "foreign_key": "外键关系",
    "bridge_table": "桥表",
    "fact_table": "事实表",
    "derivation": "派生/溯源",
    "other": "其他",
}


def infer_relation_structure_type(
    description: str | None = None,
    source_evidence: str | None = None,
) -> str:
    """根据描述与证据文本推断关系结构类型。

    先判业务关联结构（外键、桥表），再判溯源/派生（血缘），最后判事实表/其他。
    血缘/派生类关系(description 形如「血缘：A 加工至 B」)归入 derivation，与业务
    关联关系区分开——它是数据溯源(PROV-O)而非业务事实。
    """
    text = f"{description or ''} {source_evidence or ''}".lower()
    if "外键" in text or "foreign" in text:
        return "foreign_key"
    if "桥" in text or "bridge" in text:
        return "bridge_table"
    if "血缘" in text or "lineage" in text or "加工至" in text or "派生" in text:
        return "derivation"
    if "事实" in text or "fact_" in text or "fact table" in text:
        return "fact_table"
    return "other"


def validate_relation_structure_type(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in RELATION_STRUCTURE_TYPES:
        return "无效的关系结构类型"
    return None
