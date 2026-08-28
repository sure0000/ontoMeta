"""Data Agent 技能注册表（V3 S1）。

一个 skill = 面向某类数据工作的能力包：触发描述（给模型选）+ prompt 叠加片段
（该任务类型的专门工具选择策略）+ 解锁的额外工具 + 声明产出哪些渲染块。

**S1 取舍：只解锁不收窄**（对齐决策）。基础 12 工具永远可用，skill 只做两件事——
叠 prompt overlay + 解锁新工具（query 解锁 render_chart）。从不移除工具，故零回归；
也为 S2/S3 的 get_lineage / propose_draft「解锁」铺好机制。文档 §4 的「收窄工具」
留到 S1.x 或按需再做。

路由是 opt-in 的：模型按 `when_to_use` 自己调 select_skill；不选=现有通用行为
（护住 V2 的 avg_llm_calls）。见 chat_bi.py 的 `_apply_select_skill`。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Skill:
    name: str
    display: str
    # 给 select_skill 工具描述用——模型据此判断该不该选这个技能。
    when_to_use: str
    # 叠加到基座 system prompt 之后的该任务类型专门策略。
    prompt_overlay: str
    # 该技能额外解锁的工具名（须在 chat_bi._TOOL_BY_NAME 里有 schema）。
    extra_tool_names: tuple[str, ...] = ()
    # 该技能倾向产出的渲染块类型（S1 仅作声明/文档，前端按实际块渲染）。
    block_types: tuple[str, ...] = field(default_factory=tuple)
    # 激活时把当前生效治理规约的约束卡（compile_prompt_card）并入 overlay——让该技能的
    # 提案「事前遵循」规约（命名/类型/凭据底线 + 提案终会过治理闸门）。见 chat_bi 的 governance_card。
    attach_governance: bool = False


SKILLS: dict[str, Skill] = {
    "overview": Skill(
        name="overview",
        display="域概览",
        when_to_use="用户想了解这个域有哪些对象/指标/业务板块、能查些什么这类结构性问题",
        prompt_overlay=(
            "【域概览模式】\n"
            "当前任务：回答数据域的结构、组成和能力问题。\n\n"

            "工作流程：\n"
            "1. 优先使用系统已加载的**域语义卡**（在 system 消息中），包含业务板块、核心对象和已发布口径\n"
            "2. 需要补充细节时：\n"
            "   - 对象详情 → search_objects(关键词) → get_object(id)\n"
            "   - 关系详情 → search_relations(关键词)\n"
            "   - 口径详情 → search_logics(关键词) → get_logic(id)\n"
            "   - 完整概览 → get_domain_overview()\n"
            "3. 用**显示名**和**分组列表**表达，方便用户理解\n\n"

            "回答原则：\n"
            "- 基于语义卡的内容是完整的，可直接引用\n"
            "- search_* 返回的是样本，说「包含以下对象（部分）」而非「包含以下对象」\n"
            "- 突出核心业务板块和高频对象，不必穷举\n"
        ),
        block_types=("markdown", "mapping"),
    ),
    "query": Skill(
        name="query",
        display="取数分析",
        when_to_use="用户要具体数据、数量、明细、趋势或对比这类需要实际取数的问题",
        prompt_overlay=(
            "【取数分析模式】\n"
            "当前任务：查询实际数据并分析。\n\n"

            "标准工作流程：\n"
            "1. **定位对象**\n"
            "   - 如系统已深加载相关对象（见 system 消息的【已深加载的相关本体】），可直接使用其字段信息\n"
            "   - 否则：search_objects(关键词) → get_object(id) 查看字段\n"
            "2. **处理跨对象查询**\n"
            "   - 需要关联多个对象 → find_join_path(对象A, 对象B) 找关联路径\n"
            "   - 已有发布的口径 → compile_metric(口径id) 获取权威 SQL\n"
            "3. **处理筛选条件**\n"
            "   - 需要精确值（如「广东省」的准确写法）→ profile_values(对象id, 字段名) 获取候选值\n"
            "4. **执行查询**\n"
            "   - run_sql(SQL语句) 查询 Doris 数仓（自动加 LIMIT）\n"
            "   - 复杂探索性分析 → scout_query(探索需求)\n"
            "   - 多步分析 → update_plan(步骤列表) 规划后逐步执行\n"
            "5. **分析和呈现**\n"
            "   - analyze_result() 提炼统计洞察、发现离群值\n"
            "   - render_chart(type, data) 可视化（type 可选 line/bar/pie/scatter）\n"
            "   - 需要保存 → propose_panel(图表) 或 propose_dashboard(多图)\n\n"

            "示例：「本月销售额多少？」\n"
            "→ search_objects('销售') → 找到「销售订单」\n"
            "→ get_object(销售订单_id) → 看到 amount、order_date 字段\n"
            "→ run_sql('SELECT SUM(amount) FROM sales_order WHERE MONTH(order_date) = MONTH(NOW())')\n"
            "→ analyze_result() → 提炼「本月销售额 XXX 万元，环比增长...」\n\n"

            "注意事项：\n"
            "- 不要编造对象名或字段名，一定先 search 确认存在\n"
            "- SQL 使用本体标识符（name），不是显示名\n"
            "- 深加载的对象已包含字段、关系、取值样例，无需重复 get_object\n"
        ),
        extra_tool_names=(
            "update_plan", "scout_query", "analyze_result", "render_chart",
            "propose_panel", "propose_dashboard",
        ),
        block_types=(
            "markdown", "plan", "sql", "table", "insight", "chart", "mapping", "app_proposal",
        ),
    ),
    "lineage": Skill(
        name="lineage",
        display="血缘影响",
        when_to_use="用户问某对象/表的血缘、上下游、从哪来、被谁引用、改动影响面这类问题",
        prompt_overlay=(
            "【血缘影响模式】\n"
            "当前任务：分析对象的上下游依赖和影响范围。\n\n"

            "工作流程：\n"
            "1. search_objects(关键词) 定位目标对象\n"
            "2. get_lineage(对象id) 获取血缘邻域（上游/下游/关系）\n"
            "   list_datasets(q=对象名) 看它在数仓里落成了哪张表（ODS/DWD/…）\n"
            "3. 解读结果：\n"
            "   - upstream：数据来源（从哪来）\n"
            "   - downstream：数据去向（被谁用）\n"
            "   - relations：业务关系（与谁关联）\n"
            "4. 使用**显示名**说明，解释业务含义\n\n"

            "回答要点：\n"
            "- 按方向分类：上游数据源 / 下游消费方 / 业务关联对象\n"
            "- 说明影响：「修改 X 会影响下游的 Y、Z」\n"
            "- 如果血缘为空，说明是孤立对象或数据源头\n"
        ),
        extra_tool_names=("get_lineage", "list_datasets"),
        block_types=("markdown", "lineage"),
    ),
    "create": Skill(
        name="create",
        display="建数（口径提案）",
        when_to_use="用户想新建一个指标/口径/标签/规则定义（如『建个复购率指标』『加个高价值客户标签』）",
        prompt_overlay=(
            "【建数模式】\n"
            "当前任务：为用户起草业务口径或指标定义。\n\n"

            "工作流程：\n"
            "1. **查重**：search_logics(关键词) 检查是否已有类似定义\n"
            "2. **明确需求**：\n"
            "   - 如需求包含明确对象、字段、算法 → propose_expression(结构化定义)\n"
            "   - 如需求只有名称和业务含义 → propose_draft(名称, 含义)\n"
            "   - 需求不明确 → ask_clarification(缺少的信息)\n"
            "3. **核对技术名**：\n"
            "   - search_objects(关键词) → get_object(id) 确认字段技术名\n"
            "   - 使用本体标识符（name），不是显示名\n"
            "4. **校验规范**：lint_against_standard(提案) 检查是否符合治理规约\n\n"

            "示例：「建个复购率指标」\n"
            "→ search_logics('复购') 查重 → 无类似定义\n"
            "→ ask_clarification('复购率的计算口径是？如：复购客户数/总客户数，还是复购订单数/总订单数？')\n"
            "→ [用户回答后] propose_expression(name='repurchase_rate', formula='...', based_on=[客户对象])\n\n"

            "注意：提案只是草稿，需要后续六环确认才能发布。\n"
        ),
        extra_tool_names=("propose_draft", "propose_expression", "lint_against_standard"),
        block_types=("markdown", "draft_proposal"),
    ),
    "task": Skill(
        name="task",
        display="建/管数据任务",
        when_to_use="用户要把本体/数据物化落库、建数据同步/加工任务，或问某个数据任务跑到哪了、成没成功",
        prompt_overlay=(
            "【数据任务模式】\n"
            "当前任务：创建或管理数据任务（物化/同步/加工/指标）。\n\n"

            "核心原则：**所有数据任务都按六环分别确认**\n"
            "需求 → 本体 → 数据 → 执行方案 → 执行 → 结果\n"
            "前三环在表单向导里确认，后三环在任务详情里确认。任何一环都不能替用户跳过。\n\n"

            "单任务标准流程：\n"
            "1. get_task_options(kind) 查看该类任务的配置选项和候选值\n"
            "2. request_form(title='任务名', task_kind=kind, intent='用户需求描述') 生成六环确认表单\n"
            "   - fields 参数留空，由服务端生成六环向导\n"
            "3. [等待用户填表] 用户在界面上逐环确认\n"
            "4. propose_action(kind, context={...}) 根据回填内容生成任务提案\n"
            "   - context 必须包含 task_confirmation_id（从表单回填中获取）\n\n"

            "任务类型说明：\n"
            "- **materialize（物化）**：把本体对象建成真实数据库表\n"
            "  候选：物化范围、目标引擎（默认 Doris）、真实数据库\n"
            "- **sync（同步）**：从业务源库同步数据到 ODS 层\n"
            "  候选：本体对象、匹配的业务源、装载模式（全量/增量/CDC）\n"
            "  落点：恒为 ODS 库的 ods_{域}_{表名}，不给用户选\n"
            "  **注意**：sync 会自动建表（CREATE TABLE IF NOT EXISTS），不需要先物化\n"
            "- **transform（加工）**：从 ODS 转换到分层（DWD/DWS/ADS）\n"
            "  候选：ODS ready 对象、清洗规则、目标分层\n"
            "  **上游表用 list_datasets(layer=\'ods\') 查**：搬完的才能加工，表名不要自己拼\n"
            "- **metric（指标）**：基于形式化业务口径生成定时计算任务\n"
            "  候选：已发布的业务逻辑（口径）\n\n"

            "任务链流程：\n"
            "- 多步需求（如「把 ERP 订单同步过来并加工到 DWD」）→ propose_pipeline(steps=[...])\n"
            "- 链上每一步同样逐环确认，只是上游落点自动接成下游默认值\n\n"

            "查询任务状态：\n"
            "- get_task_status(artifact_id) 或 get_task_status() 查询本会话相关任务\n\n"

            "重要约束：\n"
            "- 提案只创建草稿，不会立即执行\n"
            "- 不要说「任务已创建」或「已执行」，应说「已生成任务提案，请在界面逐环确认」\n"
            "- 同步任务不需要先物化，一个 sync 任务就够\n"
        ),
        extra_tool_names=(
            "get_task_options", "propose_action", "propose_pipeline", "get_task_status",
            "lint_against_standard", "list_datasets",
        ),
        block_types=("markdown", "action_proposal", "pipeline_proposal", "task_status"),
    ),
    "onboard": Skill(
        name="onboard",
        display="接数据",
        when_to_use=(
            "用户要把一个新的库/系统接进来、问怎么连数据源、或要为某个数据域生成本体草稿"
            "（如『把我们的 ERP 库接进来』『给销售域生成一版本体』）"
        ),
        prompt_overlay=(
            "【接数据模式】\n"
            "当前任务：接入新数据源或生成本体草稿。\n\n"

            "工作流程：\n"
            "1. list_onboarding_targets() 查看现有数据源和数据域\n"
            "2. 根据需求类型：\n"
            "   - **登记新连接** → propose_datasource(name, kind, description)\n"
            "     · kind 可选：mysql/postgresql/doris/starrocks/hive/datahub\n"
            "     · 连接信息（host/port/database/user/password）由确认界面填写\n"
            "   - **生成本体草稿** → propose_ontology_draft(domain_id, source_id)\n"
            "     · 使用 list_onboarding_targets 返回的 domain_id\n"
            "     · 会从 DataHub 或数据源抽取元数据生成本体\n\n"

            "说明：\n"
            "- DataHub 元数据采集在 DataHub 中配置（ingestion recipe），不在这里做\n"
            "- 接入后的同步和物化使用「任务」技能（select_skill('task')）\n"
            "- 提案需要用户在界面确认连接信息和生成范围\n"
        ),
        extra_tool_names=(
            "list_onboarding_targets", "propose_datasource", "propose_ontology_draft",
        ),
        block_types=("markdown", "onboard_proposal"),
    ),
    "ops": Skill(
        name="ops",
        display="运行记录",
        when_to_use="用户问某个任务/对象的运行状态、物理落点、执行记录、或其它已经发生过的事实",
        prompt_overlay=(
            "【运行记录模式】\n"
            "当前任务：查询已经发生过什么——任务跑到哪了、对象落在哪张表、什么状态。\n\n"

            "核心原则：**只读现场勘查模式**\n"
            "- 先定位主体（对象/任务/域）→ search_objects / get_object\n"
            "- 再取运行记录 → get_landing / get_ops_record / get_task_status / get_lineage\n"
            "- 答案**必须带 as_of（记录时点）与 source（权威层）**\n"
            "- **不出任何提案**：这是纯只读模式，要建任务请 select_skill('task')\n\n"

            "两类问题：\n"
            "1. **物理落点**（get_landing）：\n"
            "   - 这个对象/口径落到哪张物理表了？\n"
            "   - 表建了吗？数搬了吗？现在能不能查？\n"
            "   - 工作流：search_objects → get_landing(object_id)\n"
            "   - 返回 ODS 表、服务层表、装载模式、最近成功搬数时间\n"
            "   - **重要**：没有落点登记 ≠ 错误 — 正是这条信息挡住了「推测表名」\n\n"

            "2. **任务执行**（get_task_status）：\n"
            "   - 那个任务跑完了吗？卡在哪一步？失败原因是什么？\n"
            "   - 工作流：get_task_status(artifact_id) 或 get_task_status() 列最近任务\n"
            "   - 返回状态、执行时间、失败原因、物化/同步制品的回执摘要\n\n"

            "3. **血缘影响**（get_lineage）：\n"
            "   - 这个对象的上游是谁、下游影响谁？\n"
            "   - 工作流：search_objects → get_lineage(center_id, depth)\n\n"

            "回答原则：\n"
            "- 基于工具真实返回的记录；没查到就说没查到，不许推测\n"
            "- 必须说清「截至 XX 时间」和「数据来源：XX」\n"
            "- 落点状态有 7 种：未落地/已登记/表已建/搬运中/已落地/陈旧/失败\n"
            "- 没有落点登记是个真答案，说明这个对象在数仓里还没有对应的物理表\n"
        ),
        extra_tool_names=(
            "get_landing", "get_ops_record", "get_task_status", "get_lineage",
        ),
        block_types=("markdown", "record", "task_status", "lineage"),
    ),
}


def skill_choices_text() -> str:
    """拼给 select_skill 工具描述的候选清单。"""
    return "；".join(f"{s.name}（{s.when_to_use}）" for s in SKILLS.values())
