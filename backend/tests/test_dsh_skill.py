"""dsh 的 ontoMeta skill 集：保持可发现、完整、且不与工具注册表脱节。

skill 是外部 agent 唯一的行为契约——MCP 只给工具，**怎么用、什么算答完、什么不能说**
全在这几份 SKILL.md 里。所以这里钉三件事：

1. **工具覆盖**：注册表里每个工具都得在某个 skill 里被提到。新加一个工具却不写指引，
   等于把它交给模型自由发挥——`propose_*` 当年就栽在这上面。
2. **每份 skill 的关键指引在自己身上**，不是靠总入口兜底：总入口是
   ``disable-model-invocation: true``，模型自动路由时根本不会加载它。
3. **会安静出错的那几条**（口径权威、连接键/字面量要查、运行事实只从记录读）必须在场。
   这些错了不报错，只会给出一个看起来合理的错答案。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.mcp.tools import TOOL_REGISTRY
from app.mcp.skills import OUTPUT_CONTRACT_HEADING, builtin_composed

SKILL_ROOT = Path(__file__).parents[1] / "app/mcp/skills"

ROUTER = "ontometa-mcp"
#: 出口契约总控：它不是"某一类问题的指引"，而是所有回答共同的格式与提问规矩。
CONTRACT = "ontometa-output"
SPECIALIZED = (
    "ontometa-flow",
    "ontometa-discovery",
    "ontometa-query",
    "ontometa-task-plan",
    "ontometa-task-execute",
    "ontometa-admin",
)
ALL_SKILLS = (ROUTER, CONTRACT, *SPECIALIZED)

# 每份 skill 自己必须带的指引。放这里而不是「在所有 skill 的并集里找一遍」——
# 并集能过，说明标记只是存在于某处，不代表用得上它的那份 skill 里有。
REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "ontometa-flow": (
        "start_task_flow", "advance_task_flow",
        "answers",           # 无服务端状态：答案必须累计带回
        "__confirm_",        # 六环逐环确认
        "blocked",           # 缺前置条件是事实，不是可以绕过的提示
        "search",            # 候选几百条时怎么收窄
    ),
    "ontometa-discovery": (
        "query_ontology", "get_ontology_overview", "query_objects", "query_object_detail",
        "query_relations", "search_logics", "get_logic", "get_lineage", "get_landing",
        "list_datasources",
        "formalized",        # 只有文字口径的那条编译不出 SQL
        "is_derivation",     # 外键不是「数据从这里来」
        "not_landed",        # 没登记就是没落地，不许拼表名
        "truncated",
    ),
    "ontometa-query": (
        "search_logics", "compile_metric", "execute_sql", "validate_sql",
        "find_join_path", "profile_values",
        "caliber_trace",     # 口径证据
        "sql_hint", "fanout_risk", "safe_aggs",
        "sample_note",
    ),
    "ontometa-task-plan": (
        "propose_sync", "propose_transform", "propose_materialize", "propose_metric",
        "draft_task", "validate_task", "draft_payload",
        "target_datasource_id",   # 四类任务都必须给，缺了就白建
        "blocking_count",
    ),
    "ontometa-task-execute": (
        "get_task_status", "wait_task_status", "confirm_task", "execute_task", "list_tasks", "get_ops_record",
        "run_url",
        "observed_at",       # 读取时刻 ≠ 记录自身的权威时点
        "failed_without_reason",
        "ask_user_question",
        "host_confirmation",
        "interactive_approval.digest",
    ),
    "ontometa-admin": (
        "server_info", "list_audit_logs", "get_mcp_stats",
        "denied", "rate_limited",
    ),
}


def _read(name: str) -> tuple[dict, str]:
    """frontmatter + **下发正文**。

    正文取合成后的那一份（``{{OUTPUT_CONTRACT}}`` 已替换成总控的契约）——dsh 装到
    skills 目录里、``get_playbook`` 回传的都是它；拿仓库原文去断言，等于在检查一份
    没有任何 Agent 会读到的文本。
    """
    raw = (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"{name}: 缺少 frontmatter"
    _, frontmatter, _body = raw.split("---", 2)
    composed = builtin_composed(name)
    return yaml.safe_load(frontmatter), composed.split("---", 2)[2]


def test_all_skills_present_and_well_formed():
    found = {p.parent.name for p in SKILL_ROOT.glob("*/SKILL.md")}
    assert found >= set(ALL_SKILLS), f"缺少 skill：{set(ALL_SKILLS) - found}"

    for name in ALL_SKILLS:
        metadata, body = _read(name)
        assert metadata["name"] == name
        assert metadata["user-invocable"] is True
        # 总入口只作显式 /ontometa-mcp 使用；专用 skill 才允许模型自动路由
        assert metadata["disable-model-invocation"] is (name == ROUTER)
        assert "ontoMeta" in metadata["description"]
        assert metadata.get("whenToUse"), f"{name}: 缺 whenToUse，模型无从判断该不该选它"
        assert "结论" in body, f"{name}: 没有声明输出契约"
        contract = body.split(OUTPUT_CONTRACT_HEADING, 1)[1]
        for marker in ("## 结论", "## 结果", "## 依据", "## 限制", "## 下一步", "状态", "最多 10 行"):
            assert marker in contract, f"{name}: 输出契约缺少 {marker}"


@pytest.mark.parametrize("name", SPECIALIZED)
def test_specialized_skill_is_self_sufficient(name):
    """专用 skill 会被模型直接自动路由到，总入口不会一起加载。

    所以安全底线（不读 .env、不绕 REST、凭据不入回答）必须每份自带；只写在总入口里
    等于没写。
    """
    _metadata, body = _read(name)
    assert "## 通用底线" in body
    assert ".env" in body and "REST" in body
    assert "凭据" in body


@pytest.mark.parametrize("name,markers", sorted(REQUIRED_MARKERS.items()))
def test_skill_carries_its_own_guidance(name, markers):
    _metadata, body = _read(name)
    missing = [m for m in markers if m not in body]
    assert not missing, f"{name} 缺少关键指引：{missing}"


def test_router_points_at_every_specialized_skill():
    _metadata, body = _read(ROUTER)
    missing = [name for name in (*SPECIALIZED, CONTRACT) if name not in body]
    assert not missing, f"总入口没有路由到：{missing}"


def test_every_skill_inherits_the_single_output_contract():
    """契约只有一份：仓库原文里除了总控都不许自带契约，下发正文里又必须人人都有。"""
    for name in ALL_SKILLS:
        raw = (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        if name == CONTRACT:
            assert OUTPUT_CONTRACT_HEADING in raw
            continue
        assert "{{OUTPUT_CONTRACT}}" in raw, f"{name}: 没有引用出口契约总控"
        assert OUTPUT_CONTRACT_HEADING not in raw, f"{name}: 自己抄了一份契约，不会跟随总控"
        composed = builtin_composed(name)
        assert composed.count(OUTPUT_CONTRACT_HEADING) == 1
        assert "{{OUTPUT_CONTRACT}}" not in composed


def test_every_registered_tool_has_skill_guidance():
    """注册表里每个工具都要在某份 skill 里被提到。

    加了工具却不写指引，等于把「什么时候用、结果怎么解读、什么不能说」留给模型自由发挥。
    这条失败时的修法是去补 skill，不是把工具从这里豁免掉。
    """
    bodies = {name: _read(name)[1] for name in ALL_SKILLS}
    # 总控只讲出口，不承担工具指引——工具覆盖由其它 skill 负责。
    uncovered = {
        tool: sorted(bodies)
        for tool in sorted(TOOL_REGISTRY)
        if not any(tool in body for body in bodies.values())
    }
    assert not uncovered, (
        "这些 MCP 工具没有任何 skill 指引，请补进对应 skill："
        f"{sorted(uncovered)}"
    )


def test_silent_failure_red_lines_are_stated_somewhere():
    """三条「错了不报错」的红线必须在总入口写明，作为所有 skill 的共同前提。"""
    _metadata, body = _read(ROUTER)
    for marker in ("compile_metric", "find_join_path", "profile_values", "get_ops_record"):
        assert marker in body, f"总入口红线缺 {marker}"
