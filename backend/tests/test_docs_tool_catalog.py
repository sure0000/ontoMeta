"""`docs/MCP_README.md` 的工具清单不得与 registry 脱节。

这份清单是手写维护的，于是漂过：工具从 16 涨到 31 的过程里，文档停在「29 个」，
`get_playbook` / `resolve_subject` 压根没进去，而 `locate_entities` 还挂在
「尚未实现」里——它要的能力已经由 `resolve_subject` 实现了。对外文档说错工具名，
接入方照着调就是一个不存在的工具。

和 `test_dsh_skill.py::test_every_registered_tool_has_skill_guidance` 同一个思路：
把「文档齐备」变成被检查的属性。这条失败时的修法是去补文档，不是把工具从这里豁免掉。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.mcp.tools import TOOL_REGISTRY, tool_required_role

README = Path(__file__).parents[2] / "docs/MCP_README.md"
_SECTION_START = "**"
_SECTION_END = "### 尚未实现（设计稿里的名字，别当成可调用工具）"


def _catalog() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("个已注册工具**")
    end = text.index(_SECTION_END)
    return text[start:end]


def _listed() -> dict[str, str]:
    """文档里列出的 工具名 → 最低角色。"""
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^- `([a-z_]+)`（([a-z]+)）", _catalog(), re.M)
    }


def test_every_registered_tool_is_documented():
    missing = sorted(set(TOOL_REGISTRY) - set(_listed()))
    assert not missing, (
        f"这些工具没写进 docs/MCP_README.md 的工具清单：{missing}"
    )


def test_no_phantom_tools_in_the_catalog():
    """文档里列了但 registry 里没有的名字最毒——接入方照着调会直接失败。"""
    phantom = sorted(set(_listed()) - set(TOOL_REGISTRY))
    assert not phantom, f"这些名字不在 registry 里，别列进「已注册工具」：{phantom}"


def test_documented_roles_match_the_server():
    """角色写错比不写更糟：接入方会按文档去申请一个不够用（或过大）的令牌。"""
    drift = {
        name: (listed, tool_required_role(TOOL_REGISTRY[name]))
        for name, listed in _listed().items()
        if name in TOOL_REGISTRY and listed != tool_required_role(TOOL_REGISTRY[name])
    }
    assert not drift, f"最低角色与 registry 不一致（文档值, 实际值）：{drift}"


def test_tool_count_headline_matches():
    text = README.read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+) 个已注册工具\*\*", text)
    assert m, "工具清单缺少「N 个已注册工具」这句"
    assert int(m.group(1)) == len(TOOL_REGISTRY)


def test_not_yet_implemented_list_holds_no_registered_tool():
    """「尚未实现」那一节是给接入方看的能力缺口。做完了不从里面摘掉，读者会以为
    这块还没有——`get_lineage` 一族当年就是这么被漏掉过一次。

    该节里带「已实现，见上」的括注是**指路**，不是缺口声明，先摘掉再判。
    """
    text = README.read_text(encoding="utf-8")
    # 只取这一节本身：后面的「时间表」里还留着当年的 checklist（"实现第一个测试工具
    # （query_ontology）"），那是历史记录，不是能力缺口声明。
    section = text[text.index(_SECTION_END) :]
    section = section.split("\n---", 1)[0]
    section = re.sub(r"（[^（）]*已实现[^（）]*）", "", section)
    still_listed = sorted(
        name for name in TOOL_REGISTRY if f"`{name}`" in section
    )
    assert not still_listed, (
        f"这些工具已经实现了，却还挂在「尚未实现」里：{still_listed}"
    )
