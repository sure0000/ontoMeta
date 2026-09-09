"""工具目录的**可读性**也是被检查的属性。

73 个工具排成一张英文平表，人是读不下去的——所以每个工具必须自带中文名和分类。
``register_tool`` 在导入期就会拒绝漏写的工具（加工具时直接炸，不会等到界面上才
发现多出一行没归属的灰字）；这里补齐 registry 层拦不住的那几条：中文名不能互相
重复、不能是把英文标识名抄一遍、分类顺序必须真的被目录用上。

失败时的修法是去补 ``display_name`` / ``category``，不是把工具从这里豁免掉。
"""

from __future__ import annotations

import pytest

from app.mcp.introspection import category_catalog, tool_catalog
from app.mcp.tools import (
    TOOL_CATEGORIES,
    TOOL_REGISTRY,
    register_tool,
    tool_category,
    tool_display_name,
)


def test_every_registered_tool_declares_a_chinese_name_and_category():
    bad = {
        name: (tool_display_name(tool), tool_category(tool))
        for name, tool in TOOL_REGISTRY.items()
        if not tool_display_name(tool)
        or tool_display_name(tool) == name
        or tool_category(tool) not in TOOL_CATEGORIES
    }
    assert not bad, f"这些工具缺中文名或分类（中文名, 分类）：{bad}"


def test_display_names_are_actually_chinese():
    """纯 ASCII 的"中文名"等于没起名。允许名字里夹英文专名（Superset / SQL）。"""
    latin_only = sorted(
        name
        for name, tool in TOOL_REGISTRY.items()
        if not any("一" <= ch <= "鿿" for ch in tool_display_name(tool))
    )
    assert not latin_only, f"这些工具的 display_name 里一个汉字都没有：{latin_only}"


def test_display_names_are_unique():
    """两个工具同名，界面上就没法区分该调哪个。"""
    seen: dict[str, list[str]] = {}
    for name, tool in TOOL_REGISTRY.items():
        seen.setdefault(tool_display_name(tool), []).append(name)
    collisions = {label: sorted(v) for label, v in seen.items() if len(v) > 1}
    assert not collisions, f"中文名撞车：{collisions}"


def test_register_tool_rejects_a_tool_without_a_category():
    """漏写要炸在导入期。这条钉的就是"炸"本身——否则守卫哪天被摘掉也没人知道。"""

    with pytest.raises(ValueError, match="category"):

        @register_tool
        class _NoCategory:  # pragma: no cover - 注册即抛
            name = "_test_no_category"
            display_name = "没有分类的工具"
            category = "nonexistent"
            description = ""
            input_schema: dict = {}

    with pytest.raises(ValueError, match="display_name"):

        @register_tool
        class _NoDisplayName:  # pragma: no cover - 注册即抛
            name = "_test_no_display_name"
            display_name = ""
            category = "ontology"
            description = ""
            input_schema: dict = {}

    assert "_test_no_category" not in TOOL_REGISTRY
    assert "_test_no_display_name" not in TOOL_REGISTRY


def test_catalog_is_grouped_in_declared_category_order():
    """目录按分类顺序出，不是按英文名——后者会把毫不相干的工具排成邻居。"""
    order = list(TOOL_CATEGORIES)
    ranks = [order.index(row["category"]) for row in tool_catalog()]
    assert ranks == sorted(ranks), "工具目录没有按 TOOL_CATEGORIES 的声明顺序分组"

    for row in tool_catalog():
        assert row["category_label"] == TOOL_CATEGORIES[row["category"]]


def test_category_catalog_covers_every_tool():
    categories = category_catalog()
    assert sum(c["tool_count"] for c in categories) == len(TOOL_REGISTRY)
    assert [c["key"] for c in categories] == [
        key for key in TOOL_CATEGORIES if any(
            tool_category(t) == key for t in TOOL_REGISTRY.values()
        )
    ]
