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
        selected = set(spec.get("selected_targets") or []) or None
        with SessionLocal() as db:
            # 引擎由目标数据源类型推定（旧制品显式给了则优先），不再靠表单单选。
            engine = materialization_runner.resolve_engine(
                db, spec.get("target_datasource_id"), spec.get("engine")
            )
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

        # P2：提交前强制跑 preflight，有阻断项就拒绝执行（保护 Data Agent 提交的制品）
        with SessionLocal() as db:
            from app.services.materialize_preflight import run_preflight

            engine = materialization_runner.resolve_engine(
                db, spec.get("target_datasource_id"), spec.get("engine")
            )
            preflight = run_preflight(
                db,
                ontology_id,
                target_datasource_id=spec["target_datasource_id"],
                engine=engine,
                selected_targets=spec.get("selected_targets"),
            )
            if not preflight.ok:
                blocking = [
                    f"[{i.label}] {i.detail}" + (f" → {i.next_step}" if i.next_step else "")
                    for i in preflight.blocking_failures
                ]
                raise RuntimeError(
                    f"提交前自检发现 {len(blocking)} 项阻断，无法执行物化：\n"
                    + "\n".join(f"  {idx+1}. {msg}" for idx, msg in enumerate(blocking))
                )

        with SessionLocal() as db:
            receipt = materialization_runner.run(
                db,
                ontology_id,
                target_datasource_id=spec["target_datasource_id"],
                engine=materialization_runner.resolve_engine(
                    db, spec.get("target_datasource_id"), spec.get("engine")
                ),
                database_prefix=spec.get("database_prefix"),
                database_overrides=spec.get("database_overrides"),
                table_overrides=spec.get("table_overrides"),
                load_strategy=spec.get("load_strategy"),
                selected_targets=spec.get("selected_targets"),
                overrides=spec.get("overrides"),
                # 整批调度：runner 在对齐契约之后展开到各选中契约（见其 refresh_cron 形参）。
                refresh_cron=spec.get("refresh_cron"),
                # run_id 取制品 id：重复提交在 Airflow 侧因 run_id 冲突而幂等
                artifact_id=context.get("artifact_id"),
            )
        return receipt
