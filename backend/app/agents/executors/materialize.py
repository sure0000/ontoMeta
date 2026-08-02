"""⑤ 物化 Executor —— 生成 DDL/ETL 并对目标数据源真正落库。

**这是唯一直接对数据源执行写操作的 Executor**：sync/transform/metric 只产产物交
调度器，而物化是用户显式点「执行」要求真正建表落数，故在此调
``services/materialization_runner`` 走 ``data_app_executor.execute_write``。

dry-run 只渲染将建的表清单，不触碰目标库。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.services import materialization_runner


class MaterializeExecutor(Executor):
    kind = "materialize"

    def _plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        """无副作用地算出将要物化的表清单（供 dry-run）。"""
        from app.services.materialization_runner import _generator, _select

        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺少 ontology_id")
        engine = spec.get("engine") or "hive"
        selected = set(spec.get("selected_targets") or []) or None
        with SessionLocal() as db:
            ddl = _generator.generate_ddl(
                db,
                ontology_id,
                engine,
                database_prefix=spec.get("database_prefix"),
                database_overrides=spec.get("database_overrides"),
                table_overrides=spec.get("table_overrides"),
            )
            etl = _generator.generate_etl_sql(
                db,
                ontology_id,
                engine,
                database_prefix=spec.get("database_prefix"),
                database_overrides=spec.get("database_overrides"),
                table_overrides=spec.get("table_overrides"),
                load_strategy=spec.get("load_strategy"),
            )
        return {
            "engine": engine,
            "ddl_tables": [q for q, _ in _select(ddl["statements"], selected)],
            "etl_tables": [q for q, _ in _select(etl["statements"], selected)],
            "unsupported": (ddl.get("unsupported") or []) + (etl.get("unsupported") or []),
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(spec)
        return {
            "action": "materialize_ontology",
            "engine": plan["engine"],
            "create_tables": plan["ddl_tables"],
            "load_tables": plan["etl_tables"],
            "unsupported": plan["unsupported"],
            "side_effects": "无（dry-run 仅渲染将建的表清单，不触碰目标库）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺少 ontology_id")
        with SessionLocal() as db:
            receipt = materialization_runner.run(
                db,
                ontology_id,
                target_datasource_id=spec["target_datasource_id"],
                engine=spec.get("engine") or "hive",
                sync_tool=spec.get("sync_tool"),
                database_prefix=spec.get("database_prefix"),
                database_overrides=spec.get("database_overrides"),
                table_overrides=spec.get("table_overrides"),
                load_strategy=spec.get("load_strategy"),
                selected_targets=spec.get("selected_targets"),
                overrides=spec.get("overrides"),
                # run_id 取制品 id：重复提交在 Airflow 侧因 run_id 冲突而幂等
                artifact_id=context.get("artifact_id"),
            )
        return receipt
