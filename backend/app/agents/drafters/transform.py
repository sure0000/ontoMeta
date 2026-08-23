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
            target_datasource_id = context.get("target_datasource_id")
            if target_datasource_id:
                from app.models import DataSource
                from app.warehouse.policy import require_doris_datasource

                datasource = db.get(DataSource, target_datasource_id)
                require_doris_datasource(datasource, operation="Doris Transform")
                if not datasource.is_default_warehouse:
                    raise ValueError("Transform 只能使用默认 Doris")
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
                # 见 sync.py 同名字段：缺它执行器只渲染 SQL 不落库。
                "target_datasource_id": context.get("target_datasource_id") or None,
                # 表单显式选的层优先——此前一律取契约（无契约则 dim），用户在表单里
                # 选的 dwd/dws 被静默丢弃，建出来的表落在错误的层。
                "target_layer": context.get("target_layer")
                or (contract.target_layer if contract else "dim"),
                "database_prefix": context.get("database_prefix"),
                "engine": "doris",
                "cleansing_rules": self._rules_from_context(context.get("cleansing_rules"))
                or self._rules(intent),
                # 表单填的备注优先；对话路径仍把未匹配成规则的原文留在这里交人处理。
                "notes": context.get("notes") or intent,
                "schedule": context.get("schedule") or context.get("refresh_cron"),
            }

    # code → 描述，用于把表单下拉给的规则码结构化成 {rule, description}。
    _RULE_DESC: dict[str, str] = {code: desc for _p, code, desc in _RULE_PATTERNS}

    @classmethod
    def _rules_from_context(cls, raw: Any) -> list[dict[str, str]]:
        """表单多选给的是规则码列表（如 ["deduplicate"]）；结构化成执行器要的
        [{rule, description}]。已是 dict 的原样保留；未知码丢弃（闭集外不臆造）。"""
        if not raw or not isinstance(raw, (list, tuple)):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                out.append({"rule": str(item["rule"]), "description": str(item.get("description") or cls._RULE_DESC.get(str(item["rule"]), ""))})
            elif isinstance(item, str) and item in cls._RULE_DESC:
                out.append({"rule": item, "description": cls._RULE_DESC[item]})
        return out

    @staticmethod
    def _rules(intent: str) -> list[dict[str, str]]:
        text = intent or ""
        return [
            {"rule": code, "description": desc}
            for pattern, code, desc in _RULE_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)
        ]

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return self.name_from_spec(spec)

    def name_from_spec(self, spec: dict[str, Any]) -> str:
        return f"ETL · {spec.get('target_table')}"
