"""部分模型不走原生 function-calling，而把工具调用以 DSML/XML 文本写进正文。
验证：agent 能解析并执行这些文本工具调用，且绝不把 DSML 标记当答案/思考输出。
"""

from __future__ import annotations

from app.services.chat_bi import ChatBiService

_DSML = (
    "<｜｜DSML｜｜tool_calls>\n"
    '<｜｜DSML｜｜invoke name="search_relations">\n'
    '<｜｜DSML｜｜parameter name="keyword" string="true">分组</｜｜DSML｜｜parameter>\n'
    "</｜｜DSML｜｜invoke>\n"
    '<｜｜DSML｜｜invoke name="search_relations">\n'
    '<｜｜DSML｜｜parameter name="keyword" string="true">换算为</｜｜DSML｜｜parameter>\n'
    "</｜｜DSML｜｜invoke>\n"
    "</｜｜DSML｜｜tool_calls>"
)


def test_parse_dsml_tool_calls():
    calls = ChatBiService._extract_text_tool_calls(_DSML)
    assert [c.function.name for c in calls] == ["search_relations", "search_relations"]
    import json

    assert json.loads(calls[0].function.arguments) == {"keyword": "分组"}
    assert json.loads(calls[1].function.arguments) == {"keyword": "换算为"}


def test_strip_dsml_markup_keeps_prose():
    assert ChatBiService._strip_tool_markup("我来查一下。" + _DSML + "查完了") == "我来查一下。查完了"
    # 纯标记 → 清空
    assert ChatBiService._strip_tool_markup(_DSML) == ""
    # 未闭合残片也要清掉
    assert ChatBiService._strip_tool_markup(
        '正文<｜｜DSML｜｜invoke name="x"><｜｜DSML｜｜parameter name="k">v'
    ) == "正文"


def test_no_markup_passthrough():
    assert ChatBiService._extract_text_tool_calls("普通答案，无工具调用。") == []
    assert ChatBiService._strip_tool_markup("普通答案，无工具调用。") == "普通答案，无工具调用。"


def test_generic_xml_invoke_without_dsml_prefix():
    """兼容不带 DSML 前缀的通用 <invoke>/<parameter> 文本工具调用。"""
    xml = (
        '<invoke name="get_domain_overview">'
        '<parameter name="q">全部</parameter>'
        "</invoke>"
    )
    calls = ChatBiService._extract_text_tool_calls(xml)
    assert len(calls) == 1
    assert calls[0].function.name == "get_domain_overview"
