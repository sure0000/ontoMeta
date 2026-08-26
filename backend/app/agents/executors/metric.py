"""Doris-native metric/tag/rule executor.

BusinessLogic AST is compiled once by MetricCompiler(dialect="doris"), then
published to Doris ADS through the same SQL DAG/staging/quality/swap boundary as
transform. This module never imports or invokes Flink.
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DataSource,
    DorisWarehouseConfig,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)
from app.services.metric_compiler import (
    LOGIC_TYPES,
    TAG_VALUE_COLUMN,
    compile_metric,
    result_column_specs,
    value_source_column,
)
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


# 「指标任务」这条链原本只按 metric 一种形状生成结果表与取数列，但编译器对三类口径
# 产出的列**并不相同**（见 metric_compiler 的 §1 分叉）：
#
#   metric → 度量列，别名 = 口径技术名（agg_alias=logic.name）
#   tag    → 分桶列，别名 = 口径技术名（**字符串标签**）；外加计数列 row_count
#   rule   → 计数列 violations；**没有**以口径名命名的列
#
# 一律取 `logic.name AS metric_value` 的后果：
#   · tag：字符串标签被塞进 decimal 的 metric_value，而真正要的「每个标签值下多少实体」
#     （row_count）被整列丢掉——跑得通，数是错的，最难发现的那种。
#   · rule：引用了子查询里根本不存在的列，SQL 一执行就报 column not found。
# 故结果表形状与取数列都必须按口径类型分叉——形状的唯一权威在 metric_compiler
# （物化那条路的 warehouse_generator._logic_tables 读的是同一份），这里不另立一套。


def _spec_logic_type(spec: dict[str, Any]) -> str:
    """本 Spec 的口径类型。未知一律当 metric——旧 Spec 没这个字段，行为不变。"""
    lt = str(spec.get("logic_type") or "metric").strip().lower()
    return lt if lt in LOGIC_TYPES else "metric"


def _build_table(spec: dict[str, Any]) -> LogicalTable:
    database, name = _qualified(spec)
    logic_type = _spec_logic_type(spec)
    display = spec.get("display_name") or name
    columns = [LogicalColumn("stat_date", "date", "datetime", "统计日期")]
    # 维度字段进结果表，指标才可下钻。
    columns += [
        LogicalColumn(dim, "string", "category", dim) for dim in spec.get("group_by") or []
    ]
    columns += [
        LogicalColumn(cname, dtype, stype, comment)
        for cname, dtype, stype, comment in result_column_specs(logic_type, display)
    ]
    return LogicalTable(
        name=name,
        database=database,
        layer=spec.get("target_layer") or "ads",
        comment=f"{display} · 口径：{spec.get('expression') or '未填写'}",
        columns=tuple(columns),
        partition_key="stat_date",
    )


def _build_sql(spec: dict[str, Any], engine: str) -> str:
    """由绑定角色推导聚合 SQL：dimension→GROUP BY、filter→WHERE、expression→度量。

    **只在口径没有形式化时走这条路**：``expression`` 是给人看的口径摘要（真实数据里
    常常是「SUM(订单.金额)」这种中文显示名），原样拼进 SQL 得到的是 `SUM(订单.金额)`
    ——列名对不上、``order`` 这类保留字也没引号，跑不了。已形式化的口径走
    :func:`_compiled_sql`，由 metric_compiler 从 AST 确定性编译。
    """
    database, name = _qualified(spec)
    logic_type = _spec_logic_type(spec)
    if logic_type in ("tag", "rule"):
        # 标签/规则的口径**只存在于表达式里**：标签是 CASE 分桶、规则是违规谓词，
        # 二者都无法从「绑定角色 + 口径摘要」拼出来——这条兜底路只会拼出一条与该口径
        # 毫无关系的聚合。宁可在起草时报错，也不产出一张跑得通、数无意义的表。
        label = "标签" if logic_type == "tag" else "规则"
        raise ValueError(
            f"{label}「{spec.get('display_name') or name}」尚未形式化（没有表达式 AST），"
            f"无法生成任务——请先在业务逻辑里补全该{label}的表达式并发布，再建任务。"
        )
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

    select_body = "SELECT\n" + ",\n".join(select_cols) + f"\nFROM {subject}"
    if filters:
        select_body += "\nWHERE " + " AND ".join(f"{f} IS NOT NULL" for f in filters)
    if group_by:
        select_body += "\nGROUP BY " + ", ".join(group_by)
    adapter = get_adapter(engine)
    # 装载语句写法归 Adapter（postgres 没有 INSERT OVERWRITE）。
    return adapter.translate_sql(
        adapter.render_load(f"{database}.{name}", select_body, overwrite=True)
    )


def _compiled_sql(spec: dict[str, Any], engine: str) -> str | None:
    """已形式化的口径 → 用 ``metric_compiler`` 编译聚合 SQL。没形式化返回 None。

    口径的权威表达是 ``expression_json``（AST），编译器据它生成 SQL 并过语义证明；
    指标物化没有理由另拼一份字符串。这里只负责把编译出的 SELECT 套进结果表的形状
    （stat_date + 维度 + metric_value），聚合逻辑本身一行都不重写。

    编译失败**不静默退回**字符串拼接：那正是「看着成功、跑起来报错」的来源。
    错误原样抛出，由校验闸门当 dry_run_error 呈现。
    """
    logic_id = spec.get("business_logic_id")
    if not logic_id:
        return None
    adapter = get_adapter(engine)
    q = adapter.quote_identifier
    database, name = _qualified(spec)
    with SessionLocal() as db:
        logic = db.get(BusinessLogic, logic_id)
        if logic is None or not logic.expression_json:
            return None  # 只有文字口径 → 退回 _build_sql 的既有行为
        compiled = compile_metric(db, logic_id, limit=None, dialect=engine)

    # 结果表形状：统计日 + 维度 + [标签取值] + 值列。按**编译产物自报的口径类型**分叉
    # （compiled.logic_type 来自 AST，与编译器实际产出的列名同源；用 spec 里的类型可能
    # 与 AST 不一致，DDL 与 SQL 就会各说各话）。
    select_cols = ["  CURRENT_DATE AS stat_date"]
    select_cols += [f"  {q(c)} AS {q(c)}" for c in spec.get("group_by") or []]
    if compiled.logic_type == "tag":
        select_cols.append(f"  {q(compiled.logic_name)} AS {q(TAG_VALUE_COLUMN)}")
    select_cols.append(
        f"  {q(value_source_column(compiled.logic_type, compiled.logic_name))} AS metric_value"
    )
    select_body = (
        "SELECT\n" + ",\n".join(select_cols)
        + f"\nFROM (\n{compiled.sql}\n) {q('metric_src')}"
    )
    return adapter.translate_sql(
        adapter.render_load(f"{database}.{name}", select_body, overwrite=True)
    )


class MetricExecutor(Executor):
    kind = "metric"

    @staticmethod
    def _subject_table(db, spec: dict[str, Any], entity: str) -> LogicalTable | None:
        """主对象实际物化到的那张表（层/库/表名/列全部来自本体+契约）。

        指标 Spec 里没有 ontology_id（口径本身带），故从 BusinessLogic 反查。
        """
        logic_id = spec.get("business_logic_id")
        logic = db.get(BusinessLogic, logic_id) if logic_id else None
        if logic is None:
            return None
        from app.services.warehouse_generator import WarehouseGenerator

        plan = WarehouseGenerator().build_logical_schema(
            db, logic.ontology_id, database_prefix=spec.get("database_prefix")
        )
        return next(
            (t for t in plan.schema.tables if t.source_name == entity), None
        )

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        engine = str(spec.get("engine") or "doris").lower()
        if engine != "doris":
            raise ValueError("新 metric/tag/rule 任务只允许 Doris SQL")
        adapter = get_adapter(engine)
        table = _build_table(spec)
        database, name = _qualified(spec)
        gaps = adapter.guard(table)  # 能力不足直接抛 CapabilityError，不静默降级
        return {
            "engine": engine,
            "target_table": f"{database}.{name}",
            "ddl": adapter.render_create_table(table),
            "sql": _compiled_sql(spec, engine) or _build_sql(spec, engine),
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

    @staticmethod
    def _deployment_mapping(db, spec: dict[str, Any], ds: DataSource) -> tuple[dict, OntologyWarehouseDeployment]:
        logic = db.get(BusinessLogic, spec.get("business_logic_id"))
        if logic is None:
            raise ValueError("业务逻辑不存在")
        ontology = db.get(Ontology, logic.ontology_id)
        if ontology is None or ontology.status != "published":
            raise ValueError("Metric 只能执行当前已发布本体的业务逻辑")
        deployment = (
            db.query(OntologyWarehouseDeployment)
            .filter(
                OntologyWarehouseDeployment.ontology_id == logic.ontology_id,
                OntologyWarehouseDeployment.ontology_version == ontology.version,
                OntologyWarehouseDeployment.doris_datasource_id == ds.id,
                OntologyWarehouseDeployment.status.in_(("schema_ready", "ready")),
            )
            .first()
        )
        if deployment is None:
            raise ValueError("当前业务逻辑没有可用的 Doris Deployment")
        projections = (
            db.query(WarehouseObjectProjection)
            .filter(WarehouseObjectProjection.deployment_id == deployment.id)
            .all()
        )
        objects = {
            row.id: row
            for row in db.query(ObjectType).filter(
                ObjectType.ontology_id == logic.ontology_id
            ).all()
        }
        tables: dict[str, str] = {}
        columns: dict[str, str] = {}
        import json
        for projection in projections:
            obj = objects.get(projection.object_type_id)
            if obj is None or not projection.queryable:
                continue
            if not projection.serving_database or not projection.serving_table:
                continue
            tables[obj.name] = f"{projection.serving_database}.{projection.serving_table}"
            try:
                mapping = json.loads(projection.column_mapping_json or "{}")
            except (TypeError, ValueError):
                mapping = {}
            columns.update(mapping if isinstance(mapping, dict) else {})
        required = set(spec.get("subject_objects") or spec.get("object_types") or [])
        missing = sorted(required - set(tables))
        if missing:
            raise ValueError(
                "指标上游 Projection 尚未 ready/queryable：" + ", ".join(missing)
            )
        return {"tables": tables, "columns": columns}, deployment

    @staticmethod
    def _physical_sql(sql: str, mapping: dict) -> str:
        from app.services.data_app_executor import _apply_mapping

        return _apply_mapping(sql, mapping)

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        datasource_id = spec.get("target_datasource_id") or context.get("target_datasource_id")
        if not datasource_id:
            return {
                **artifacts,
                "execute_mode": "handoff",
                "compute_engine": "doris",
                "note": "未配置默认 Doris DataSource，只产出 Doris DDL+SQL",
            }
        artifact_id = str(context.get("artifact_id") or "manual")
        adapter = get_adapter("doris")
        table = _build_table(spec)
        staging_name = adapter.staging_table_name(table, artifact_id)
        staging = adapter._qual(table.database, staging_name)

        with SessionLocal() as db:
            ds = db.get(DataSource, datasource_id)
            from app.warehouse.policy import require_doris_datasource
            require_doris_datasource(ds, operation="Doris Metric")
            if not ds.is_default_warehouse:
                raise ValueError("Metric 只能使用默认 Doris")
            mapping, deployment = self._deployment_mapping(db, spec, ds)
            logic = db.get(BusinessLogic, spec.get("business_logic_id"))
            compiled = compile_metric(
                db,
                logic.id,
                dimensions=spec.get("group_by") or (),
                filters=(),
                limit=None,
                dialect="doris",
                mapping=mapping,
            )
            # Use the same result-shape wrapper as DDL generation, then replace
            # semantic table identifiers with ready Doris serving projections.
            wrapped = _compiled_sql(spec, "doris")
            if wrapped is None:
                raise ValueError("Metric 必须有已发布的形式化 expression_json")
            marker = wrapped.find("SELECT\n")
            if marker < 0:
                raise ValueError("MetricCompiler 未生成 SELECT")
            select_sql = self._physical_sql(
                wrapped[marker:].rstrip().rstrip(";"), mapping
            )
            config = (
                db.query(DorisWarehouseConfig)
                .filter(DorisWarehouseConfig.warehouse_datasource_id == ds.id)
                .first()
            )
            token = "".join(c for c in ds.id.lower() if c.isalnum())[:12]
            conn_id = (
                config.airflow_etl_conn_id
                if config and config.airflow_etl_conn_id
                else f"ontometa_doris_{token}_etl"
            )
            # 真要落库的那条 DDL 按目标实例的实测 BE 数重渲染：``artifacts["ddl"]`` 是
            # 给 dry-run 看的（那时还没定下目标仓），而 Doris 建表的副本数不能超过存活
            # BE 数，否则单 BE 的实例上这条 ADS 建表必被 FE 拒。
            from app.services.materialization_runner import target_storage_nodes
            create_ads = get_adapter("doris").for_storage_nodes(
                target_storage_nodes(ds)
            ).render_create_table(table)
            setup = [
                create_ads,
                f"DROP TABLE IF EXISTS {staging};",
                adapter.render_create_staging(table, artifact_id),
            ]
            execute_sql = [f"INSERT INTO {staging}\n{select_sql};"]
            quality_sql = [f"SELECT COUNT(*) >= 0 FROM {staging};"]
            publish_sql = adapter.render_swap(table, artifact_id)
            from app.services.doris_job_runner import run_doris_sql
            receipt = run_doris_sql(
                db,
                artifact_id=artifact_id,
                kind="metric",
                conn_id=conn_id,
                setup_sql=setup,
                execute_sql=execute_sql,
                quality_sql=quality_sql,
                publish_sql=publish_sql,
                schedule=spec.get("schedule") or spec.get("refresh_cron"),
                source_tables=sorted(mapping["tables"].values()),
                target_tables=[artifacts["target_table"]],
            )
            projection = (
                db.query(WarehouseLogicProjection)
                .filter(
                    WarehouseLogicProjection.deployment_id == deployment.id,
                    WarehouseLogicProjection.business_logic_id == logic.id,
                )
                .first()
            )
            if projection is None:
                projection = WarehouseLogicProjection(
                    deployment_id=deployment.id,
                    business_logic_id=logic.id,
                    serving_database=table.database or "ads",
                    serving_table=table.name,
                )
                db.add(projection)
            projection.status = "running" if receipt.get("ok") else "failed"
            projection.queryable = False
            db.commit()
            return {
                **receipt,
                "ontology_id": logic.ontology_id,
                "ontology_version": deployment.ontology_version,
                "datasource_id": ds.id,
                "business_logic_id": logic.id,
                "logic_projection_id": projection.id,
                "logic_type": compiled.logic_type,
            }
