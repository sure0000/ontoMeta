from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

class ChatBiReference(BaseModel):
    id: str | None = None
    name: str | None = None
    display_name: str | None = None


class ChatBiCaliberReference(BaseModel):
    """口径拆解项引用的本体实体。kind 决定前端跳转目标。"""

    kind: str  # object_type / property / relation_type / business_logic
    id: str | None = None
    name: str | None = None
    display_name: str | None = None


class ChatBiCaliberItem(BaseModel):
    """口径拆解项：将用户问题拆解为若干步骤，每步映射到本体中的具体实体。"""

    label: str
    description: str | None = None
    references: list[ChatBiCaliberReference] = Field(default_factory=list)


class ChatBiAskRequest(BaseModel):
    domain_id: str
    question: str
    history: list[dict[str, Any]] | None = None
    conversation_id: str | None = None


class ChatBiAnswer(BaseModel):
    domain_id: str
    domain_name: str
    ontology_id: str | None = None
    answer: str
    suggested_sql: str | None = None
    caliber_decomposition: list[ChatBiCaliberItem] = Field(default_factory=list)
    referenced_objects: list[ChatBiReference] = Field(default_factory=list)
    referenced_logics: list[ChatBiReference] = Field(default_factory=list)
    used_mock: bool = False
    grounding_refused: bool = False
    conversation_id: str | None = None
    conversation_title: str | None = None


class ChatBiSuggestions(BaseModel):
    domain_id: str
    suggestions: list[str] = Field(default_factory=list)


# --- ChatBI · Conversation Management ---


class ChatBiConversationSummary(BaseModel):
    id: str
    domain_id: str
    title: str
    category: str | None = None
    is_pinned: bool = False
    is_archived: bool = False
    message_count: int = 0
    last_message_preview: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatBiConversationCreate(BaseModel):
    domain_id: str
    title: str | None = None
    category: str | None = None


class ChatBiConversationUpdate(BaseModel):
    title: str | None = None
    category: str | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None


class ChatBiMessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    payload: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- ChatBI · Category Management ---


class ChatBiCategoryItem(BaseModel):
    name: str
    conversation_count: int


class ChatBiCategoryList(BaseModel):
    categories: list[ChatBiCategoryItem]


class ChatBiCategoryRenameRequest(BaseModel):
    domain_id: str
    old_name: str
    new_name: str


class ChatBiCategoryDeleteRequest(BaseModel):
    domain_id: str
    name: str
