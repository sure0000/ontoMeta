"""检索子 agent（P4.2）：把「找的过程」关进隔离上下文，只把「找到的结论」带回来。

**为什么存在**：大本体上，定位相关实体这件事本身要烧掉大量上下文——
`test_large_ontology_scale` 实测：一次典型检索序列（宽泛搜 → 取详情 → 再搜相邻板块）
往主上下文塞进 **~8000 字符 / 4 次工具调用**，而其中绝大部分是**找的过程**，
不是找到的结论。这些垃圾会一直躺在主上下文里，被后续每一轮反复 prefill。

子 agent 用自己的消息列表跑同一套检索工具，主 agent 只收到
``{objects: [...], logics: [...], reason: "..."}``——**一两百字符**。

**范围严格受限**：子 agent 只拿检索类工具，拿不到 `run_sql` / `compile_metric` /
`profile_values`。它的职责只有「定位」，不做取数、不做口径展开——
职责一旦放宽，隔离上下文就会重新变成一个什么都往里塞的主上下文。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.services.agent_subagent import SubAgentSpec, run_subagent

logger = logging.getLogger("ontometa.retrieval_agent")

# 子 agent 可用的工具：**只有检索**。加工具前先想清楚它是不是「定位」职责的一部分。
RETRIEVAL_TOOLS = ("search_objects", "search_relations", "search_logics", "get_object")

# 子循环预算：定位是个收敛很快的任务，给多了只会让它闲逛
MAX_STEPS = 4
# 子 agent 内部单个工具结果的字符预算（比主上下文松——反正不出这个房间）
TOOL_RESULT_MAX_CHARS = 12000

_SYSTEM_PROMPT = (
    "你是本体检索助手。任务：在已发布本体中**定位**与用户问题相关的业务对象与口径，"
    "然后仅回报结论。\n"
    "纪律：\n"
    "1. 用检索工具查找；关键词不中就换词再试（中英文、同义词、上位词）。\n"
    "2. **只定位，不解读**：不要分析数据、不要写 SQL、不要展开口径。\n"
    "3. 找完后**必须**只输出一个 JSON，不要任何其它文字：\n"
    '   {"objects": ["对象标识符", ...], "logics": ["口径标识符", ...], '
    '"reason": "一句话说明为何相关"}\n'
    "   标识符必须是工具真实返回过的 name 字段；宁可少给，不可编造。\n"
    "   确实找不到就回 {\"objects\": [], \"logics\": [], \"reason\": \"未找到相关实体\"}。"
)

# V4 O4：把检索子 agent 的「系统提示 + 工具子集 + 预算」声明成一份 spec，
# 循环骨架复用 agent_subagent.run_subagent（不再自己维一套）。
RETRIEVAL_SPEC = SubAgentSpec(
    name="检索",
    system_prompt=_SYSTEM_PROMPT,
    allowed_tools=RETRIEVAL_TOOLS,
    max_steps=MAX_STEPS,
    tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
    final_nudge="请立即只输出结论 JSON。",
)


@dataclass
class RetrievalResult:
    """子 agent 的**全部**产出——这就是允许进入主上下文的东西。"""

    objects: list[str] = field(default_factory=list)
    logics: list[str] = field(default_factory=list)
    reason: str = ""
    steps: int = 0
    llm_calls: int = 0
    # 子上下文里实际流动过的字符数。它**不进主上下文**，只用于度量隔离效果。
    isolated_chars: int = 0

    def to_dict(self) -> dict:
        return {
            "objects": self.objects,
            "logics": self.logics,
            "reason": self.reason,
            "note": (
                "以上是检索子 agent 的定位结论；检索过程未进入本对话上下文。"
                "需要字段/口径细节请对这些标识符调 get_object / get_logic。"
            ),
        }


async def locate_entities(
    db: Session,
    *,
    client,
    model: str,
    intent: str,
    domain_id: str,
    ontology_id: str,
    dispatch: Callable[..., tuple[Any, str, bool]],
    tool_schemas: list[dict],
    to_thread,
) -> RetrievalResult:
    """在隔离上下文里跑检索循环，返回紧凑结论。

    ``dispatch`` / ``tool_schemas`` 由调用方注入（就是主 agent 那套），
    保证子 agent 与主 agent 的工具行为**完全一致**——不另起一套实现，
    否则两边的检索语义迟早分叉。

    V4 O4：循环骨架已抽到 ``agent_subagent.run_subagent``，本函数只负责
    提供检索 spec + 把最终文本按检索结论 schema 解析回 RetrievalResult。
    """
    run = await run_subagent(
        db,
        client=client,
        model=model,
        spec=RETRIEVAL_SPEC,
        user_prompt=f"请定位与以下问题相关的实体：\n{intent}",
        domain_id=domain_id,
        ontology_id=ontology_id,
        dispatch=dispatch,
        tool_schemas=tool_schemas,
        to_thread=to_thread,
    )
    result = RetrievalResult(
        steps=run.steps, llm_calls=run.llm_calls, isolated_chars=run.isolated_chars
    )
    _parse_conclusion(run.final_text, result)
    return result


def _parse_conclusion(content: str, result: RetrievalResult) -> None:
    """从收尾文本里取结论 JSON。取不到就返回空结论——不猜、不把正文当结论。"""
    text = (content or "").strip()
    if not text:
        result.reason = "检索未得出结论"
        return
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        result.reason = "检索未得出结构化结论"
        return
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        result.reason = "检索结论解析失败"
        return
    if not isinstance(data, dict):
        result.reason = "检索结论格式不合法"
        return
    result.objects = [str(x) for x in (data.get("objects") or []) if str(x).strip()]
    result.logics = [str(x) for x in (data.get("logics") or []) if str(x).strip()]
    result.reason = str(data.get("reason") or "")


__all__ = ["RetrievalResult", "locate_entities", "RETRIEVAL_TOOLS", "MAX_STEPS"]
