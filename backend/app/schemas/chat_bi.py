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


class ChatBiAgentStep(BaseModel):
    """Agent 工具编排的一步轨迹（供前端可折叠步骤条 + 审计回放）。"""

    index: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "succeeded"  # succeeded / failed
    summary: str | None = None


class ChatBiDataResult(BaseModel):
    """run_sql 返回的真实数据（供前端结果表格）。"""

    columns: list[dict[str, Any]] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


class ChatBiAskRequest(BaseModel):
    domain_id: str
    question: str
    history: list[dict[str, Any]] | None = None
    conversation_id: str | None = None


class ChatBiClarification(BaseModel):
    """需要用户澄清的缺口（P4.1）。``options`` 必须来自工具返回的真实实体。"""

    question: str
    options: list[str] = Field(default_factory=list)
    reason: str = ""


class ChatBiBlock(BaseModel):
    """渲染块（V3 S0）：Data Agent 回答由一串有类型的块组成，前端按 ``type`` 查注册表渲染。

    块携带的字段随 ``type`` 变化——markdown.content / sql.sql /
    table.columns+rows / mapping.variant+items / steps.steps /
    notice.level+variant / clarify.clarification / refs.objects+logics——
    故允许额外字段：S1 的 chart / lineage / draft_proposal 块新增字段无需改本模型。
    """

    model_config = {"extra": "allow"}

    id: str
    type: str  # markdown|sql|table|mapping|steps|notice|clarify|refs


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
    # Agent 工具编排（P1）：过程轨迹 + run_sql 真实结果。旧数据/Mock 路径可为空。
    steps: list[ChatBiAgentStep] = Field(default_factory=list)
    data_result: ChatBiDataResult | None = None
    # P4.1 澄清反问：模型判定缺口只能由用户补齐时，本轮不作答而是回问。
    # 与 grounding_refused 是**两种不同结局**——拒答是「答不了」，澄清是「先确认再答」。
    clarification: ChatBiClarification | None = None
    # V3 S0 渲染块协议：由 chat_bi_blocks.answer_to_blocks 从上述扁平字段投影而来。
    # 双写——旧字段全部保留，前端优先用 blocks、缺失时本地 answerToBlocks 兜底旧消息。
    blocks: list[ChatBiBlock] = Field(default_factory=list)
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


class ChatBiTaskLinkRequest(BaseModel):
    """P1：把「本会话催生的数据任务（治理制品）」关联到会话。

    用户对 Data Agent 的任务提案点「去校验并执行」建出制品后，前端调用记录此关联，
    使该会话后续能用 get_task_status 免 id 追踪。
    """

    artifact_id: str
    kind: str | None = None
    intent: str | None = None


class ChatBiExternalToolCreate(BaseModel):
    """P4：注册一个配置驱动的外部工具（HTTP）。"""

    name: str  # 小写 snake_case，全局唯一，不得与原生工具同名
    description: str
    url: str
    parameters: dict[str, Any] | None = None  # OpenAI function 的 JSON-Schema 入参
    method: str = "POST"
    auth_header: str | None = None  # 机密：整串作请求头值（如 "Bearer xxx"），不回显
    domain_id: str | None = None  # 空=全局
    display_name: str | None = None
    result_max_chars: int = 4000


class ChatBiExternalToolUpdate(BaseModel):
    enabled: bool


class ChatBiPreferenceRequest(BaseModel):
    """P3.1：把用户确认的约定落库为本域记忆。"""

    domain_id: str
    text: str


class ChatBiExternalToolOut(BaseModel):
    id: str
    name: str
    display_name: str | None = None
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    method: str
    url: str
    has_auth: bool = False  # 机密不回显，仅标识是否配置了鉴权头
    enabled: bool
    domain_id: str | None = None
    result_max_chars: int
    created_at: datetime

    model_config = {"from_attributes": True}


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


class ChatBiExecuteRequest(BaseModel):
    """执行某条回答的 suggested_sql。"""

    data_source_id: str
    limit: int = 100


class ChatBiExecuteResult(BaseModel):
    message_id: str
    sql: str
    columns: list[dict] = []
    rows: list[dict] = []
    row_count: int = 0
