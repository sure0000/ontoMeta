"""制品类型 → (Drafter, Executor) 注册表。

M5 只提供骨架；四类具体实现在 M6 按「④指标 → ③ETL → ①同步 → ⓪部署」
（风险由低到高）逐个注册。未注册的 kind 由 API 返回 501，而不是静默放行。
"""

from __future__ import annotations

from app.agents.drafters.base import Drafter
from app.agents.executors.base import Executor
from app.models.agent import ArtifactKind

_DRAFTERS: dict[str, Drafter] = {}
_EXECUTORS: dict[str, Executor] = {}


class UnregisteredKindError(LookupError):
    def __init__(self, kind: str):
        super().__init__(f"制品类型 {kind!r} 的 Drafter/Executor 尚未实现（M6）")
        self.kind = kind


def register(kind: str, drafter: Drafter, executor: Executor) -> None:
    _DRAFTERS[kind] = drafter
    _EXECUTORS[kind] = executor


def unregister(kind: str) -> None:
    _DRAFTERS.pop(kind, None)
    _EXECUTORS.pop(kind, None)


def get_drafter(kind: str) -> Drafter:
    try:
        return _DRAFTERS[kind]
    except KeyError:
        raise UnregisteredKindError(kind) from None


def get_executor(kind: str) -> Executor:
    try:
        return _EXECUTORS[kind]
    except KeyError:
        raise UnregisteredKindError(kind) from None


def registered_kinds() -> list[str]:
    return sorted(_DRAFTERS)


def all_kinds() -> list[str]:
    return [k.value for k in ArtifactKind]
