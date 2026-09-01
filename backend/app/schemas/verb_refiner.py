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


class VerbRefinementBatchOut(BaseModel):
    """批量动词细化结果。"""
    suggestions: list[VerbSuggestion]
    total: int
    rule_count: int
    llm_count: int
    fallback_count: int


class VerbRefinementApplyRequest(BaseModel):
    """应用动词细化建议的请求。"""
    relation_id: str
    new_verb: str
    operator: str | None = None


class VerbRefinementBatchApplyRequest(BaseModel):
    """批量应用动词细化建议的请求。"""
    items: list[VerbRefinementApplyRequest]
    operator: str | None = None
