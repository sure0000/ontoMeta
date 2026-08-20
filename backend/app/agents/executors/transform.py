"""③ ETL 任务 Executor。

字段映射 SQL **直接复用 M3 的 ``warehouse_generator``**，不重写一份——
两份源→目标层的映射逻辑必然分叉。本执行器只在其产出之上叠加清洗规则。

P1-4 执行路径：Flink on YARN（经 flink_job_runner），未配 SqlRunner JAR 退回「仅产出」。

**跨库由 Flink 承担，不假设源已经在数仓里**：源表按源库的连接器声明（``erp_readonly``
→ MariaDB），目标表按数仓的连接器声明（``ontometa_ds_pg`` → postgres），Flink 同时连
两边、把数据搬过去。此前两个端点都写死成 ``warehouse_conn_id``，背后是「sync 已把源
搬进数仓 ODS 层」这条假设——而生成的 SQL 明明 ``FROM 原始源表``，数仓根本看不见它，
两者对不上。数据搬运一律走传输工具（Flink / SeaTunnel / runner），不靠数仓内 SQL 直连。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.models import DataSource
from app.services import flink_params
from app.services.job_planner import DEFAULT_SOURCE_ALIAS
from app.services.flink_sql_generator import (
    FlinkEndpoint,
    generate_flink_sql,
    quote_identifier as flink_quote,
)
from app.services.warehouse_generator import WarehouseGenerator
from app.warehouse import LogicalTable, get_adapter
from app.warehouse.jobs.base import endpoint_credential_env

_generator = WarehouseGenerator()

# 标识符引用函数。谁给这个函数决定了产物是哪种 SQL：数仓语句用目标引擎 Adapter 的规则，
# Flink 语句用 Flink 自己的（反引号，与目标引擎无关）——两者不能混。
Quote = Callable[[str], str]

# 清洗规则 → 叠加在生成 SQL 之上的 SQL 片段。
# 只覆盖能确定性表达的规则；其余留在 unapplied 里交人处理，不硬凑。
#
# **进这个集合的规则必须真的改写 SQL**：``drop_null`` 曾只挂在这里而没有任何实现，
# 于是回执写着「已应用 drop_null」、SQL 里连一个 WHERE 都没有——比不做更糟，因为
# 人会据此以为空值已经滤掉了。规则应用不了就进 unapplied，不进这里。
_APPLIABLE = {"drop_null", "deduplicate"}

# 去重用的排名列名。带双下划线前缀，避免与本体属性撞名。
_RANK_COLUMN = "__rn"


def _key_columns(table: LogicalTable | None) -> tuple[str, ...]:
    """「关键字段」= 主键列；本体没声明身份属性时退回非空列。

    两条清洗规则都以此为准：``deduplicate`` 按它分组取一行，``drop_null`` 过滤它为空的行。
    两者都拿不到关键字段时该规则无法确定性表达，进 unapplied 交人处理。
    """
    if table is None:
        return ()
    pk = table.primary_key
    if pk and pk.columns:
        return tuple(pk.columns)
    return tuple(c.name for c in table.columns if not c.nullable)


def _drop_null(select_body: str, keys: tuple[str, ...], quote: Quote) -> str:
    """关键字段非空过滤。已有 WHERE（增量水位谓词）则并入，不另起一个。"""
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
    """按主键去重：``ROW_NUMBER() OVER (PARTITION BY 主键 ORDER BY …) = 1``。

    此前是整行 ``SELECT DISTINCT``——对带 ``modified`` / ``_assign`` 这类审计列的源表
    （Frappe 全系如此）一行都去不掉，规则说的「按主键去重」名不副实。

    留哪一行：有分区键（通常是 ``modified``）就取最新一条，否则按主键排序取定值——
    没有时间列时「哪条更新」本就无从判断，至少保证同一份输入产出同一个结果。
    包一层子查询而不在原 SELECT 里插列：原 SELECT 由 M3 生成器产出，不在这里改写它的内部。
    """
    q = quote
    cols = ", ".join(q(c.name) for c in table.columns)
    order_col = table.partition_key if table.partition_key else keys[0]
    order_by = f"{q(order_col)} DESC" if table.partition_key else ", ".join(q(k) for k in keys)
    rank = (
        f"ROW_NUMBER() OVER (PARTITION BY {', '.join(q(k) for k in keys)} "
        f"ORDER BY {order_by}) AS {q(_RANK_COLUMN)}"
    )
    return (
        f"SELECT {cols}\nFROM (\n"
        f"  SELECT {cols}, {rank}\n  FROM (\n{select_body}\n  ) {q('__src')}\n"
        f") {q('__ranked')}\nWHERE {q(_RANK_COLUMN)} = 1"
    )


def _split_statement(sql: str) -> tuple[str, str]:
    """``INSERT … TABLE x\\nSELECT …;`` → (装载头, SELECT 体)。找不到 SELECT 则整条当体。"""
    idx = sql.find("SELECT\n")
    if idx < 0:
        return "", sql.rstrip().rstrip(";")
    return sql[:idx], sql[idx:].rstrip().rstrip(";")


def _apply_rules(
    select_body: str,
    rules: list[str],
    table: LogicalTable | None,
    quote: Quote,
) -> tuple[str, list[str], list[str], list[dict[str, str]]]:
    """把清洗规则叠加到 SELECT 体上。返回 (新 SELECT 体, applied, unapplied, 说明)。

    dry_run 与 execute 走同一个函数：两处各写一份，界面上看到的 SQL 与真正跑的
    SQL 迟早不是同一条。
    """
    keys = _key_columns(table)
    applied: list[str] = []
    unapplied: list[str] = []
    notes: list[dict[str, str]] = []
    for rule in rules:
        if rule not in _APPLIABLE:
            unapplied.append(rule)
        # 滤空必须说得出滤哪几列：本体既无主键也无必填字段时，这条规则无法确定性表达，
        # 归 unapplied 交人处理——绝不假装应用了（回执写 applied 而 SQL 没 WHERE 是最坏的一种）。
        elif rule == "drop_null" and not keys:
            unapplied.append(rule)
            notes.append({
                "rule": rule,
                "detail": "本体未声明主键、也无必填字段，说不出该滤哪几列，规则未应用",
            })
        else:
            applied.append(rule)
    # 顺序固定：先滤空（少扫一遍行），再去重（去重要包子查询，须在最外层）。
    if "drop_null" in applied:
        select_body = _drop_null(select_body, keys, quote)
        notes.append({
            "rule": "drop_null",
            "detail": "过滤关键字段为空的行：" + "、".join(keys),
        })
    if "deduplicate" in applied:
        if keys and table is not None:
            select_body = _deduplicate(select_body, table, keys, quote)
            detail = "按 " + "、".join(keys) + " 去重" + (
                f"，同键取 {table.partition_key} 最大的一行" if table.partition_key else ""
            )
        else:
            # 无主键可依 → 退回整行去重。**说清楚这是整行**：源表带 modified/_assign
            # 这类审计列时，整行去重可能一行都去不掉，人得知道自己拿到的是哪一种。
            select_body = select_body.replace("SELECT\n", "SELECT DISTINCT\n", 1)
            detail = "本体未声明主键，退回整行去重（含审计列的源表可能去不掉任何行）"
        notes.append({"rule": "deduplicate", "detail": detail})
    return select_body, applied, unapplied, notes


class TransformExecutor(Executor):
    kind = "transform"

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺少 ontology_id")
        engine = spec.get("engine") or "hive"
        target = spec.get("target_table")
        with SessionLocal() as db:
            result = _generator.generate_etl_sql(
                db,
                ontology_id,
                engine,
                database_prefix=spec.get("database_prefix"),
            )
            # 清洗规则要按主键/非空列改写 SQL，故取同一份逻辑表（列、主键、分区键）。
            plan = _generator.build_logical_schema(
                db, ontology_id, database_prefix=spec.get("database_prefix")
            )
            table = next((t for t in plan.schema.tables if t.name == target), None)

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
        adapter = get_adapter(engine)
        # 生成器产出形如 `INSERT ... TABLE x\nSELECT\n...`；规则只改 SELECT 体，
        # 装载语句（覆盖/追加）由契约决定，不在这里动。
        head, body = _split_statement(sql)
        body, applied, unapplied, notes = _apply_rules(
            body, rules, table, adapter.quote_identifier
        )

        annotated = f"{head}{body};"
        if applied:
            header = "\n".join(f"-- 清洗规则：{r}" for r in applied)
            annotated = f"{header}\n{annotated}"

        return {
            "engine": engine,
            "target_table": qualified,
            "sql": annotated,
            "applied_rules": applied,
            # 不静默丢弃：无法确定性表达的规则显式列出交人处理。
            "unapplied_rules": unapplied,
            # 说清每条规则**具体按哪些列**做的——「已去重」不写明按什么去重等于没说。
            "rule_notes": notes,
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
        # 这条任务自己的 Flink 提交参数（并行度/队列/提交目标/checkpoint/额外 -D）：
        # Spec 优先、context 兜底，留空的项跟随设置页。非法值在此就抛（消息可读），
        # 不带着一个跑不起来的参数去投递 DAG。
        task_flink = flink_params.from_spec(spec, context)

        with SessionLocal() as db:
            ds = db.get(DataSource, target_datasource_id)
            if not ds:
                return {
                    **self._artifacts(spec),
                    "handoff": "datasource_not_found",
                    "note": f"target_datasource_id={target_datasource_id} 不存在，退回仅产出",
                }
            warehouse_conn_id = _warehouse_conn_id(ds)
            source_alias = spec.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS

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

            # 叠加清洗规则到 SELECT 体——与 dry_run 走同一个 _apply_rules：
            # 此前这里只认 deduplicate 且实现与 dry_run 不同，界面看到的 SQL 和真跑的
            # 不是同一条。
            select_body, *_ = _apply_rules(
                flink_input["select_body"],
                [r["rule"] for r in spec.get("cleansing_rules") or []],
                flink_input["target_table"],
                # Flink 语句用 Flink 的引用规则，不是目标数仓的（目标是 postgres 也一样）。
                flink_quote,
            )

            # streaming 作业的 checkpoint 要写进 SQL（SET 'state.checkpoints.dir'），
            # 只放进提交参数是不够的——没有它，流作业重启就从头重算。任务级优先，
            # 其次设置页；批作业跑完即退，无状态可存，不注入。
            checkpoint_dir = ""
            if execution_mode == "streaming":
                from app.services.settings_service import SettingsService

                checkpoint_dir = flink_params.resolve_checkpoint_dir(
                    SettingsService().get_airflow_runtime(db), task_flink
                )

            # 生成完整 Flink SQL
            flink_sql = generate_flink_sql(
                source_table=flink_input["source_table"],
                target_table=flink_input["target_table"],
                # 源端点指**源库**（别名 + 源平台），目标端点指数仓：Flink 同时连两边。
                source=FlinkEndpoint(source_alias, flink_input["source_platform"]),
                target=FlinkEndpoint(warehouse_conn_id, flink_input["target_platform"]),
                select_body=select_body,
                execution_mode=execution_mode,
                source_physical=flink_input["source_physical"],
                target_physical=flink_input["target_physical"],
                checkpoint_dir=checkpoint_dir or None,
            )

            # L1 血缘：源物理名 + 目标物理名 → inlets/outlets URN。
            # 优先用 executor 已算好的物理名（最准）；A1 SQL 解析作回退。
            from app.services.flink_sql_lineage import task_lineage_urns

            source_urns, target_urn = task_lineage_urns(
                sql=flink_sql,
                source_tables=[flink_input["source_physical"]],
                target_table=flink_input["target_physical"],
                source_platform=flink_input["source_platform"],
                target_platform=flink_input["target_platform"],
            )

            # 提交 Flink 作业（落盘 + 触发 Airflow + 回读 DagRun）
            receipt = run_flink_sql(
                db,
                base=context.get("artifact_id") or target_table,
                tasks=(
                    FlinkSqlTask(
                        task_id="transform",
                        sql=flink_sql,
                        # 脚本里的 ${别名_URL/USER/PASSWORD} 靠这张表在运行期补齐：
                        # 值是 Jinja 表达式，任务跑起来才由 Airflow 解析 Connection，
                        # 产物里始终只有占位符。**两端都要给**——跨库作业源和目标各是
                        # 一个 Connection，少给一端就是运行期「缺少凭据环境变量」。
                        env={
                            **endpoint_credential_env(
                                source_alias, flink_input["source_platform"]
                            ),
                            **endpoint_credential_env(
                                warehouse_conn_id, flink_input["target_platform"]
                            ),
                        },
                        # 流式作业提交即 detach（-d），否则 Airflow 任务会永远阻塞在一个
                        # 不会结束的流作业上（与 move_job_compiler 的增量/CDC 同理）。
                        detached=execution_mode == "streaming",
                        checkpoint_dir=checkpoint_dir,
                        # L1 血缘：inlets/outlets
                        source_urns=source_urns,
                        target_urn=target_urn,
                    ),
                ),
                warehouse_conn_id=warehouse_conn_id,
                artifact_id=context.get("artifact_id"),
                flink_task_params=task_flink,
            )
            return receipt


def _warehouse_conn_id(ds: DataSource) -> str:
    """目标仓的 Airflow Connection id（复用 materialization_runner 逻辑）。"""
    slug = "".join(c if c.isalnum() else "_" for c in (ds.name or ds.id)).strip("_").lower()
    return f"ontometa_ds_{slug or ds.id[:8]}"
