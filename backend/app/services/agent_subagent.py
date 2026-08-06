"""通用子 agent 骨架（V4 O4）：把「重活关进隔离上下文、只回结论」这件事**框架化**。

**为什么存在**：P4.2 的检索子 agent（`retrieval_agent.py`）证明了隔离上下文的价值——
定位实体的试错（宽搜→取详→换词再搜）往主上下文塞 ~8000 字符，而其中绝大部分是**过程**、
不是**结论**。但那套循环是写死给检索的。data-agent 里还有别的重活同样该隔离（如取数前的
多列 profile + join 探路）。与其复制第二套几乎一样的循环，不如把循环骨架抽出来。

**骨架 = 一个受限的工具编排循环**：自己的消息列表、**工具子集**（越权明确回绝）、步数预算、
单结果字符预算；跑完把最终文本 + 度量交回，**由调用方按自己的 schema 解析结论**。
骨架不认识「objects/logics」这类领域结论——那是各子 agent 自己的事，塞进骨架就又耦合死了。

**不变式**：
- 工具**只增不越权**：`allowed_tools` 之外的调用回绝而非静默忽略（否则模型死循环重试）。
- 隔离字符**不进主上下文**：`isolated_chars` 仅供度量，回主上下文的只有结论文本。
- 故障**不外溢**：任何异常由调用方兜底降级，骨架只管把循环跑完。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("ontometa.subagent")


@dataclass(frozen=True)
class SubAgentSpec:
    """一个子 agent 的能力声明：系统提示 + 允许的工具 + 预算。"""

    name: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    max_steps: int = 4
    tool_result_max_chars: int = 12000
    # 步数耗尽时强制收尾的追加指令（不带工具，逼它给结论）。
    final_nudge: str = "请立即只输出结论，不要再调用工具。"


@dataclass
class SubAgentRun:
    """骨架的产出。``final_text`` 交给调用方解析；其余是度量。"""

    final_text: str = ""
    steps: int = 0
    llm_calls: int = 0
    # 子上下文里实际流动过的字符数——**不进主上下文**，只度量隔离效果。
    isolated_chars: int = 0


async def run_subagent(
    db: Session,
    *,
    client,
    model: str,
    spec: SubAgentSpec,
    user_prompt: str,
    domain_id: str,
    ontology_id: str,
    dispatch: Callable[..., tuple[Any, str, bool]],
    tool_schemas: list[dict],
    to_thread,
) -> SubAgentRun:
    """在隔离上下文里跑 ``spec`` 声明的受限工具循环，返回最终文本 + 度量。

    ``dispatch`` / ``tool_schemas`` 由主 agent 注入，保证子 agent 与主 agent 的工具行为
    **完全一致**——不另起一套实现，否则两边语义迟早分叉（沿用 retrieval_agent 的纪律）。
    """
    allowed = set(spec.allowed_tools)
    schemas = [
        t for t in tool_schemas if t.get("function", {}).get("name") in allowed
    ]
    messages: list[dict] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    run = SubAgentRun()

    for _ in range(spec.max_steps):
        run.llm_calls += 1
        resp = await client.chat.completions.create(
            model=model, messages=messages, tools=schemas, tool_choice="auto"
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        if not tool_calls:
            run.final_text = msg.content or ""
            return run

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": t.id, "type": "function",
                 "function": {"name": t.function.name, "arguments": t.function.arguments}}
                for t in tool_calls
            ],
        })
        for t in tool_calls:
            name = t.function.name
            if name not in allowed:
                # 越权调用：明确回绝而非静默忽略，否则模型会一直重试；且**不计步数**。
                payload: Any = {
                    "error": f"{spec.name} 子 agent 不能调用 {name}，"
                    f"只能用：{', '.join(sorted(allowed))}。"
                }
            else:
                try:
                    args = json.loads(t.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                payload, _summary, _err = await to_thread(
                    dispatch, db, domain_id=domain_id, ontology_id=ontology_id,
                    name=name, args=args,
                )
                run.steps += 1
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) > spec.tool_result_max_chars:
                text = text[: spec.tool_result_max_chars]
            run.isolated_chars += len(text)
            messages.append({"role": "tool", "tool_call_id": t.id, "content": text})

    # 步数耗尽：再要一次结论，不带工具。
    run.llm_calls += 1
    resp = await client.chat.completions.create(
        model=model,
        messages=messages + [{"role": "user", "content": spec.final_nudge}],
    )
    run.final_text = resp.choices[0].message.content or ""
    return run


__all__ = ["SubAgentSpec", "SubAgentRun", "run_subagent"]
