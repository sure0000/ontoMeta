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
            "【域概览技能】用户想了解这个域有什么。直接依据常驻的域语义卡作答其业务板块、"
            "核心对象与已发布指标；细节不足时用 search_* 或 get_domain_overview 补齐。"
            "这类问题通常**无需写 SQL、无需 run_sql**。用显示名，给出有结构的概览而非罗列全部。"
        ),
        block_types=("markdown", "mapping"),
    ),
    "query": Skill(
        name="query",
        display="取数分析",
        when_to_use="用户要具体数据、数量、明细、趋势或对比这类需要实际取数的问题",
        prompt_overlay=(
            "【取数分析技能】用户要具体数据/趋势/对比。流程：定位实体 → 有现成口径就 "
            "compile_metric、跨对象先 find_join_path、写死值前先 profile_values → 用 run_sql "
            "提交只读 SQL 取数。\n"
            "若是开放式/多步分析（如「探索销售数据有没有异常」），**先用 update_plan 列 2-5 步计划**"
            "让用户看到你的拆解思路，再逐步执行；单值或单步取数不必规划。\n"
            "拿到结果后：找异常/看分布/离群用 **analyze_result** 对结果做统计画像（均值/分位/IQR 离群），"
            "别口头臆测；若适合可视化就调 **render_chart** 出图：时间/日期维度用 line 或 area，"
            "类别对比用 bar；x/y 必须照抄结果表里的真实列名。单值或纯明细不必作图。"
        ),
        extra_tool_names=("update_plan", "analyze_result", "render_chart"),
        block_types=("markdown", "plan", "sql", "table", "insight", "chart", "mapping"),
    ),
    "lineage": Skill(
        name="lineage",
        display="血缘影响",
        when_to_use="用户问某对象/表的血缘、上下游、从哪来、被谁引用、改动影响面这类问题",
        prompt_overlay=(
            "【血缘影响技能】用户想看某对象的上下游/血缘/影响面。先用 search_objects 定位对象、"
            "拿到它的 id，再调 **get_lineage(center_id, depth)** 取邻域子图（默认 depth=1，本体大时"
            "别铺全图，按中心邻域下钻）。\n"
            "解读：`structure_type=derivation` 的边是**数据加工血缘**（上游加工到下游），其它边是"
            "业务关系（外键/引用等）。用显示名说明上游从哪来、下游影响谁；边方向即数据流向。"
        ),
        extra_tool_names=("get_lineage",),
        block_types=("markdown", "lineage"),
    ),
    "create": Skill(
        name="create",
        display="建数（口径提案）",
        when_to_use="用户想新建一个指标/口径/标签/规则定义（如『建个复购率指标』『加个高价值客户标签』）",
        prompt_overlay=(
            "【建数技能】用户想新建口径定义（指标 metric / 标签 tag / 规则 rule）。\n"
            "先用 search_logics 查重——本体里已有同义口径就别重复建，直接指给用户。\n"
            "确无重复时用 **propose_draft** 产出一份**提案**（中文名 + 类型 + 口径自然语言说明）。\n"
            "**不要替用户编具体表达式**——口径是人定的；提案只给名字与说明，交由用户确认后创建为草稿、"
            "再自行补全表达式并走发布。你不直接改动本体或数据，只出提案。\n"
            "提案终会过下方治理规约的校验；若提案涉及具体物理表名，先用 **lint_against_standard** "
            "自检命名是否合规约、按返回的 fix 修正后再提。"
        ),
        extra_tool_names=("propose_draft", "lint_against_standard"),
        block_types=("markdown", "draft_proposal"),
        attach_governance=True,
    ),
    "task": Skill(
        name="task",
        display="建/管数据任务",
        when_to_use="用户要把本体/数据物化落库、建数据同步/加工任务，或问某个数据任务跑到哪了、成没成功",
        prompt_overlay=(
            "【建/管数据任务技能】用户想建数据任务（物化 materialize / 同步 sync / 加工 transform）"
            "或查任务进度。\n"
            "建任务分三步，**顺序不能颠倒**：\n"
            "1. **先调 get_task_options(kind)** 拿到可选项（物化：目标数据源/目标库/待物化实体及其"
            "契约 id、分层、分区键、装载方式、现有调度/装载方式/调度频率预置；同步与加工：候选对象）。"
            "不先问就不知道有哪些数据源，只能编——而编出来的 id 一定过不了校验。\n"
            "2. 缺的参数用 **request_form(title, task_kind=该类型)** 一次收齐——带上 task_kind，"
            "服务端会按该类型补齐必问字段（物化=目标数据源/目标库/表名/装载方式/分区键/调度频率）"
            "并填入真实候选，你**不用也不该**自己列 fields；确有额外要问的再追加。\n"
            "   用户填回的选项形如「名称｜id」：取 ｜ 右边那段作为 context 里的 id/取值。\n"
            "3. 参数齐了再用 **propose_action(kind, intent, context)** 产出**提案**（**不执行、不写库**）；"
            "真正的创建/校验/dry-run/确认/执行由用户在前端点击后走既有治理流程，你不直接建表或跑作业。"
            "若提案涉及具体物理表名，先用 **lint_against_standard** 自检命名是否合规约、按 fix 修正后再提。\n"
            "查进度：用 **get_task_status(artifact_id 或 kind)** 回读任务状态与执行回执，据实回答"
            "「跑完没/成没成功」；不要臆造状态。"
        ),
        extra_tool_names=(
            "get_task_options", "propose_action", "get_task_status", "lint_against_standard",
        ),
        block_types=("markdown", "action_proposal", "task_status"),
        attach_governance=True,
    ),
}


def skill_choices_text() -> str:
    """拼给 select_skill 工具描述的候选清单。"""
    return "；".join(f"{s.name}（{s.when_to_use}）" for s in SKILLS.values())
