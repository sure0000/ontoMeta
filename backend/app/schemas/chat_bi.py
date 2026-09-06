from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# 表单结构住在 `schemas/task_form`（中性位置）——Web 任务面板与 MCP 建数流程都要用它，
# 而它们不该为了发一张表单先依赖对话模块。这里按原名再导出，前端契约不动。
from app.schemas.task_form import (  # noqa: F401
    ChatBiFormField,
    ChatBiFormOption,
    ChatBiFormRequest,
)


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
    """Agent 工具编排的一步轨迹（供前端可折叠步骤条 + 审计回放）。

    并非每一步都是工具调用：`kind="thought"` 是模型在调工具前写下的一句自述（`text` 为句子、
    `tool` 为空），`kind="repair"` 是自愈重写。流式路径发的是裸 dict 所以一直带着这两个字段，
    但非流式 `POST /chat-bi/ask` 声明了 `response_model=ChatBiAnswer`——模型里缺字段就会被
    **静默剥掉**，同一次问答两条路径给出的轨迹不一致，thought 步在那边退化成空白工具行。
    """

    index: int
    tool: str
    # tool | thought | repair
    kind: str = "tool"
    # 仅 thought / repair 有：那一步的人话；工具步的说明在 summary 里。
    text: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "succeeded"  # succeeded / failed
    summary: str | None = None


class ChatBiDataResult(BaseModel):
    """run_sql 返回的真实数据（供前端结果表格）。"""

    columns: list[dict[str, Any]] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


class ChatBiAskRequest(BaseModel):
    # 多域问答：domain_ids 非空 = 跨域接地；空 = 不选域（全域通盘）。
    domain_ids: list[str] = Field(default_factory=list)
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


class ChatBiAgentRunInfo(BaseModel):
    """One durable Data Agent turn. Its id is the assistant message id."""

    id: str
    status: str  # succeeded | refused | waiting_input | failed | cancelled
    question: str
    intent: str | None = None
    skill: str | None = None
    grounded: bool = False
    started_at: datetime
    finished_at: datetime
    error: str | None = None


class ChatBiAgentArtifactRef(BaseModel):
    """Safe index entry pointing into fields already stored in the run payload."""

    id: str
    kind: str
    label: str
    payload_path: str
    snapshot: dict[str, Any] | None = None
    source: str | None = None
    as_of: Any | None = None


class ChatBiAnswer(BaseModel):
    # 多域：domain_ids 为本次接地的域集合（空=全域通盘）；domain_id/domain_name 保留首域作锚点兼容。
    domain_ids: list[str] = Field(default_factory=list)
    domain_names: list[str] = Field(default_factory=list)
    domain_id: str | None = None
    domain_name: str = ""
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
    # P6 交互表单：需一次补齐多个结构化参数时，Agent 生成可填写表单收集上下文。
    # 与 clarification 同为终态出口——本轮结束、等用户填完提交带回；不入接地判定。
    form_request: ChatBiFormRequest | None = None
    # V3 S0 渲染块协议：由 chat_bi_blocks.answer_to_blocks 从上述扁平字段投影而来。
    # 双写——旧字段全部保留，前端优先用 blocks、缺失时本地 answerToBlocks 兜底旧消息。
    blocks: list[ChatBiBlock] = Field(default_factory=list)
    # Persistent run envelope. Legacy messages created before P4 may omit both fields.
    agent_run: ChatBiAgentRunInfo | None = None
    agent_artifacts: list[ChatBiAgentArtifactRef] = Field(default_factory=list)
    conversation_id: str | None = None
    conversation_title: str | None = None


class ChatBiSuggestions(BaseModel):
    domain_ids: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


# --- ChatBI · Conversation Management ---


class ChatBiConversationSummary(BaseModel):
    id: str
    domain_ids: list[str] = Field(default_factory=list)
    domain_id: str | None = None
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
    # domain_ids 空 = 不选域（全域通盘）会话
    domain_ids: list[str] = Field(default_factory=list)
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
    # 催生这条任务的表单向导 id。闭环按任务分开时，前三环靠它归属到这条任务上。
    confirmation_id: str | None = None
    # 决策留痕：提案原样 vs 人确认前改成的样子。两份都在前端手上（proposal.context 与
    # 本地编辑态），顺这一次已有的往返带回来，无需额外请求。
    proposed_context: dict[str, Any] | None = None
    chosen_context: dict[str, Any] | None = None
    message_id: str | None = None
    block_id: str | None = None


class ChatBiPreferenceRequest(BaseModel):
    """P3.1：把用户确认的约定落库为本域记忆。"""

    domain_id: str
    text: str


class ChatBiMessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    payload: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatBiAgentRunSummary(ChatBiAgentRunInfo):
    message_id: str
    artifact_count: int = 0
    answer_preview: str = ""
    created_at: datetime


class ChatBiAgentRunDetail(BaseModel):
    message_id: str
    run: ChatBiAgentRunInfo
    artifacts: list[ChatBiAgentArtifactRef] = Field(default_factory=list)
    payload: dict[str, Any]
    created_at: datetime


# --- ChatBI · Category Management ---


class ChatBiCategoryItem(BaseModel):
    name: str
    conversation_count: int


class ChatBiCategoryList(BaseModel):
    categories: list[ChatBiCategoryItem]


class ChatBiCategoryRenameRequest(BaseModel):
    # 多域：对所选域集合下的同名分类统一重命名。空 = 全域。
    domain_ids: list[str] = Field(default_factory=list)
    old_name: str
    new_name: str


class ChatBiCategoryDeleteRequest(BaseModel):
    domain_ids: list[str] = Field(default_factory=list)
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
