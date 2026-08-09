"""确定性 Executor 基类。

约束：
- **幂等**：同一 Spec 重复执行，结果一致、不产生重复副作用。
- **先 dry-run 再执行**：差异必须能呈现给人确认。
- **不自行 SSH / 不碰主机**：Executor 只产出可部署产物并交接，不直接登录目标机。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Executor(ABC):
    kind: str

    @abstractmethod
    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """返回将要发生的变更差异，不产生副作用。"""

    @abstractmethod
    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """执行并返回回执。必须幂等。"""
