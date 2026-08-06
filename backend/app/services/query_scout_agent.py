"""取数探路子 agent（V4 O4）：把「取数前的探索」关进隔离上下文，只回候选 SQL + 要点。

**为什么存在**：跨对象、要先摸清取值分布/找 join 路径的取数问题，主 agent 得连着调
好几次 `profile_values` / `find_join_path` / `compile_metric` 来探路——每次结果都往主上下文
塞几千字符，而这些**探路过程**对最终答案没用，只有**探出来的那条 SQL**有用。

复用 `agent_subagent.run_subagent` 的隔离骨架（与检索子 agent 同一套循环），只换一份 spec：
- 工具子集 = **只读探索**（search / get_object / find_join_path / profile_values / compile_metric），
  **不含 run_sql**——探路不执行，候选 SQL 交回主 agent，由主 agent 走 run_sql（F3 证明 + RBAC）执行。
- 结论 = `{"sql": "候选只读 SQL", "brief": "口径/依据一句话", "objects": [...], "logics": [...]}`。

**守卫不变**：子 agent 产的是**候选 SQL 文本**，不落库、不执行；真正取数仍过主 agent 的
只读校验 + 语义证明 + 权限闸门。隔离只省上下文，不绕任何一道闸。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.services.agent_subagent import SubAgentSpec, run_subagent

logger = logging.getLogger("ontometa.query_scout")

# 只读探索工具：定位 + 摸分布 + 找 join + 编口径。**不含 run_sql**——探路不执行。
SCOUT_TOOLS = (
    "search_objects", "search_logics", "get_object",
    "find_join_path", "profile_values", "compile_metric",
)
MAX_STEPS = 5
TOOL_RESULT_MAX_CHARS = 12000

_SYSTEM_PROMPT = (
    "你是取数探路助手。任务：为用户的取数问题**探路**——定位实体、摸清相关字段的取值分布、"
    "跨对象时找 join 路径、有现成口径就编译，最终产出**一条候选只读 SQL**。\n"
    "纪律：\n"
    "1. 只用给你的探索工具；**不要执行 SQL**（你没有 run_sql，也不该有）。\n"
    "2. 写死过滤值前先 profile_values 看真实取值；跨对象先 find_join_path 拿声明过的关系。\n"
    "3. 探完后**必须**只输出一个 JSON，不要任何其它文字：\n"
    '   {"sql": "SELECT ...", "brief": "一句话说明口径与依据", '
    '"objects": ["对象标识符", ...], "logics": ["口径标识符", ...]}\n'
    "   SQL 必须是只读 SELECT，列/表用工具真实返回过的名字；探不出就把 sql 设为空串。"
)

SCOUT_SPEC = SubAgentSpec(
    name="取数探路",
    system_prompt=_SYSTEM_PROMPT,
    allowed_tools=SCOUT_TOOLS,
    max_steps=MAX_STEPS,
    tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
    final_nudge="请立即只输出候选 SQL 的结论 JSON。",
)


@dataclass
class ScoutResult:
    """子 agent 的**全部**产出——这就是允许进入主上下文的东西。"""

    sql: str = ""
    brief: str = ""
    objects: list[str] = field(default_factory=list)
    logics: list[str] = field(default_factory=list)
    steps: int = 0
    llm_calls: int = 0
    isolated_chars: int = 0

    def to_dict(self) -> dict:
        return {
            "candidate_sql": self.sql,
            "brief": self.brief,
            "objects": self.objects,
            "logics": self.logics,
            "note": (
                "以上是取数探路子 agent 探出的**候选**只读 SQL；探路过程未进入本对话上下文。"
                "如认可，请用 run_sql 提交它执行（会再过只读校验与语义证明）；"
                "需要调整口径/字段就在此基础上改。勿臆造未探到的列。"
            ),
        }


async def scout_query(
    db: Session,
    *,
    client,
    model: str,
    intent: str,
    domain_id: str,
    ontology_id: str,
    dispatch,
    tool_schemas: list[dict],
    to_thread,
) -> ScoutResult:
    """在隔离上下文里探路，返回候选 SQL + 要点。"""
    run = await run_subagent(
        db,
        client=client,
        model=model,
        spec=SCOUT_SPEC,
        user_prompt=f"请为以下取数问题探路并给出候选 SQL：\n{intent}",
        domain_id=domain_id,
        ontology_id=ontology_id,
        dispatch=dispatch,
        tool_schemas=tool_schemas,
        to_thread=to_thread,
    )
    result = ScoutResult(
        steps=run.steps, llm_calls=run.llm_calls, isolated_chars=run.isolated_chars
    )
    _parse_conclusion(run.final_text, result)
    return result


def _parse_conclusion(content: str, result: ScoutResult) -> None:
    """从收尾文本里取结论 JSON。取不到就返回空候选——不猜、不把正文当 SQL。"""
    text = (content or "").strip()
    if not text:
        result.brief = "探路未得出结论"
        return
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        result.brief = "探路未得出结构化结论"
        return
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        result.brief = "探路结论解析失败"
        return
    if not isinstance(data, dict):
        result.brief = "探路结论格式不合法"
        return
    result.sql = str(data.get("sql") or "").strip()
    result.brief = str(data.get("brief") or "")
    result.objects = [str(x) for x in (data.get("objects") or []) if str(x).strip()]
    result.logics = [str(x) for x in (data.get("logics") or []) if str(x).strip()]


__all__ = ["ScoutResult", "scout_query", "SCOUT_TOOLS", "SCOUT_SPEC", "MAX_STEPS"]
