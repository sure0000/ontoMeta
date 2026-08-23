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
            "【域概览】使用域语义卡回答业务板块、核心对象和已发布口径。"
            "需要补充时调用 search_* 或 get_domain_overview。用显示名和分组列表表达。"
        ),
        block_types=("markdown", "mapping"),
    ),
    "query": Skill(
        name="query",
        display="取数分析",
        when_to_use="用户要具体数据、数量、明细、趋势或对比这类需要实际取数的问题",
        prompt_overlay=(
            "【取数分析】先定位对象或业务口径。已有口径使用 compile_metric；跨对象使用 find_join_path；"
            "筛选值使用 profile_values。然后用 run_sql 查询默认 Doris。"
            "多步分析可用 update_plan，复杂探查可用 scout_query。"
            "结果分析使用 analyze_result，可视化使用 render_chart。"
            "需要保存结果时使用 propose_panel 或 propose_dashboard。"
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
            "【血缘影响】先用 search_objects 定位对象，再用 get_lineage 获取邻域。"
            "按边方向说明上游、下游和业务关系，使用显示名。"
        ),
        extra_tool_names=("get_lineage",),
        block_types=("markdown", "lineage"),
    ),
    "create": Skill(
        name="create",
        display="建数（口径提案）",
        when_to_use="用户想新建一个指标/口径/标签/规则定义（如『建个复购率指标』『加个高价值客户标签』）",
        prompt_overlay=(
            "【业务口径】先用 search_logics 查找相近定义。"
            "需求包含明确对象、字段和算法时，使用 search_objects/get_object 核对技术名，再调用 "
            "propose_expression。需求只有名称和业务含义时，调用 propose_draft。"
            "需要补充阈值或算法时使用 ask_clarification。"
        ),
        extra_tool_names=("propose_draft", "propose_expression", "lint_against_standard"),
        block_types=("markdown", "draft_proposal"),
    ),
    "task": Skill(
        name="task",
        display="建/管数据任务",
        when_to_use="用户要把本体/数据物化落库、建数据同步/加工任务，或问某个数据任务跑到哪了、成没成功",
        prompt_overlay=(
            "【数据任务】支持 materialize、sync、transform、metric 和任务状态查询。\n"
            "单任务流程：get_task_options(kind) → request_form(title, task_kind=kind, intent=需求) → "
            "收到表单回填后 propose_action。request_form 的 fields 留空，由服务端生成三步确认向导。"
            "propose_action.context 要包含回填的 task_confirmation_id。\n"
            "候选含义：materialize 选择物化范围、默认 Doris 和真实数据库；sync 选择本体、匹配的业务源、"
            "ODS 库和装载模式；transform 选择 ODS ready 对象、规则和分层；metric 选择形式化业务口径。\n"
            "提案仅创建任务草稿。后续由界面完成校验与 dry-run、方案确认、执行和结果确认。"
            "多步需求使用 propose_pipeline；任务进度使用 get_task_status。"
        ),
        extra_tool_names=(
            "get_task_options", "propose_action", "propose_pipeline", "get_task_status",
            "lint_against_standard",
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
            "【接数据】先用 list_onboarding_targets 查看现有数据源和数据域。"
            "登记连接使用 propose_datasource；连接信息由确认界面填写。"
            "生成本体草稿使用 propose_ontology_draft，并使用目录返回的 domain_id。"
            "DataHub 元数据采集在 DataHub 中配置；同步和物化使用数据任务技能。"
        ),
        extra_tool_names=(
            "list_onboarding_targets", "propose_datasource", "propose_ontology_draft",
        ),
        block_types=("markdown", "onboard_proposal"),
    ),
}


def skill_choices_text() -> str:
    """拼给 select_skill 工具描述的候选清单。"""
    return "；".join(f"{s.name}（{s.when_to_use}）" for s in SKILLS.values())
