"""③ ETL 任务 Drafter —— 「给出数据清洗需求，智能体自动创建 ETL 任务」。

结构不由模型编：目标表、字段映射、源表全部来自本体与物化契约（M1/M3 已具备）。
Drafter 只做两件事——**挑出意图指向的目标表**，以及**把清洗需求结构化成规则**。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.common import require_context, select_by_intent
from app.agents.drafters.base import Drafter
from app.database import SessionLocal
from app.models import MaterializationContract, ObjectType
from app.models.warehouse import TargetKind

# 清洗需求 → 结构化规则。命中不了的原文保留在 notes 里交人处理，不臆造规则。
_RULE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"去重|重复|dedup", "deduplicate", "按主键去重"),
    (r"空值|为空|null|缺失", "drop_null", "过滤关键字段空值"),
    (r"去空格|trim|首尾空", "trim", "字符串首尾去空格"),
    (r"大写|uppercase", "uppercase", "统一转大写"),
    (r"小写|lowercase", "lowercase", "统一转小写"),
    (r"标准化|统一编码|归一", "normalize_code", "编码标准化"),
)

# 可产出的清洗规则词表（闭集）。**对外公开**是因为这是一份能力边界：说不出的清洗需求
# 会被静默丢掉（只留在 notes 里），故 Data Agent 要在提需求时就把这份词表摆给用户，
# 而不是产出一个什么都不做的 ETL 任务。见 chat_bi._entity_task_options。
SUPPORTED_CLEANSING_RULES: tuple[tuple[str, str], ...] = tuple(
    (code, desc) for _pattern, code, desc in _RULE_PATTERNS
)


class TransformDrafter(Drafter):
    kind = "transform"
    required_context = ("ontology_id",)

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        ontology_id = context["ontology_id"]
        with SessionLocal() as db:
            objects = (
                db.query(ObjectType)
                .filter(ObjectType.ontology_id == ontology_id)
                .all()
            )
            explicit = context.get("target_table")
            target = (
                next((o for o in objects if o.name == explicit), None)
                if explicit
                else select_by_intent(
                    intent, objects, key=lambda o: (o.name, o.display_name, o.description)
                )
            )
            if target is None:
                raise ValueError("未在本体中找到匹配的目标对象；请在 context.target_table 指定")

            contract = (
                db.query(MaterializationContract)
                .filter(
                    MaterializationContract.ontology_id == ontology_id,
                    MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
                    MaterializationContract.target_id == target.id,
                )
                .first()
            )
            return {
                "target_table": target.name,
                "ontology_id": ontology_id,
                "engine": context.get("engine")
                or (contract.engines[0] if contract and contract.engines else "hive"),
                "target_layer": contract.target_layer if contract else "dim",
                "database_prefix": context.get("database_prefix"),
                "execution_mode": context.get("execution_mode") or "batch",  # P1-7: batch/streaming
                "cleansing_rules": self._rules(intent),
                "notes": intent,
            }

    @staticmethod
    def _rules(intent: str) -> list[dict[str, str]]:
        text = intent or ""
        return [
            {"rule": code, "description": desc}
            for pattern, code, desc in _RULE_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)
        ]

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return f"ETL · {spec.get('target_table')}"
