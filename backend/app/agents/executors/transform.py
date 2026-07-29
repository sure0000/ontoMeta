"""③ ETL 任务 Executor。

字段映射 SQL **直接复用 M3 的 ``warehouse_generator``**，不重写一份——
两份 ODS→目标层的映射逻辑必然分叉。本执行器只在其产出之上叠加清洗规则。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.services.warehouse_generator import WarehouseGenerator

_generator = WarehouseGenerator()

# 清洗规则 → 叠加在生成 SQL 之上的 SQL 片段。
# 只覆盖能确定性表达的规则；其余留在 unapplied 里交人处理，不硬凑。
_APPLIABLE = {"drop_null", "deduplicate"}


class TransformExecutor(Executor):
    kind = "transform"

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺少 ontology_id")
        engine = spec.get("engine") or "hive"
        with SessionLocal() as db:
            result = _generator.generate_etl_sql(
                db,
                ontology_id,
                engine,
                database_prefix=spec.get("database_prefix"),
            )

        target = spec.get("target_table")
        match = next(
            (
                (qualified, sql)
                for qualified, sql in result["statements"].items()
                if qualified.split(".")[-1] == target
            ),
            None,
        )
        if match is None:
            reasons = [
                u["reason"] for u in result["unsupported"] if u["target"] in (target, "")
            ]
            raise ValueError(
                f"目标表 {target} 无法生成 ETL"
                + (f"：{reasons[0]}" if reasons else "（请先执行物化契约推导）")
            )

        qualified, sql = match
        rules = [r["rule"] for r in spec.get("cleansing_rules") or []]
        applied = [r for r in rules if r in _APPLIABLE]
        unapplied = [r for r in rules if r not in _APPLIABLE]

        annotated = sql
        if applied:
            header = "\n".join(f"-- 清洗规则：{r}" for r in applied)
            annotated = f"{header}\n{sql}"
        if "deduplicate" in applied:
            annotated = annotated.replace("SELECT\n", "SELECT DISTINCT\n", 1)

        return {
            "engine": engine,
            "target_table": qualified,
            "sql": annotated,
            "applied_rules": applied,
            # 不静默丢弃：无法确定性表达的规则显式列出交人处理。
            "unapplied_rules": unapplied,
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        return {
            "action": "overwrite_target_table",
            "target_table": artifacts["target_table"],
            "sql": artifacts["sql"],
            "applied_rules": artifacts["applied_rules"],
            "unapplied_rules": artifacts["unapplied_rules"],
            "side_effects": "无（dry-run 仅渲染 SQL）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
            **self._artifacts(spec),
            "handoff": "DolphinScheduler",
            "note": "ontoMeta 只生成产物，不直接执行",
        }
