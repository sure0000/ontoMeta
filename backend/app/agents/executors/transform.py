"""③ ETL 任务 Executor。

字段映射 SQL **直接复用 M3 的 ``warehouse_generator``**，不重写一份——
两份 ODS→目标层的映射逻辑必然分叉。本执行器只在其产出之上叠加清洗规则。

P1-4 执行路径：Flink on YARN（经 flink_job_runner），未配 SqlRunner JAR 退回「仅产出」。
源(ODS)与目标(dwd)在同一数仓连接（sync 已把源搬进数仓 ODS 层），别名都用 warehouse_conn_id。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import DataSource
from app.services.flink_sql_generator import FlinkEndpoint, generate_flink_sql
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
        # 未配 SqlRunner JAR 或未配 target_datasource，退回「仅产出」（与 metric 同逻辑）
        target_datasource_id = spec.get("target_datasource_id") or context.get("target_datasource_id")
        if not target_datasource_id:
            return {
                **self._artifacts(spec),
                "handoff": "DolphinScheduler",
                "note": "未配置 target_datasource_id，ontoMeta 只生成 SQL，不执行（链上游会传入此字段）",
            }

        # Flink on YARN 执行路径（P1-4）：源与目标在同一数仓，别名都用 warehouse_conn_id
        try:
            from app.services.flink_job_runner import run_flink_sql
            from app.services.airflow_dag_builder import FlinkSqlTask
        except ImportError as exc:
            return {
                **self._artifacts(spec),
                "handoff": "import_error",
                "note": f"Flink 模块导入失败：{exc}，退回仅产出",
            }

        ontology_id = spec.get("ontology_id")
        target_table = spec.get("target_table")
        engine = spec.get("engine") or "hive"
        execution_mode = spec.get("execution_mode") or "batch"

        with SessionLocal() as db:
            ds = db.get(DataSource, target_datasource_id)
            if not ds:
                return {
                    **self._artifacts(spec),
                    "handoff": "datasource_not_found",
                    "note": f"target_datasource_id={target_datasource_id} 不存在，退回仅产出",
                }
            warehouse_conn_id = _warehouse_conn_id(ds)

            # 从 warehouse_generator 取 Flink ETL 结构化输入（守住映射逻辑不重写）
            try:
                flink_input = _generator.build_flink_etl_input(
                    db,
                    ontology_id,
                    target_table,
                    engine,
                    database_prefix=spec.get("database_prefix"),
                )
            except ValueError as exc:
                return {
                    **self._artifacts(spec),
                    "handoff": "flink_input_error",
                    "note": f"构造 Flink ETL 输入失败：{exc}，退回仅产出",
                }

            # 叠加清洗规则到 SELECT 体（deduplicate → DISTINCT，其余规则待 P1 完整实现）
            select_body = flink_input["select_body"]
            rules = [r["rule"] for r in spec.get("cleansing_rules") or []]
            if "deduplicate" in rules:
                select_body = select_body.replace("SELECT\n", "SELECT DISTINCT\n", 1)

            # 生成完整 Flink SQL
            flink_sql = generate_flink_sql(
                source_table=flink_input["source_table"],
                target_table=flink_input["target_table"],
                source=FlinkEndpoint(warehouse_conn_id, flink_input["source_platform"]),
                target=FlinkEndpoint(warehouse_conn_id, flink_input["target_platform"]),
                select_body=select_body,
                execution_mode=execution_mode,
                source_physical=flink_input["source_physical"],
                target_physical=flink_input["target_physical"],
            )

            # 提交 Flink 作业（落盘 + 触发 Airflow + 回读 DagRun）
            receipt = run_flink_sql(
                db,
                base=context.get("artifact_id") or target_table,
                tasks=(FlinkSqlTask(task_id="transform", sql=flink_sql),),
                warehouse_conn_id=warehouse_conn_id,
                artifact_id=context.get("artifact_id"),
            )
            return receipt


def _warehouse_conn_id(ds: DataSource) -> str:
    """目标仓的 Airflow Connection id（复用 materialization_runner 逻辑）。"""
    slug = "".join(c if c.isalnum() else "_" for c in (ds.name or ds.id)).strip("_").lower()
    return f"ontometa_ds_{slug or ds.id[:8]}"
