"""⑤ 物化 Executor —— 只**建结构**：把本体想要的表在目标数据源里建出来。

物化 = 建表（DDL），同步 = 搬数据（DML），且物化在先。故这里只调
``materialization_runner.run_materialize``，产出的 DAG 只有 ``create_tables``：
**不产任何搬运作业、不产 staging/swap，一行数据都不动**。要把数据搬进来是
同步制品（``executors/sync``）的事。

建表幂等（``CREATE TABLE IF NOT EXISTS``），重复物化跳过已存在的表。
dry-run 只渲染将建的表清单，不触碰目标库。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import DataSource
from app.services import flink_params, materialization_runner


class MaterializeExecutor(Executor):
    kind = "materialize"

    def _plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        """无副作用地算出将要建的表清单（供 dry-run）。

        **只看 DDL**：物化不产搬运作业，此前这里还调 ``generate_etl_sql`` 报一份
        「将装载的表」，拆分后那份清单是谎言——dry-run 说会装载，执行却只建表。
        """
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
        return {
            "engine": engine,
            "ddl_tables": [q for q, _ in _select(ddl["statements"], selected)],
            "unsupported": ddl.get("unsupported") or [],
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(spec)
        return {
            "action": "materialize_ontology",
            "engine": plan["engine"],
            "create_tables": plan["ddl_tables"],
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
            receipt = materialization_runner.run_materialize(
                db,
                ontology_id,
                target_datasource_id=spec["target_datasource_id"],
                engine=materialization_runner.resolve_engine(
                    db, spec.get("target_datasource_id"), spec.get("engine")
                ),
                database_prefix=spec.get("database_prefix"),
                database_overrides=spec.get("database_overrides"),
                table_overrides=spec.get("table_overrides"),
                selected_targets=spec.get("selected_targets"),
                overrides=spec.get("overrides"),
                # 整批调度：runner 在对齐契约之后展开到各选中契约。物化产的是一次性建表
                # DAG（schedule=None），故这里只是把 cron 写回契约供后续同步分组用。
                refresh_cron=spec.get("refresh_cron"),
                # run_id 取制品 id：重复提交在 Airflow 侧因 run_id 冲突而幂等
                artifact_id=context.get("artifact_id"),
                # 物化建表不经 Flink，但 Spec 里的 Flink 参数照样透传下去：一份 Spec
                # 里的参数只有一处口径，别让「物化时填的和同步时填的」变成两回事。
                flink_task_params=flink_params.from_spec(spec, context),
            )
            # Submission is not schema readiness. Persist only credential-free
            # reconciliation inputs in the receipt; Airflow final-success later
            # calls publish_schema_ready().
            target = db.get(DataSource, spec["target_datasource_id"])
            if target is not None:
                receipt["deployment_reconciliation"] = {
                    "ontology_id": ontology_id,
                    "datasource_id": target.id,
                    "artifact_id": context.get("artifact_id"),
                    "database_prefix": spec.get("database_prefix"),
                    "database_overrides": spec.get("database_overrides") or {},
                    "table_overrides": spec.get("table_overrides") or {},
                }
        return receipt
