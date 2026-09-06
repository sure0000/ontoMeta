"""运维脚本调的端点必须真实存在。

审计发现 ``scripts/e2e_task_full_flow.py`` 在调 ``/agents/pipelines*`` 共 5 个端点，
而手工任务链早已随六环确认一起删除——脚本跑起来只会得到一串 404。这类腐烂没有任何
信号：脚本不进 CI、不被 import，删端点时也不会有人想起去改它。

这条用例把脚本里的端点路径抽出来，和 FastAPI 真实注册的路由对账。端点被删时，
它会红在这里，而不是等某天有人去跑冒烟才发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCRIPTS = [
    Path(__file__).resolve().parent.parent.parent / "scripts" / "smoke.py",
    Path(__file__).resolve().parent.parent.parent / "scripts" / "e2e_task_full_flow.py",
]

#: 脚本里出现的 /api/... 或 _req("GET", "/xxx") 形式的路径。
_PATH_IN_CALL = re.compile(r'"(/(?:api/)?[a-z0-9][a-z0-9/_{}.-]*)"')

#: 路径里的插值段（f-string 的 {var}、以及真实路由的 {param}）统一成通配。
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _registered_routes() -> set[tuple[str, str]]:
    """真实注册的 (方法, 路径)。

    取自 OpenAPI schema 而不是 ``app.routes``：本版 FastAPI 把 include_router 进来的
    路由收在一个 ``_IncludedRouter`` 条目里，不摊平到 ``app.routes``，直接遍历只能看到
    7 条（/docs、/health 之类），会把所有业务端点误判成「不存在」。
    """
    from app.main import app

    routes: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        normalized = _PLACEHOLDER.sub("{}", path)
        for method in operations:
            routes.add((method.upper(), normalized))
    return routes


def _script_calls(source: str) -> set[tuple[str, str]]:
    """从脚本里抽出 (方法, 路径)。只认得出方法的才收，收不准的宁可漏。"""
    calls: set[tuple[str, str]] = set()
    for match in re.finditer(
        r'(?:call|_req)\(\s*"(GET|POST|PUT|PATCH|DELETE)"\s*,\s*f?"([^"]+)"', source
    ):
        method, path = match.group(1), match.group(2)
        if not path.startswith("/"):
            continue
        path = path.split("?")[0]
        if not path.startswith("/api"):
            path = "/api" + path
        calls.add((method, _PLACEHOLDER.sub("{}", path)))
    return calls


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_script_endpoints_still_exist(script):
    assert script.exists(), f"脚本不见了：{script}"
    registered = _registered_routes()
    calls = _script_calls(script.read_text())
    assert calls, f"{script.name} 里没抽到任何端点调用——正则该跟着脚本写法更新了"

    missing = sorted(f"{m} {p}" for m, p in calls if (m, p) not in registered)
    assert not missing, (
        f"{script.name} 调了不存在的端点（后端删了、脚本没跟上）：{missing}"
    )


def test_e2e_script_no_longer_references_deleted_task_chains():
    """任务链端点是被删掉的那批，脚本里不该再出现对它们的调用。

    docstring 里说明「为什么没有了」是可以的，调用不行。
    """
    source = _SCRIPTS[1].read_text()
    calls = _script_calls(source)
    assert not [p for _, p in calls if "pipelines" in p], (
        "脚本仍在调用已删除的任务链端点"
    )
