"""思考流水：模型自述的提取、压句，以及轨迹步在两条返回路径上的一致性。

界面上「思考」那一栏的内容全部来自这条链：提示词要求模型写 `<thinking>` →
`_extract_thinking` 摘出来 → `_thought_headline` 压成一行 → 作为 `kind="thought"`
的伪步进 `steps`。链上任何一环静默丢字，用户看到的就是空白行或一段撑爆版面的长文。
"""

from __future__ import annotations

from app.schemas.chat_bi import ChatBiAgentStep, ChatBiAnswer
from app.services.chat_bi import ChatBiService
from app.services.chat_bi_tool_schemas import _AGENT_SYSTEM_PROMPT


def test_prompt_asks_for_thinking_tag():
    """没有这句要求，`_extract_thinking` 就是死代码——模型不会主动写这个标签。"""
    assert "<thinking>" in _AGENT_SYSTEM_PROMPT


def test_extract_single_thinking_block():
    thinking, rest = ChatBiService._extract_thinking(
        "<thinking>先确认这个对象存不存在。</thinking>我来查一下。"
    )
    assert thinking == "先确认这个对象存不存在。"
    assert rest == "我来查一下。"


def test_extract_handles_model_variants():
    """一轮多段、`<think>` 简写、标签未闭合——三种走样都不能把裸标记漏进正文。"""
    multi, rest = ChatBiService._extract_thinking("<thinking>甲</thinking>正文<thinking>乙</thinking>")
    assert multi == "甲 乙"
    assert rest == "正文"

    short, rest = ChatBiService._extract_thinking("<think>简写也认</think>正文")
    assert short == "简写也认"
    assert rest == "正文"

    unclosed, rest = ChatBiService._extract_thinking("正文<thinking>忘了闭合")
    assert unclosed == "忘了闭合"
    assert rest == "正文"
    assert "<thinking>" not in rest


def test_extract_no_tag_leaves_text_alone():
    thinking, rest = ChatBiService._extract_thinking("普通开场白。")
    assert thinking == ""
    assert rest == "普通开场白。"


def test_headline_takes_first_sentence_and_caps_length():
    assert (
        ChatBiService._thought_headline("先确认对象存不存在。然后再看字段。还要查血缘。")
        == "先确认对象存不存在。"
    )
    # 无句读的长段落：截断而不是整段塞进时间线
    long = "甲" * 200
    headline = ChatBiService._thought_headline(long)
    assert len(headline) == 60
    assert headline.endswith("…")


def test_headline_normalizes_whitespace_and_markdown():
    assert ChatBiService._thought_headline("  **先查**\n  一下  ") == "先查 一下"
    assert ChatBiService._thought_headline(None) == ""
    assert ChatBiService._thought_headline("   ") == ""


def test_agent_step_keeps_thought_fields_through_answer_model():
    """非流式 `/chat-bi/ask` 声明了 response_model，模型里缺字段就会被静默剥掉。

    改造前 `ChatBiAgentStep` 没有 kind/text，于是同一次问答：流式路径（裸 dict）有思考句、
    非流式路径的思考步退化成一行空白工具行。这条用例把两条路径的形状钉在一起。
    """
    answer = ChatBiAnswer(
        answer="好的",
        steps=[
            ChatBiAgentStep(index=0, tool="", kind="thought", text="先确认对象存不存在。"),
            ChatBiAgentStep(index=1, tool="search_objects", summary="找到 3 条"),
        ],
    )
    dumped = answer.model_dump()["steps"]
    assert dumped[0]["kind"] == "thought"
    assert dumped[0]["text"] == "先确认对象存不存在。"
    # 工具步不写 kind 时默认归为 tool，前端据此把它渲染成附注行而不是思考句
    assert dumped[1]["kind"] == "tool"
    assert dumped[1]["text"] is None
