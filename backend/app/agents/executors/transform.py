"""Doris-native Transform Executor.

Transform reads Doris ODS/upstream semantic tables and writes Doris
DIM/DWD/DWS. It never imports or invokes Flink; Flink is reserved for sync.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import (
    DataSource,
    DorisWarehouseConfig,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.warehouse_generator import WarehouseGenerator
from app.warehouse import LogicalTable, get_adapter
from app.warehouse.policy import require_doris_datasource

_generator = WarehouseGenerator()
Quote = Callable[[str], str]
_APPLIABLE = {"drop_null", "deduplicate"}
_RANK_COLUMN = "__rn"


def _key_columns(table: LogicalTable | None) -> tuple[str, ...]:
    if table is None:
        return ()
    pk = table.primary_key
    if pk and pk.columns:
        return tuple(pk.columns)
    return tuple(c.name for c in table.columns if not c.nullable)


def _drop_null(select_body: str, keys: tuple[str, ...], quote: Quote) -> str:
    predicate = " AND ".join(f"{quote(c)} IS NOT NULL" for c in keys)
    if "\nWHERE " in select_body:
        return f"{select_body} AND {predicate}"
    return f"{select_body}\nWHERE {predicate}"


def _deduplicate(
    select_body: str,
    table: LogicalTable,
    keys: tuple[str, ...],
    quote: Quote,
) -> str:
    q = quote
    cols = ", ".join(q(c.name) for c in table.columns)
    order_col = table.partition_key if table.partition_key else keys[0]
    order_by = (
        f"{q(order_col)} DESC"
        if table.partition_key
        else ", ".join(q(k) for k in keys)
    )
    rank = (
        f"ROW_NUMBER() OVER (PARTITION BY {', '.join(q(k) for k in keys)} "
        f"ORDER BY {order_by}) AS {q(_RANK_COLUMN)}"
    )
    return (
        f"SELECT {cols}\nFROM (\n"
        f"  SELECT {cols}, {rank}\n  FROM (\n{select_body}\n  ) {q('__src')}\n"
        f") {q('__ranked')}\nWHERE {q(_RANK_COLUMN)} = 1"
    )


def _apply_rules(
    select_body: str,
    rules: list[str],
    table: LogicalTable | None,
    quote: Quote,
) -> tuple[str, list[str], list[str], list[dict[str, str]]]:
    """Apply the deterministic cleansing rule closed set."""
    keys = _key_columns(table)
    applied: list[str] = []
    unapplied: list[str] = []
    notes: list[dict[str, str]] = []
    for rule in rules:
        if rule not in _APPLIABLE:
            unapplied.append(rule)
        elif rule == "drop_null" and not keys:
            unapplied.append(rule)
            notes.append({
                "rule": rule,
                "detail": "本体未声明主键、也无必填字段，说不出该滤哪几列，规则未应用",
            })
        else:
            applied.append(rule)
    if "drop_null" in applied:
        select_body = _drop_null(select_body, keys, quote)
        notes.append({"rule": "drop_null", "detail": "过滤关键字段为空的行：" + "、".join(keys)})
    if "deduplicate" in applied:
        if keys and table is not None:
            select_body = _deduplicate(select_body, table, keys, quote)
            detail = "按 " + "、".join(keys) + " 去重" + (
                f"，同键取 {table.partition_key} 最大的一行"
                if table.partition_key else ""
            )
        else:
            select_body = select_body.replace("SELECT\n", "SELECT DISTINCT\n", 1)
            detail = "本体未声明主键，退回整行去重（含审计列的源表可能去不掉任何行）"
        notes.append({"rule": "deduplicate", "detail": detail})
    return select_body, applied, unapplied, notes


def _doris_conn_id(config: DorisWarehouseConfig | None, ds: DataSource) -> str:
    if config and config.airflow_etl_conn_id:
        return config.airflow_etl_conn_id
    token = "".join(c for c in ds.id.lower() if c.isalnum())[:12]
    return f"ontometa_doris_{token}_etl"


class TransformExecutor(Executor):
    kind = "transform"

    @staticmethod
    def _logical_table(db, ontology_id: str, target: str, prefix: str | None) -> LogicalTable:
        plan = _generator.build_logical_schema(
            db, ontology_id, database_prefix=prefix
        )
        table = next(
            (t for t in plan.schema.tables if t.source_name == target or t.name == target),
            None,
        )
        if table is None:
            raise ValueError(f"目标表 {target} 无法从本体/物化契约解析")
        return table

    @staticmethod
    def _ods_source(
        db,
        ontology: Ontology,
        obj: ObjectType,
        datasource_id: str | None,
        prefix: str | None,
    ) -> str:
        if datasource_id:
            deployment = (
                db.query(OntologyWarehouseDeployment)
                .filter(
                    OntologyWarehouseDeployment.ontology_id == ontology.id,
                    OntologyWarehouseDeployment.ontology_version == ontology.version,
                    OntologyWarehouseDeployment.doris_datasource_id == datasource_id,
                )
                .first()
            )
            if deployment:
                projection = (
                    db.query(WarehouseObjectProjection)
                    .filter(
                        WarehouseObjectProjection.deployment_id == deployment.id,
                        WarehouseObjectProjection.object_type_id == obj.id,
                    )
                    .first()
                )
                if projection and projection.ods_database and projection.ods_table:
                    if projection.sync_status != "ready":
                        raise ValueError(
                            f"对象 {obj.name} 的 ODS 尚未同步完成（sync_status={projection.sync_status}）"
                        )
                    return f"{projection.ods_database}.{projection.ods_table}"
            raise ValueError("当前本体版本没有可用的 Doris Deployment/ODS Projection")
        database = f"ods_{prefix}" if prefix else "ods"
        from app.services.ods_naming import target_ods_table_name
        from app.services.source_ref import has_physical_source

        table = (
            target_ods_table_name(db, ontology.id, obj)
            if has_physical_source(obj.source_ref)
            else obj.name
        )
        return f"{database}.{table}"

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        ontology_id = spec.get("ontology_id")
        target = str(spec.get("target_table") or "")
        if not ontology_id or not target:
            raise ValueError("Transform Spec 缺少 ontology_id/target_table")
        if str(spec.get("engine") or "doris").lower() != "doris":
            raise ValueError("新 transform 只允许 Doris SQL")
        prefix = spec.get("database_prefix")
        datasource_id = spec.get("target_datasource_id")
        adapter = get_adapter("doris")
        with SessionLocal() as db:
            ontology = db.get(Ontology, ontology_id)
            if ontology is None:
                raise ValueError("本体不存在")
            obj = (
                db.query(ObjectType)
                .filter(ObjectType.ontology_id == ontology.id, ObjectType.name == target)
                .first()
            )
            if obj is None:
                raise ValueError(f"目标对象 {target} 不在本体中")
            table = self._logical_table(db, ontology.id, target, prefix)
            source = self._ods_source(db, ontology, obj, datasource_id, prefix)

        q = adapter.quote_identifier
        select_body = (
            "SELECT\n"
            + ",\n".join(f"  {q(c.name)}" for c in table.columns)
            + f"\nFROM {adapter.quote_table_ref(source)}"
        )
        rules = [str(r.get("rule")) for r in spec.get("cleansing_rules") or []]
        select_body, applied, unapplied, notes = _apply_rules(
            select_body, rules, table, adapter.quote_identifier
        )
        target_physical = table.qualified_name
        sql = f"INSERT OVERWRITE TABLE {adapter.quote_table_ref(target_physical)}\n{select_body};"
        if applied:
            sql = "\n".join(f"-- 清洗规则：{rule}" for rule in applied) + "\n" + sql
        return {
            "engine": "doris",
            "compute_engine": "doris",
            "source_tables": [source],
            "target_table": target_physical,
            "target_logical_table": table,
            "key_columns": list(_key_columns(table)),
            "select_sql": select_body,
            "sql": sql,
            "applied_rules": applied,
            "unapplied_rules": unapplied,
            "rule_notes": notes,
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        return {
            "action": "doris_native_transform",
            "compute_engine": "doris",
            "source_tables": artifacts["source_tables"],
            "target_table": artifacts["target_table"],
            "sql": artifacts["sql"],
            "applied_rules": artifacts["applied_rules"],
            "unapplied_rules": artifacts["unapplied_rules"],
            "side_effects": "无（dry-run 与执行使用同一份 Doris SELECT）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        datasource_id = spec.get("target_datasource_id") or context.get("target_datasource_id")
        if not datasource_id:
            return {
                **{k: v for k, v in artifacts.items() if k != "target_logical_table"},
                "execute_mode": "handoff",
                "note": "未配置默认 Doris DataSource，只产出 Doris SQL",
            }
        artifact_id = str(context.get("artifact_id") or "manual")
        adapter = get_adapter("doris")
        table = artifacts["target_logical_table"]
        staging_name = adapter.staging_table_name(table, artifact_id)
        staging = adapter._qual(table.database, staging_name)
        target = adapter._qual(table.database, table.name)
        setup = [
            f"DROP TABLE IF EXISTS {staging};",
            adapter.render_create_staging(table, artifact_id),
        ]
        execute_sql = [f"INSERT INTO {staging}\n{artifacts['select_sql']};"]
        keys = artifacts["key_columns"]
        if keys:
            not_null = " AND ".join(
                f"COUNT(*) = COUNT({adapter.quote_identifier(key)})" for key in keys
            )
            distinct = ", ".join(adapter.quote_identifier(key) for key in keys)
            quality_sql = [
                f"SELECT ({not_null}) AND "
                f"(COUNT(*) = COUNT(DISTINCT {distinct})) FROM {staging};"
            ]
        else:
            quality_sql = [f"SELECT COUNT(*) >= 0 FROM {staging};"]
        publish_sql = adapter.render_swap(table, artifact_id)

        with SessionLocal() as db:
            ds = db.get(DataSource, datasource_id)
            require_doris_datasource(ds, operation="Doris Transform")
            if not ds.is_default_warehouse:
                raise ValueError("Transform 只能使用默认 Doris")
            config = (
                db.query(DorisWarehouseConfig)
                .filter(DorisWarehouseConfig.warehouse_datasource_id == ds.id)
                .first()
            )
            from app.services.doris_job_runner import run_doris_sql

            receipt = run_doris_sql(
                db,
                artifact_id=artifact_id,
                kind="transform",
                conn_id=_doris_conn_id(config, ds),
                execute_sql=execute_sql,
                setup_sql=setup,
                quality_sql=quality_sql,
                publish_sql=publish_sql,
                schedule=spec.get("schedule") or spec.get("refresh_cron"),
                source_tables=artifacts["source_tables"],
                target_tables=[artifacts["target_table"]],
            )
            # Submission does not imply queryability. Final Airflow success must
            # reconcile this projection before the Query Gateway can use it.
            ontology = db.get(Ontology, spec["ontology_id"])
            obj = (
                db.query(ObjectType)
                .filter(
                    ObjectType.ontology_id == ontology.id,
                    ObjectType.name == spec["target_table"],
                )
                .first()
            )
            deployment = (
                db.query(OntologyWarehouseDeployment)
                .filter(
                    OntologyWarehouseDeployment.ontology_id == ontology.id,
                    OntologyWarehouseDeployment.ontology_version == ontology.version,
                    OntologyWarehouseDeployment.doris_datasource_id == ds.id,
                )
                .first()
            )
            if deployment and obj:
                projection = (
                    db.query(WarehouseObjectProjection)
                    .filter(
                        WarehouseObjectProjection.deployment_id == deployment.id,
                        WarehouseObjectProjection.object_type_id == obj.id,
                    )
                    .first()
                )
                if projection:
                    projection.transform_status = "running" if receipt.get("ok") else "failed"
                    projection.queryable = False
                    db.commit()
            return {
                **receipt,
                "datasource_id": ds.id,
                "ontology_id": ontology.id,
                "ontology_version": ontology.version,
                "object_type_id": obj.id if obj else None,
            }
