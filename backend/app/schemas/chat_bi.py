from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

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


class ChatBiFormOption(BaseModel):
    """表单候选项：**显示什么**（label）与**回填什么**（value）分开。

    此前两者是同一个字符串，于是需要带 id 的候选（数据源、对象）只能写成「名称｜id」，
    那串 id 就直接糊在下拉里给人看。分开之后：界面只显示 label，id 留在 value 里，
    提交时随回填文本带回给模型。

    ``disabled`` 用于「摆出来但选不了」的候选——执行侧不支持的装载方式必须**看得见**
    （否则用户以为系统只会全量），但不能真被选中（与 MaterializeModal 的置灰同口径）。
    """

    label: str
    value: str
    disabled: bool = False


class ChatBiFormField(BaseModel):
    """交互表单的单个字段（P6）。``type`` 决定前端用哪种控件渲染。

    ``options`` 仅对 select/multiselect/radio/autocomplete 有意义，且**必须来自工具返回
    的真实实体**（与 clarification.options 同一约束）；没有候选项时应退化为 text/number
    让用户自填。为兼容纯字符串候选，字符串会被归一为 ``label == value`` 的候选项。
    """

    name: str  # 字段标识（回填时作键）
    label: str  # 中文标签
    # text/textarea/number/select/multiselect/radio/boolean/date/autocomplete/cron
    type: str
    options: list[ChatBiFormOption] = Field(default_factory=list)
    required: bool = False
    placeholder: str | None = None
    help: str | None = None
    default: Any | None = None
    # 建数确认向导中的所属环节：requirement / ontology / data。通用表单留空。
    confirmation_node: str | None = None
    # 级联候选：depends_on 字段当前值 → options_by_value[value]。
    depends_on: str | None = None
    options_by_value: dict[str, list[ChatBiFormOption]] = Field(default_factory=dict)
    # 候选实时取：``object_properties`` = 取 depends_on 那个对象的字段清单。
    # 静态摊开几百个对象的字段是几 MB 的消息负载，故这类候选按需拉。
    options_from: str | None = None
    # 条件可见：``{"field": "mode", "in": ["incremental", "cdc"]}``。不满足时前端既不
    # 渲染、也不校验、更不把值提交上来——避免「先选了 CDC 填了 sequence 列，又改回全量」
    # 时把一个不该生效的参数留在 Spec 里（它会真的进建表语句）。
    visible_when: dict[str, Any] | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, v: Any) -> Any:
        """字符串候选项归一为 {label, value}——老的纯字符串写法仍然合法。"""
        if not isinstance(v, list):
            return v
        return [{"label": o, "value": o} if isinstance(o, str) else o for o in v]


class ChatBiConfirmationStep(BaseModel):
    """一个数据任务的一环人审；node 与决策闭环的六环同名。

    ``phase`` 说明这一环在**哪儿**确认：``form`` = 对话内的表单向导（需求/本体/数据），
    ``artifact`` = 任务详情抽屉（执行方案/执行/结果）。表单一次给全六环，前端据此把
    「还剩几环、下一环在哪确认」画在同一条进度上，而不是让人以为填完表单就完事了。
    """

    node: str
    title: str
    description: str = ""
    phase: str = "form"


class ChatBiFormRequest(BaseModel):
    """Agent 动态生成的**可填写表单**（P6）：一次向用户收集多个结构化参数。

    与 clarification 一样是**终态出口**——本轮到此为止，等用户在前端填完提交后作为
    新一轮问题带回（结构化回填文本进 history，Agent 据此继续）。表单只描述「要收集什么」，
    不携带业务结论，故不入接地账本、不参与拒答判定。适用于取数（指标+时间+维度）、
    建数任务（目标表+更新策略+调度）等需一次补齐多参数的场景。
    """

    title: str
    intent: str = ""
    #: 服务端改判/合并了这次请求时给人的一句解释（如「同步自带建表，已省掉物化那一步」）。
    #: 空 = 没有可说的。改判不能只在后台发生——人得知道自己拿到的为什么是这张表单。
    notice: str = ""
    submit_label: str = "提交"
    fields: list[ChatBiFormField] = Field(default_factory=list)
    task_kind: str | None = None
    ontology_id: str | None = None
    confirmation_id: str | None = None
    confirmation_steps: list[ChatBiConfirmationStep] = Field(default_factory=list)


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


class ChatBiDecisionRequest(BaseModel):
    """记一条人工决策留痕。

    **不含 operator/subject 字段**：责任人一律由服务端从已认证主体取，
    前端传了也会被忽略——否则「谁确认的」可被客户端伪造，追踪与管理就失去依据。
    """

    node: str
    outcome: str | None = None
    stage: str | None = None
    trigger: str | None = None
    message_id: str | None = None
    block_id: str | None = None
    summary: str | None = None
    proposed: Any | None = None
    chosen: Any | None = None
    ref_kind: str | None = None
    ref_id: str | None = None
    dedup_key: str | None = None


class ChatBiDecisionOut(BaseModel):
    id: str
    conversation_id: str
    # 仅跨会话查询（search_decisions）填充：会话内时间线本就在会话上下文里，不必重复。
    conversation_title: str | None = None
    message_id: str | None = None
    block_id: str | None = None
    seq: int = 0
    node: str
    stage: str | None = None
    trigger: str | None = None
    outcome: str
    subject_id: str | None = None
    subject_role: str | None = None
    summary: str | None = None
    proposed: Any | None = None
    chosen: Any | None = None
    overridden_fields: list[str] = []
    ref_kind: str | None = None
    ref_id: str | None = None
    created_at: datetime | None = None


class ChatBiClosureNode(BaseModel):
    node: str
    label: str
    reached: bool
    latest_outcome: str | None = None
    latest_at: datetime | None = None
    summary: str | None = None
    count: int = 0


class ChatBiClosureTask(BaseModel):
    """本会话催生的一条数据任务**及它自己的六环闭环**。

    闭环的粒度是任务，不是会话：一条会话可能连着建好几条任务，也可能通篇只是查数
    什么都没建。前者混成一组六环就读不出"哪一环是给哪条任务走的"，后者压根没有要
    闭的环。卡片一条任务一张，并据此给出「重新进入某一环」的入口。
    """

    artifact_id: str
    name: str
    kind: str | None = None
    status: str | None = None
    #: 催生它的表单向导 id；历史关联为空，此时前三环无从归属，如实标灰。
    confirmation_id: str | None = None
    nodes: list[ChatBiClosureNode] = []
    reached_count: int = 0
    total_count: int = 6
    dangling: list[str] = []


class ChatBiDecisionClosure(BaseModel):
    """一次对话的决策总结。

    ``tasks`` 是给人看的闭环——**一条任务一组六环**（恒六环，未到达的标灰而非隐藏）。
    会话级的 ``nodes``/``reached_count``/``dangling`` 是审计聚合，供跨会话统计与
    运行记录问答；界面不拿它当"闭环"画，否则一次纯查询也会顶着一张六环卡。
    """

    conversation_id: str
    nodes: list[ChatBiClosureNode]
    reached_count: int
    total_count: int
    dangling: list[str] = []
    tasks: list[ChatBiClosureTask] = []
    records: list[ChatBiDecisionOut] = []


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
