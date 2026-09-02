"""动词细化相关的请求和响应模型。"""
from pydantic import BaseModel


class VerbSuggestion(BaseModel):
    """单个动词建议。"""
    relation_id: str
    current_verb: str
    suggested_verb: str
    method: str  # "rule" | "llm" | "fallback"
    confidence: float  # 0.0-1.0
    source_object_name: str
    target_object_name: str


class VerbRefinementSuggestRequest(BaseModel):
    """生成建议的范围。

    ``relation_ids`` 为空表示全本体扫描（保留旧行为）；给出时只对这一批关系出建议，
    对应审核台「一屏判一组」的工作单元——细化的范围必须和人刚看过的那批一致，
    否则采纳会连带改掉屏幕外的关系。
    """
    relation_ids: list[str] | None = None


class VerbRefinementBatchOut(BaseModel):
    """批量动词细化结果。"""
    suggestions: list[VerbSuggestion]
    total: int
    rule_count: int
    llm_count: int
    fallback_count: int
    #: 本次扫过的关系条数（含改不动、因此没进 suggestions 的那些）。
    candidate_count: int = 0
    #: LLM 这一路的下场：unused（规则全覆盖）/ unavailable（没配模型）/ ok / failed。
    #: 「一条建议都没有」得能分清是没得改还是没跑成。
    llm_status: str = "unused"


class VerbRefinementApplyItem(BaseModel):
    """应用动词细化建议的单项请求。"""
    relation_id: str
    new_verb: str
    operator: str | None = None


class VerbRefinementBatchApplyRequest(BaseModel):
    """批量应用动词细化建议的请求。"""
    items: list[VerbRefinementApplyItem]
    operator: str | None = None
