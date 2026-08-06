"""④ 指标任务 Executor。

**ontoMeta 只生成、不执行**——产出建表 DDL 与聚合 SQL，交由 DolphinScheduler
调度执行；与 ``generate_cube_model_files`` 生成 Cube 文件让 Cube 自己加载同理。
保持 ontoMeta 是语义层，不变成又一个调度器。

P1-5 执行路径：Flink on YARN（经 flink_job_runner），未配 SqlRunner JAR 退回「仅产出」。
metric 的 ads 表需先在数仓建（warehouse_ddl），再执行 Flink 聚合。

幂等：产物由 Spec 确定性推导，同一 Spec 反复执行结果逐字节一致。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import DataSource
from app.services.flink_sql_generator import FlinkEndpoint, generate_flink_sql
from app.warehouse import (
    LogicalColumn,
    LogicalTable,
    get_adapter,
)


def _qualified(spec: dict[str, Any]) -> tuple[str, str]:
    layer = spec.get("target_layer") or "ads"
    prefix = spec.get("database_prefix")
    database = f"{layer}_{prefix}" if prefix else layer
    return database, spec["metric_name"]


def _build_table(spec: dict[str, Any]) -> LogicalTable:
    database, name = _qualified(spec)
    columns = [LogicalColumn("stat_date", "date", "datetime", "统计日期")]
    # 维度字段进结果表，指标才可下钻。
    columns += [
        LogicalColumn(dim, "string", "category", dim) for dim in spec.get("group_by") or []
    ]
    columns.append(
        LogicalColumn(
            "metric_value", "decimal", "amount", spec.get("display_name") or name
        )
    )
    return LogicalTable(
        name=name,
        database=database,
        layer=spec.get("target_layer") or "ads",
        comment=f"{spec.get('display_name') or name} · 口径：{spec.get('expression') or '未填写'}",
        columns=tuple(columns),
        partition_key="stat_date",
    )


def _build_sql(spec: dict[str, Any], engine: str) -> str:
    """由绑定角色推导聚合 SQL：dimension→GROUP BY、filter→WHERE、expression→度量。"""
    database, name = _qualified(spec)
    # 主对象缺失时绝不塞占位符——那会产出看似合法、实则无法执行的 `FROM <未绑定>`，
    # 比直接报错更危险（不静默降级）。真实本体的 order_gmv 即无 subject 绑定。
    subjects = spec.get("subject_objects") or spec.get("object_types") or []
    if not subjects:
        raise ValueError(
            f"指标 {name} 未绑定主对象（subject_objects/object_types 均为空），"
            "无法确定聚合 SQL 的 FROM 源表——请先在业务逻辑中为该口径绑定 subject 对象"
        )
    subject = subjects[0]
    group_by = spec.get("group_by") or []
    filters = spec.get("filters") or []
    expression = (spec.get("expression") or "").strip() or "COUNT(1)"

    select_cols = ["  CURRENT_DATE AS stat_date"]
    select_cols += [f"  {c} AS {c}" for c in group_by]
    select_cols.append(f"  {expression} AS metric_value")

    sql = f"INSERT OVERWRITE TABLE {database}.{name}\nSELECT\n" + ",\n".join(select_cols)
    sql += f"\nFROM {subject}"
    if filters:
        sql += "\nWHERE " + " AND ".join(f"{f} IS NOT NULL" for f in filters)
    if group_by:
        sql += "\nGROUP BY " + ", ".join(group_by)
    return get_adapter(engine).translate_sql(sql + ";")


class MetricExecutor(Executor):
    kind = "metric"

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        engine = spec.get("engine") or "hive"
        adapter = get_adapter(engine)
        table = _build_table(spec)
        database, name = _qualified(spec)
        gaps = adapter.guard(table)  # 能力不足直接抛 CapabilityError，不静默降级
        return {
            "engine": engine,
            "target_table": f"{database}.{name}",
            "ddl": adapter.render_create_table(table),
            "sql": _build_sql(spec, engine),
            "warnings": [
                {"feature": g.feature, "detail": g.detail} for g in gaps
            ],
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        return {
            "action": "create_or_replace_metric_table",
            "target_table": artifacts["target_table"],
            "ddl": artifacts["ddl"],
            "sql": artifacts["sql"],
            "warnings": artifacts["warnings"],
            "side_effects": "无（dry-run 仅渲染产物）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 未配 target_datasource，退回「仅产出」（与 transform 同逻辑）
        target_datasource_id = spec.get("target_datasource_id") or context.get("target_datasource_id")
        if not target_datasource_id:
            artifacts = self._artifacts(spec)
            return {
                **artifacts,
                "handoff": "DolphinScheduler",
                "note": "未配置 target_datasource_id，ontoMeta 只生成 DDL+SQL，不执行",
            }

        # Flink on YARN 执行路径（P1-5）
        try:
            from app.services.flink_job_runner import run_flink_sql
            from app.services.airflow_dag_builder import FlinkSqlTask
        except ImportError as exc:
            artifacts = self._artifacts(spec)
            return {
                **artifacts,
                "handoff": "import_error",
                "note": f"Flink 模块导入失败：{exc}，退回仅产出",
            }

        artifacts = self._artifacts(spec)
        engine = artifacts["engine"]
        execution_mode = spec.get("execution_mode") or "batch"
        metric_name = spec.get("metric_name")

        with SessionLocal() as db:
            ds = db.get(DataSource, target_datasource_id)
            if not ds:
                return {
                    **artifacts,
                    "handoff": "datasource_not_found",
                    "note": f"target_datasource_id={target_datasource_id} 不存在，退回仅产出",
                }
            warehouse_conn_id = _warehouse_conn_id(ds)

            # metric 的 ads 表构造：源是 dwd/dws（主对象），目标是 ads（结果表）
            database, name = _qualified(spec)
            target_table = _build_table(spec)
            # 源表：主对象的物化表（假设已物化到 dwd/dws）
            subjects = spec.get("subject_objects") or spec.get("object_types") or []
            if not subjects:
                return {
                    **artifacts,
                    "handoff": "no_subject",
                    "note": "指标未绑定主对象，无法生成 Flink 聚合作业",
                }
            source_entity = subjects[0]  # 主对象的实体名
            # 源表逻辑名加 src_ 前缀，物理名就是主对象的物化表（假设在 dwd）
            source_physical = f"dwd_{spec.get('database_prefix') or ''}.{source_entity}".replace("..", ".")
            source_table = LogicalTable(
                name=f"src_{source_entity}",
                database=None,
                layer="dwd",
                columns=target_table.columns,  # 简化：假设源表列与结果表列一致（实际需按 subject 查）
            )

            # 聚合 SELECT 体：复用 _build_sql 的逻辑，但 FROM 引用 Flink 源表逻辑名
            group_by = spec.get("group_by") or []
            filters = spec.get("filters") or []
            expression = (spec.get("expression") or "").strip() or "COUNT(1)"
            select_cols = ["  CURRENT_DATE AS stat_date"]
            select_cols += [f"  {c} AS {c}" for c in group_by]
            select_cols.append(f"  {expression} AS metric_value")
            select_body = "SELECT\n" + ",\n".join(select_cols) + f"\nFROM `{source_table.name}`"
            if filters:
                select_body += "\nWHERE " + " AND ".join(f"`{f}` IS NOT NULL" for f in filters)
            if group_by:
                select_body += "\nGROUP BY " + ", ".join(f"`{c}`" for c in group_by)

            # 生成完整 Flink SQL
            flink_sql = generate_flink_sql(
                source_table=source_table,
                target_table=target_table,
                source=FlinkEndpoint(warehouse_conn_id, engine),
                target=FlinkEndpoint(warehouse_conn_id, engine),
                select_body=select_body,
                execution_mode=execution_mode,
                source_physical=source_physical,
                target_physical=target_table.qualified_name,
            )

            # 提交 Flink 作业（ads 表 DDL 先在数仓建，再执行聚合）
            receipt = run_flink_sql(
                db,
                base=context.get("artifact_id") or metric_name,
                tasks=(FlinkSqlTask(task_id="aggregate", sql=flink_sql),),
                warehouse_conn_id=warehouse_conn_id,
                warehouse_ddl=(artifacts["ddl"],),  # 先建 ads 表
                artifact_id=context.get("artifact_id"),
            )
            return receipt


def _warehouse_conn_id(ds: DataSource) -> str:
    """目标仓的 Airflow Connection id（复用 materialization_runner 逻辑）。"""
    slug = "".join(c if c.isalnum() else "_" for c in (ds.name or ds.id)).strip("_").lower()
    return f"ontometa_ds_{slug or ds.id[:8]}"
