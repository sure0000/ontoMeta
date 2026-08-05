"""Spec Drafter 基类。

**LLM 只产声明式 Spec，不产命令**——沿用 ``services/draft_generator.py`` 已确立的
原则：结构由证据确定性推导，LLM 只做语义提升。具体实现见 M6。

安全：**凭据绝不进 LLM 上下文**。Drafter 只能拿到主机别名、组件名、对象名等
非敏感标识；密钥由 Executor 按别名从独立存储取（比照 ``DataSource.dsn_secret_ref``
「仅存引用或加密串」的既有做法）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Drafter(ABC):
    kind: str

    #: 起草前必须由调用方给全的 context 键（即各实现 ``draft`` 里 ``require_context`` 的
    #: 那几个）。**声明出来**是为了让上游能在提案阶段就问清缺什么，而不是等用户点了
    #: 「去校验并执行」才在这里抛 ValueError——见 ``chat_bi._dispatch_propose_action``。
    #: 键的字面值只在这里定义一处，``draft`` 应 ``require_context(context, *self.required_context)``。
    required_context: tuple[str, ...] = ()

    @abstractmethod
    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        """自然语言意图 → 声明式 Spec（可 JSON 序列化的 dict）。"""

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return (intent or self.kind).strip()[:80] or self.kind
