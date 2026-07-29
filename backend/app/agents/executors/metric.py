"""④ 指标任务 Executor。

**ontoMeta 只生成、不执行**——产出建表 DDL 与聚合 SQL，交由 DolphinScheduler
调度执行；与 ``generate_cube_model_files`` 生成 Cube 文件让 Cube 自己加载同理。
保持 ontoMeta 是语义层，不变成又一个调度器。

幂等：产物由 Spec 确定性推导，同一 Spec 反复执行结果逐字节一致。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
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
        artifacts = self._artifacts(spec)
        return {
            **artifacts,
            "handoff": "DolphinScheduler",
            "note": "ontoMeta 只生成产物，不直接执行；请由调度器加载后运行",
        }
