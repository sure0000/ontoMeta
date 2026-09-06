"""「已建成、尚未接线」的模块清单，以及防止它悄悄变长的闸。

审计发现有几个服务只被自己的测试引用——完整、有测试、注释详尽，但生产代码里零调用方。
它们不是写错了，是在等一个上层决策；问题在于**从代码里看不出来**，下一个读到它的人
会以为这条链路已经在跑。

这条用例做两件事：
1. 把清单显式化——接线或删除时必须回来改这里，改不动就说明动作没做干净；
2. 反向拦截——清单外的模块如果变成零调用方，这里会红，而不是等下一次审计才发现。

判据用的是**静态 import 可达性**，与审计当时的算法一致：从 ``app.main`` 出发遍历
import 图，再把动态注册（MCP 工具靠装饰器注册、jobs 是子进程入口）排除掉。
"""

from __future__ import annotations

import ast
import pathlib

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"

#: 已知「建成未接线」。每一项都要写清在等什么——只写模块名等于没说。
KNOWN_UNWIRED = {
    "app.services.confidence_calibration": "等真实人工复核信号积累够，再决定是否替换分类器的死映射",
    "app.services.object_resolution": "等归并候选落到哪条治理流程的决策",
    "app.services.ops_live_eval": "评测工装，由 scripts/run_ops_live_eval.py 驱动，本就不该进生产路径",
}

#: 不参与可达性判定的模块。它们不经 import 图被引用，静态分析必然判成不可达。
_DYNAMIC_ENTRYPOINTS = (
    "app.mcp.tools.",       # @register_tool 装饰器注册
    "app.mcp.server",
    "app.mcp.auth",
    "app.mcp.__main__",
    "app.jobs",             # 分离子进程入口
    "app.agents.drafters",  # 包 __init__ 里注册
    "app.agents.executors",
    "app.warehouse.adapters",
    "app.schemas.",         # Pydantic 模型按需 import，不都在主图里
)


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(_APP.parent).with_suffix("")
    name = ".".join(rel.parts)
    return name[:-9] if name.endswith(".__init__") else name


def _import_graph() -> tuple[dict[str, pathlib.Path], dict[str, set[str]]]:
    modules = {
        _module_name(p): p
        for p in _APP.rglob("*.py")
        if "__pycache__" not in str(p)
    }
    edges: dict[str, set[str]] = {}
    for name, path in modules.items():
        targets: set[str] = set()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - 语法错会被别的用例先抓到
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(a.name for a in node.names if a.name.startswith("app"))
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                targets.add(node.module)
                targets.update(f"{node.module}.{a.name}" for a in node.names)
        edges[name] = targets
    return modules, edges


def _reachable_from_main(modules, edges) -> set[str]:
    seen: set[str] = set()
    stack = ["app.main"]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(t for t in edges.get(current, ()) if t in modules)
    return seen


def test_unwired_inventory_is_accurate():
    """清单要与实际一致：既不能漏记，也不能记了却其实已经接上。"""
    modules, edges = _import_graph()
    reachable = _reachable_from_main(modules, edges)

    unreachable = {
        name
        for name in modules
        if name not in reachable and not name.startswith(_DYNAMIC_ENTRYPOINTS)
    }

    unlisted = unreachable - set(KNOWN_UNWIRED)
    assert not unlisted, (
        "这些模块没有生产调用方，但不在 KNOWN_UNWIRED 里。"
        "要么把它接进调用链，要么删掉，要么登记进清单并写清在等什么："
        f"{sorted(unlisted)}"
    )

    stale = set(KNOWN_UNWIRED) - unreachable
    assert not stale, (
        f"这些模块已经有调用方了，清单该更新：{sorted(stale)}"
    )


def test_every_unwired_module_says_what_it_is_waiting_for():
    """清单里只写模块名等于没说——必须写清接线前提。"""
    for name, reason in KNOWN_UNWIRED.items():
        assert len(reason) > 10, f"{name} 的登记理由太短，说不清在等什么"


def test_unwired_modules_carry_the_status_banner_in_their_docstring():
    """状态要写在模块自己的 docstring 里。

    只登记在测试文件里没用——读代码的人打开的是那个模块，不是这份清单。
    """
    for name in KNOWN_UNWIRED:
        path = _APP.parent / (name.replace(".", "/") + ".py")
        assert path.exists(), name
        head = path.read_text()[:2000]
        assert "尚未接线" in head or "不该进生产路径" in head, (
            f"{name} 的模块 docstring 里没有说明它尚未接线"
        )
