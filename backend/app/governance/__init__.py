"""数据治理规约（Governance Standard）。

一份**可声明、可版本化、可喂给 agent** 的治理标准。区别于散落在各闸门里的
硬编码 if：本包把「标准是什么」收敛成数据，让同一份标准既能约束 agent 的提议、
又能守住执行的闸门。

G0（本次）：只定义 schema + 内置默认规约（把现状硬编码逐条平移，零行为变更）。
规约此时还不被任何闸门读取——G1 起才接进 `agents/validation.py` 的 Validation Gate。
"""

from app.governance.lint import (
    Violation,
    lint_against_standard,
    lint_logical_table,
    lint_spec,
)
from app.governance.standard import (
    DEFAULT_STANDARD,
    GovernanceStandard,
    LayeringStandard,
    NamingStandard,
    RequiredMetadataStandard,
    Rule,
    SecurityStandard,
    Severity,
    TaskStandard,
    TypeStandard,
    active_standard,
)

__all__ = [
    "DEFAULT_STANDARD",
    "GovernanceStandard",
    "LayeringStandard",
    "NamingStandard",
    "RequiredMetadataStandard",
    "Rule",
    "SecurityStandard",
    "Severity",
    "TaskStandard",
    "TypeStandard",
    "Violation",
    "active_standard",
    "lint_against_standard",
    "lint_logical_table",
    "lint_spec",
]
