"""Skill 正文的工具化出口。

MCP 已经通过 ``list_prompts`` / ``get_prompt`` 暴露了这几份 skill，但**prompts 不是
每个客户端都消费**：dsh 的 MCP 客户端文档写得很直白——"Only tools are bridged:
MCP resources and prompts are not supported"。实测下来，dsh 上的 skill 之所以生效，
靠的是把 ``backend/app/mcp/skills`` 手工挂进 ``skill-filesystem.customSkillDirs``；
按前端给的"远程 HTTP + Bearer"接进来的客户端，拿到的是 29 个工具和 0 份指引。

而这几份 skill 恰恰是行为契约本身——什么时候用哪个工具、``found=0`` 该怎么说、
哪些话不能说。少一份指引，模型就在那一块自由发挥。

所以把 skill 正文也做成一个**工具**：tools 是唯一保证被桥接的 MCP 能力。
``list_prompts`` 保留不动，支持 prompts 的客户端照旧走那条路，两条路读的是同一份
``app.mcp.skills`` 生效正文（含数据库覆写），不会分叉。
"""

from __future__ import annotations

from typing import Any

from app.mcp.skills import OUTPUT_CONTRACT, get_skill, list_skills

from . import AuthContext, ToolResult, register_tool
from ._common import session


def _index(skills: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "topic": skill.name,
            "description": str(skill.frontmatter.get("description") or ""),
            "when_to_use": str(skill.frontmatter.get("whenToUse") or ""),
            "output_contract_version": OUTPUT_CONTRACT["version"],
        }
        for skill in skills
    ]


@register_tool
class GetPlaybookTool:
    """按主题取回 ontoMeta 的操作指引正文。"""

    name = "get_playbook"
    required_role = "reader"
    description = (
        "取回 ontoMeta 的操作指引（playbook）正文：某类问题该按什么顺序调哪些工具、"
        "每个结果字段怎么解读、哪些结论不许说。\n"
        "正文包含强制的最终答复格式（固定标题、状态枚举、表格行数、ID 与空结果规则），"
        "不是可选写作建议。\n"
        "**第一次做某类事情之前先调它**——工具描述只讲单个工具做什么，"
        "跨工具的顺序、闸门和输出契约只在 playbook 里。\n"
        "不带 topic 调用返回主题清单与各自适用场景；带 topic 返回该主题的完整正文。\n"
        "如果你的客户端已经以 skill / prompt 形式加载过同名指引，就不必再调——"
        "两边是同一份正文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "指引主题；留空则返回可用主题清单。"
                    "ontometa-mcp=总入口与共同底线，ontometa-output=出口契约（回答格式与"
                    "「需要用户选择时怎么问」的总控），ontometa-flow=交互式建数流程，"
                    "ontometa-discovery=本体探索，ontometa-query=取数与算指标，"
                    "ontometa-task-plan=任务规划，ontometa-task-execute=任务执行与运行追溯，"
                    "ontometa-admin=服务自省与审计"
                ),
            },
        },
        "required": [],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        topic = str(arguments.get("topic") or "").strip()
        try:
            with session() as db:
                if not topic:
                    enabled = [item for item in list_skills(db) if item.enabled]
                    return ToolResult(
                        success=True,
                        data={
                            "topics": _index(enabled),
                            "note": "带 topic 再调一次取回正文；正文是本服务的行为契约，不是可选建议。",
                        },
                        metadata={"count": len(enabled)},
                    )

                skill = get_skill(db, topic)
                if skill is None or not skill.enabled:
                    available = [item.name for item in list_skills(db) if item.enabled]
                    return ToolResult(
                        success=False,
                        error=f"未知或已停用的指引主题：{topic}",
                        data={"available_topics": available},
                    )
                return ToolResult(
                    success=True,
                    data={
                        "topic": skill.name,
                        "body": skill.body,
                        # Keep the contract machine-readable for clients that
                        # can validate structured responses in addition to
                        # following the Markdown instructions in ``body``.
                        "output_contract": OUTPUT_CONTRACT,
                    },
                    metadata={
                        # override=true 说明这份被本地改写过，与仓库内置版不同——
                        # 排查"模型为什么不按文档走"时，先看这一位。
                        "source": skill.source,
                        "override": skill.override,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取指引失败：{exc}")
