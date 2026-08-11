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
from app.models import BusinessLogic, DataSource
from app.services.flink_sql_generator import FlinkEndpoint, generate_flink_sql
from app.services.metric_compiler import compile_metric
from app.warehouse.jobs.base import endpoint_credential_env
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
    """由绑定角色推导聚合 SQL：dimension→GROUP BY、filter→WHERE、expression→度量。

    **只在口径没有形式化时走这条路**：``expression`` 是给人看的口径摘要（真实数据里
    常常是「SUM(订单.金额)」这种中文显示名），原样拼进 SQL 得到的是 `SUM(订单.金额)`
    ——列名对不上、``order`` 这类保留字也没引号，跑不了。已形式化的口径走
    :func:`_compiled_sql`，由 metric_compiler 从 AST 确定性编译。
    """
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

    # 结果表形状固定：统计日 + 维度 + 度量值。维度列名即口径里的属性名，
    # 度量列在编译产物里的别名是口径技术名（agg_alias=logic.name）。
    select_cols = ["  CURRENT_DATE AS stat_date"]
    select_cols += [f"  {q(c)} AS {q(c)}" for c in spec.get("group_by") or []]
    select_cols.append(f"  {q(compiled.logic_name)} AS metric_value")
    select_body = (
        "SELECT\n" + ",\n".join(select_cols)
        + f"\nFROM (\n{compiled.sql}\n) {q('metric_src')}"
    )
    return adapter.translate_sql(
        adapter.render_load(f"{database}.{name}", select_body, overwrite=True)
    )


def _compiled_select_body(
    spec: dict[str, Any], subject: str, group_by: list[str]
) -> str | None:
    """形式化口径 → Flink 用的 SELECT 体（结果表形状：统计日 + 维度 + 度量值）。

    没有形式化口径返回 None，由调用方退回旧的字符串拼接。

    **按 hive 方言渲染**：Flink SQL 的标识符引号是反引号，与 hive 同规则；用目标引擎
    （如 postgres）的方言渲染会得到双引号，Flink 解析不了。
    """
    logic_id = spec.get("business_logic_id")
    if not logic_id:
        return None
    with SessionLocal() as db:
        logic = db.get(BusinessLogic, logic_id)
        if logic is None or not logic.expression_json:
            return None
        compiled = compile_metric(db, logic_id, limit=None, dialect="hive")
    cols = ["  CURRENT_DATE AS stat_date"]
    cols += [f"  `{c}` AS `{c}`" for c in group_by]
    cols.append(f"  `{compiled.logic_name}` AS metric_value")
    return (
        "SELECT\n" + ",\n".join(cols)
        + f"\nFROM (\n{compiled.sql}\n) `metric_src`"
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
        engine = spec.get("engine") or "hive"
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
            # 源表 = 主对象**实际物化到的那张表**，按本体+契约解析，不猜层也不猜库名。
            # 此前这里拼的是 f"dwd_{prefix}.{entity}"：prefix 为空就得到 `dwd_.brand`
            # 这种根本不存在的库名，且写死 dwd 层——而对象可能物化在 dim/dws。
            # 列也照抄了结果表（stat_date/metric_value），与源表毫无关系。
            subject_table = self._subject_table(db, spec, source_entity)
            if subject_table is None:
                return {
                    **artifacts,
                    "handoff": "subject_not_materialized",
                    "note": f"主对象 {source_entity} 没有物化表（本体/契约里查不到），"
                    "无法生成 Flink 聚合作业——请先物化该对象",
                }
            source_physical = subject_table.qualified_name
            # Flink 逻辑表名 = **主对象名**：编译器产的 SQL 按本体对象名引用表
            # （`FROM \`brand\``），逻辑名叫 src_brand 就对不上。metric 的目标表是指标名，
            # 与对象名不会撞，故这里不需要 src_ 前缀（transform 才需要，那边源实体与
            # 目标表同名）。
            source_table = LogicalTable(
                name=source_entity,
                database=None,  # Flink 逻辑表无库名，物理名单独给
                layer=subject_table.layer,
                columns=subject_table.columns,
            )

            # 聚合 SELECT 体：**优先用形式化口径编译**，与 dry_run 同一个来源。
            # 此前这里另拼一份、直接把 expression（中文口径摘要 "COUNT(品牌.排序号)"）
            # 塞进 SQL——Flink 报 `Table '品牌' not found`，而 dry_run 看到的却是编译好的
            # 正确 SQL：界面上是对的，跑起来是错的。
            group_by = spec.get("group_by") or []
            filters = spec.get("filters") or []
            select_body = _compiled_select_body(spec, source_entity, group_by)
            if select_body is None:
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

            # L1 血缘：源 = 主对象物化表，目标 = ads 结果表 → inlets/outlets URN。
            from app.services.flink_sql_lineage import task_lineage_urns

            source_urns, target_urn = task_lineage_urns(
                sql=flink_sql,
                source_tables=[source_physical],
                target_table=target_table.qualified_name,
                source_platform=engine,
                target_platform=engine,
            )

            # 提交 Flink 作业（ads 表 DDL 先在数仓建，再执行聚合）
            receipt = run_flink_sql(
                db,
                base=context.get("artifact_id") or metric_name,
                tasks=(
                    FlinkSqlTask(
                        task_id="aggregate",
                        sql=flink_sql,
                        # 见 transform 同处：占位符的运行期取值表。metric 两端同为数仓，
                        # 但仍要显式给——不给就没有任何凭据注入。
                        env=endpoint_credential_env(warehouse_conn_id, engine),
                        # L1 血缘：inlets/outlets
                        source_urns=source_urns,
                        target_urn=target_urn,
                    ),
                ),
                warehouse_conn_id=warehouse_conn_id,
                warehouse_ddl=(artifacts["ddl"],),  # 先建 ads 表
                artifact_id=context.get("artifact_id"),
            )
            return receipt


def _warehouse_conn_id(ds: DataSource) -> str:
    """目标仓的 Airflow Connection id（复用 materialization_runner 逻辑）。"""
    slug = "".join(c if c.isalnum() else "_" for c in (ds.name or ds.id)).strip("_").lower()
    return f"ontometa_ds_{slug or ds.id[:8]}"
