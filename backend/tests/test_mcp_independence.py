"""MCP 不得依赖 Data Agent（对话模块）。

目标是「Data Agent 可以整块删掉，MCP + skill 照常工作」。这条只靠人自觉是守不住的：
`from app.services.chat_bi import ...` 写下去，测试全绿、真机也全绿，直到有人真去删
`chat_bi.py` 那天才发现建数流程整条哑掉。所以把它钉成一条被检查的属性。

它抓的是**静态 import**（含函数内的局部导入），不是运行期反射——后者本仓没有先例，
真出现了会在删除演练里当场炸出来。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_MCP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app" / "mcp"

#: 对话模块的全部入口。删除 Data Agent 时这几个模块会一起消失。
_FORBIDDEN_PREFIXES = (
    "app.services.chat_bi",
    "app.schemas.chat_bi",
    "app.models.chat_bi",
    "app.api.chat_bi",
)


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _MCP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_mcp_module_does_not_import_chat_bi(path: pathlib.Path) -> None:
    offenders = sorted(
        module
        for module in _imported_modules(path)
        if module.startswith(_FORBIDDEN_PREFIXES)
    )
    assert not offenders, (
        f"{path.relative_to(_MCP_ROOT.parent.parent)} 依赖了对话模块：{offenders}。"
        "共用的东西请搬到中性位置（services/task_form、services/agent_sql、"
        "schemas/task_form），别让 MCP 反过来依赖 Data Agent。"
    )


_ISOLATION_SCRIPT = """
import sys

class _Block:
    '''把对话模块从导入图上摘掉——等价于「chat_bi.py 已经被删了」。'''

    def find_module(self, name, path=None):
        return self if name.startswith("app.services.chat_bi") else None

    def find_spec(self, name, path=None, target=None):
        if name.startswith("app.services.chat_bi"):
            raise ModuleNotFoundError(f"No module named {name!r}（删除演练）")
        return None

sys.meta_path.insert(0, _Block())

from app.mcp.tools import TOOL_REGISTRY

print(len(TOOL_REGISTRY))
"""


def test_mcp_tools_load_in_a_process_without_chat_bi() -> None:
    """在**新解释器**里把 `app.services.chat_bi` 挡掉，MCP 工具注册表仍要装配得出来。

    比静态扫描更进一步：它证明的是「删了那个模块，整套工具照样起得来」，也覆盖了经由
    `app.api.deps` 之类中转包间接被拽进来的情况。放子进程跑是为了不污染本进程的
    `sys.modules`——在同进程里重导入 `app.mcp.*` 会造出第二份工具实例，后续用例的
    monkeypatch 就会打在没人用的那一份上。
    """
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "屏蔽 app.services.chat_bi 后 MCP 装配失败——说明还有一条依赖对话模块的路：\n"
        + proc.stderr[-3000:]
    )
    assert int(proc.stdout.strip().splitlines()[-1]) >= 37


def test_shared_api_dependencies_are_lazy_without_chat_bi() -> None:
    """Removing the optional chat router must not break unrelated API imports."""
    script = """
import sys

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.startswith("app.services.chat_bi"):
            raise ModuleNotFoundError(name)
        return None

sys.meta_path.insert(0, _Block())
import app.api.deps
print("OK")
"""
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip() == "OK"
