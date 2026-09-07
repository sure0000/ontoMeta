"""读懂 LLM 这次把 JSON 写成了什么样。

**为什么单独一个模块**：``response_format={"type":"json_object"}`` 不是所有 provider 都遵守。
自建 GLM 端点会把 JSON 裹进 ```json 代码围栏，也有模型在 JSON 前后加一两句解说。
本仓为此付过代价——围栏没剥掉 → 整个域几十块命名静默落空，草稿「生成成功」而名字全是表名
（见 ``LlmResponseFormatError`` 的注释）。

这段口径原先长在 ``OntologyDraftGenerator`` 的私有方法里。键族判定要发同样形态的请求，
与其抄一份，不如抽出来共用——**第二份口径就是下一个 bug**。

注意：这里做的都是「读懂模型怎么写的」，**不是降级**。读不出 JSON 对象就抛，
绝不回退空字典让调用方以为拿到了空结果。
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("ontometa.llm_json")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


class LlmResponseFormatError(RuntimeError):
    """LLM 返回内容无法解析成调用方需要的 JSON 对象。

    以前这里回退空字典 → 结果静默落空；现在如实抛错，由调用方重试或失败上报。
    """


def unwrap_json_text(content: str) -> str:
    """剥掉 LLM 常见的包装，返回第一段能解析成 JSON 的文本（都不行则返回空串）。

    依次试：原文 → 围栏内容 → 最外层 ``{...}`` → 最外层 ``[...]``，逐个**真解析**，
    谁先成功用谁（只截取不解析会把顶层数组的外层方括号剥掉，反而弄坏它）。
    """
    text = (content or "").strip()
    if not text:
        return ""
    candidates = [text]
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        return candidate
    return ""


def coerce_json_object(content: str, *, primary_list_key: str) -> dict:
    """把 LLM 返回文本归一成顶层字典。

    - 外层包装（代码围栏 / 前后解说）先剥掉；
    - 顶层是 dict：原样返回；
    - 顶层是 ``[dict]`` 单元素包裹：拆包（常见的「用数组裹一层」写法）；
    - 顶层是其它数组：按调用方语境归到 ``primary_list_key``。

    实在读不出 JSON 对象则抛 :class:`LlmResponseFormatError`。
    """
    text = unwrap_json_text(content)
    if not text:
        raise LlmResponseFormatError(
            f"LLM 返回内容不是合法 JSON（片段：{(content or '')[:120]!r}）"
        )
    data = json.loads(text)
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
        logger.warning(
            "LLM 返回顶层数组（未遵守 json_object），按 %s 归一化", primary_list_key
        )
        return {primary_list_key: data}
    raise LlmResponseFormatError(
        f"LLM 返回的不是 JSON 对象/数组（{type(data).__name__}）"
    )
