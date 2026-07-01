"""Relation Type 结构类型（SSOT §5.3：外键、桥表、事实表等）。"""

RELATION_STRUCTURE_TYPES = frozenset({"foreign_key", "bridge_table", "fact_table", "other"})

RELATION_STRUCTURE_LABELS: dict[str, str] = {
    "foreign_key": "外键关系",
    "bridge_table": "桥表",
    "fact_table": "事实表",
    "other": "其他",
}


def infer_relation_structure_type(
    description: str | None = None,
    source_evidence: str | None = None,
) -> str:
    """根据描述与证据文本推断关系结构类型。"""
    text = f"{description or ''} {source_evidence or ''}".lower()
    if "外键" in text or "foreign" in text:
        return "foreign_key"
    if "桥" in text or "bridge" in text:
        return "bridge_table"
    if "事实" in text or "fact_" in text or "fact table" in text:
        return "fact_table"
    return "other"


def validate_relation_structure_type(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in RELATION_STRUCTURE_TYPES:
        return "无效的关系结构类型"
    return None
