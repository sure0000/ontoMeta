"""① 同步作业 Executor —— 确保目标表后搬数据，统一走 Flink SQL on YARN。

同步 DAG 先用幂等 ``CREATE TABLE IF NOT EXISTS`` 确保目标，再执行 Flink SQL 搬运
（全量走 staging + 原子切换）。目标表不存在时自动创建，已存在时不改写。对没有独立加工
的源表对象，同步目标同时就是本体 serving 表；顶层回执的 ``tables`` 仍恒为空，本次
确保的目标通过 ``ensured_tables`` 返回。

与 transform/metric 同一条执行路径（Flink SQL → BashOperator ``flink run``）。
同步只编译 Flink SQL 作业。

**凭据不进产物**：生成的 Flink SQL 里只有 `${别名_*}` 占位符，运行期由 Airflow
Connection 解析（见 flink_sql_generator / endpoint_credential_env）。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.services import flink_params
from app.models import DataSource, ObjectType
from app.services.job_planner import DEFAULT_SOURCE_ALIAS
from app.services.ods_naming import ODS_DATABASE


class SyncExecutor(Executor):
    kind = "sync"

    def _plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        """搬运计划的静态描述（不连库、不生成完整 SQL）：给 dry_run 与「仅产出」兜底用。

        真正的 Flink SQL 由 execute 经 materialization_runner 产出（那里有 DB 与本体）。
        本描述只回答「要把谁搬到哪、什么装载方式」，够界面展示与链上游对账。
        """
        preservation = spec.get("preservation") or {}
        return {
            "source": spec.get("source"),
            "target": spec.get("target"),
            "object_type": spec.get("object_type"),
            "source_datasource_id": spec.get("source_datasource_id"),
            "target_datasource_id": spec.get("target_datasource_id"),
            "target_ods_database": ODS_DATABASE,
            "target_ods_table": spec.get("target_ods_table"),
            "mode": spec.get("mode") or "full",
            "primary_keys": spec.get("primary_keys") or [],
            "incremental_column": spec.get("incremental_column"),
            "initial_watermark": spec.get("initial_watermark"),
            "sequence_column": spec.get("sequence_column"),
            "delete_policy": spec.get("delete_policy"),
            # 调度频率进计划描述：人在「确认执行方案」那一环要看得见这条同步是定时跑
            # 还是只手动跑一次——这是搬运任务最实质的一条属性。
            "refresh_cron": spec.get("refresh_cron"),
            "engine": spec.get("engine") or "doris",
            # 凭据不入产物：只放源库连接别名（= Airflow conn_id）。
            "source_ref_alias": spec.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS,
            "preserved": bool(preservation.get("preserve")),
            "preservation_reason": preservation.get("reason"),
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(spec)
        return {
            "action": "flink_sql_move",
            "source": plan["source"],
            "target": plan["target"],
            "object_type": plan["object_type"],
            "source_datasource_id": plan["source_datasource_id"],
            "target_datasource_id": plan["target_datasource_id"],
            "target_ods_database": plan["target_ods_database"],
            "target_ods_table": plan["target_ods_table"],
            "mode": plan["mode"],
            "primary_keys": plan["primary_keys"],
            "incremental_column": plan["incremental_column"],
            "initial_watermark": plan["initial_watermark"],
            "sequence_column": plan["sequence_column"],
            "delete_policy": plan["delete_policy"],
            "refresh_cron": plan["refresh_cron"],
            "engine": plan["engine"],
            "preserved": plan["preserved"],
            "preservation_reason": plan["preservation_reason"],
            "side_effects": "无（dry-run 不生成/不提交作业）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 未配 target_datasource，退回「仅产出」（与 transform/metric 同逻辑）。
        target_datasource_id = spec.get("target_datasource_id") or context.get(
            "target_datasource_id"
        )
        if not target_datasource_id:
            return {
                **self._plan(spec),
                "handoff": "flink_sql",
                "note": "未配置 target_datasource_id，ontoMeta 只给出搬运计划，不执行（链上游会传入此字段）",
            }

        # 进链执行：sync = 先确保目标表，再搬数据。走 materialization_runner 的 Flink SQL 通道，
        # 传 selected_targets=[对象名] 只搬那一个对象。回执带 dag_id，可被
        # pipeline_compiler 串进链 DAG。
        object_type = spec.get("object_type")
        if not object_type:
            return {
                **self._plan(spec),
                "handoff": "flink_sql",
                "note": "spec 缺 object_type，无法定位要搬的对象，退回仅产出",
            }

        try:
            from app.services import materialization_runner
        except ImportError as exc:
            return {
                **self._plan(spec),
                "handoff": "import_error",
                "note": f"搬运模块导入失败：{exc}，退回仅产出",
            }

        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺 ontology_id")
        engine = spec.get("engine") or "doris"
        mode = str(spec.get("mode") or "full").lower()

        from app.database import SessionLocal

        with SessionLocal() as db:
            target_ds = db.get(DataSource, target_datasource_id)
            source_datasource_id = None
            if target_ds is not None and target_ds.purpose == "warehouse":
                source_ds = db.get(DataSource, spec.get("source_datasource_id"))
                if source_ds is None or source_ds.purpose != "business_source":
                    return {
                        **self._plan(spec),
                        "handoff": "flink_sql",
                        "note": "同步未执行：未绑定启用的 business_source DataSource",
                    }
                obj = (
                    db.query(ObjectType)
                    .filter(
                        ObjectType.ontology_id == ontology_id,
                        ObjectType.name == object_type,
                    )
                    .first()
                )
                if obj is None:
                    raise ValueError(f"本体对象 {object_type} 不存在")

                # Validation normally catches these before confirmation. Repeat the
                # read-only preflight here so older validated artifacts and direct
                # executor calls cannot submit a DAG with unresolved conn_ids.
                from app.services.materialize_preflight import run_preflight

                preflight = run_preflight(
                    db,
                    ontology_id,
                    target_datasource_id=target_datasource_id,
                    engine=materialization_runner.resolve_engine(
                        db, target_datasource_id, engine
                    ),
                    selected_targets=[object_type],
                    source_datasource_id=source_ds.id,
                    source_database=(
                        str(spec["source"]).split(".", 1)[0]
                        if spec.get("source") and "." in str(spec["source"])
                        else None
                    ),
                    managed_connections=True,
                    # 与下面 run_sync 传的是同一套装载参数：自检预演的必须就是这次要提交的
                    # 作业，否则它按契约预演出 CDC/增量，拦下一条其实是全量的同步。
                    emit="dml",
                    load_strategy=mode,
                    incremental_column=spec.get("incremental_column"),
                    initial_watermark=spec.get("initial_watermark"),
                    flink_task_params=flink_params.from_spec(spec, context),
                )
                if not preflight.ok:
                    blocking = [
                        f"[{item.label}] {item.detail}"
                        + (f" -> {item.next_step}" if item.next_step else "")
                        for item in preflight.blocking_failures
                    ]
                    raise RuntimeError(
                        f"提交前自检发现 {len(blocking)} 项阻断，无法执行同步：\n"
                        + "\n".join(
                            f"  {idx + 1}. {message}"
                            for idx, message in enumerate(blocking)
                        )
                    )
                from app.services.ingestion_contract import (
                    IngestionContractError,
                    IngestionContractService,
                )
                contract_data = {
                    "object_type_id": obj.id,
                    "source_datasource_id": source_ds.id,
                    "source_physical_table": spec.get("source"),
                    "source_mapping": spec.get("source_mapping") or {},
                    "doris_datasource_id": target_ds.id,
                    # 落点库不可配：存量 Spec 里带的自定义库名不再生效，同步恒进 ODS。
                    "target_ods_database": ODS_DATABASE,
                    # IngestionContractService 会按 ods_{数据域}_{原始表名} 强制重算；
                    # Spec 里的历史值只为兼容旧制品传入，不能覆盖后端规则。
                    "target_ods_table": spec.get("target_ods_table"),
                    "mode": mode,
                    "primary_keys": spec.get("primary_keys") or [],
                    "sequence_column": spec.get("sequence_column"),
                    "incremental_column": spec.get("incremental_column"),
                    "initial_watermark": spec.get("initial_watermark"),
                    "late_arrival_policy": spec.get("late_arrival_policy") or "strict",
                    "idempotency_strategy": spec.get("idempotency_strategy")
                    or "primary_key_upsert",
                    "delete_policy": spec.get("delete_policy") or "ignore",
                    "refresh_cron": spec.get("refresh_cron"),
                    "flink_params": flink_params.from_spec(spec, context),
                    "status": "active",
                }
                try:
                    ingestion = IngestionContractService().upsert(
                        db, ontology_id, contract_data
                    )
                except IngestionContractError as exc:
                    return {
                        **self._plan(spec),
                        "handoff": "flink_sql",
                        "note": f"同步未执行（{exc}）",
                    }
                source_token = "".join(c for c in source_ds.id.lower() if c.isalnum())[:12]
                source_alias = f"ontometa_source_{source_token}"
                source_datasource_id = source_ds.id
                ods_database = ingestion.target_ods_database
                ods_table = ingestion.target_ods_table
                primary_keys = {object_type: spec.get("primary_keys") or []}
                sequences = (
                    {object_type: ingestion.sequence_column}
                    if ingestion.sequence_column else {}
                )
                incremental_columns = (
                    {object_type: ingestion.incremental_column}
                    if ingestion.incremental_column else {}
                )
                initial_watermarks = {
                    object_type: ingestion.sync_watermark
                    or ingestion.initial_watermark
                } if ingestion.mode == "incremental" else {}
                source_physical_tables = {
                    object_type: ingestion.source_physical_table
                }
                source_platforms = {object_type: source_ds.kind}
                source_mappings = {
                    object_type: contract_data["source_mapping"]
                }
                delete_policies = {object_type: ingestion.delete_policy}
            else:
                source_alias = spec.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS
                ods_database = ODS_DATABASE
                ods_table = None
                primary_keys = None
                sequences = None
                incremental_columns = None
                initial_watermarks = None
                source_physical_tables = None
                source_platforms = None
                source_mappings = None
                delete_policies = None
            try:
                receipt = materialization_runner.run_sync(
                    db,
                    ontology_id,
                    target_datasource_id=target_datasource_id,
                    engine=engine,
                    # 同步不带库名前缀：落点恒为 ODS_DATABASE（见 ods_naming）。
                    database_prefix=None,
                    load_strategy=mode,
                    # 调度频率：写回本次选中实体的契约，再由 `_cron_by_entity` 决定这条
                    # 搬运 DAG 的 schedule（一个 cron 一个 DAG）。不传的话产出的永远是
                    # schedule=None 的手动 DAG——同步任务就此只能靠人点。
                    refresh_cron=spec.get("refresh_cron"),
                    # 只搬这一个对象（按实体名裁剪）
                    selected_targets=[object_type],
                    artifact_id=context.get("artifact_id"),
                    # 这条同步自己的 Flink 提交参数（并行度/队列/提交目标/checkpoint/
                    # 额外 -D）：Spec 优先、context 兜底，留空的项跟随设置页默认。
                    flink_task_params=flink_params.from_spec(spec, context),
                    source_alias=source_alias,
                    source_datasource_id=source_datasource_id,
                    target_ods_database=ods_database,
                    target_ods_tables=({object_type: ods_table} if ods_table else None),
                    target_primary_keys=primary_keys,
                    sequence_columns=sequences,
                    incremental_columns=incremental_columns,
                    initial_watermarks=initial_watermarks,
                    source_physical_tables=source_physical_tables,
                    source_platforms=source_platforms,
                    source_mappings=source_mappings,
                    delete_policies=delete_policies,
                )
            except materialization_runner.MaterializationError as exc:
                # 未配 Airflow / Flink / 投递失败 / 无可搬对象：退回仅产出，不静默假装执行了。
                return {
                    **self._plan(spec),
                    "handoff": "flink_sql",
                    "note": f"搬运未执行（{exc}），退回仅产出",
                }
            if target_ds is not None and target_ds.purpose == "warehouse":
                ingestion.status = "submitted" if receipt.get("ok") else "failed"
                db.commit()
                receipt = {
                    **receipt,
                    "compute_engine": "flink",
                    "target_engine": "doris",
                    "ingestion_contract_id": ingestion.id,
                    "object": {
                        "name": obj.name,
                        "display_name": obj.display_name or obj.name,
                    },
                    "source_datasource": {
                        "name": source_ds.name,
                        "kind": source_ds.kind,
                    },
                    "mode": ingestion.mode,
                    "target_tables": [
                        f"{ingestion.target_ods_database}.{ingestion.target_ods_table}"
                    ],
                    "watermark_before": ingestion.sync_watermark,
                    # Final watermark/job id are deliberately absent until
                    # Airflow/Flink reconciliation obtains their real values.
                    "watermark_after": None,
                    "flink_job_id": ingestion.flink_job_id,
                }

        # 关键源保全（STG 原始副本）：判定仍在（drafter 产出），但 Flink 搬运路径尚未
        # 实现「额外产一份贴源 STG 作业」。不静默吞掉——如实在回执里标注为未落地，
        # 供人看见（真要保全需另在 Flink 路径产一个 full→STG 的搬运，属后续工作）。
        if (spec.get("preservation") or {}).get("preserve"):
            receipt = {
                **receipt,
                "preservation_pending": True,
                "preservation_reason": spec["preservation"].get("reason"),
                "preservation_note": (
                    "关键源保全（STG 原始副本）尚未在 Flink 搬运路径实现，本次未产出 STG 副本"
                ),
            }
        return receipt
