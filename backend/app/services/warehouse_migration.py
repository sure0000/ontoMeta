"""Phase 6 production migration control plane.

This module does not pretend to execute a production migration from the Web
process. Write jobs remain GovernanceArtifacts orchestrated by Airflow/Flink.
It enforces the ordered evidence, approval, observation and cleanup gates that
turn those job results into an auditable cut-over.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError
from app.models import (
    DataSource,
    DorisWarehouseConfig,
    GovernanceArtifact,
    IngestionContract,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseMigrationBatch,
    WarehouseMigrationEvidence,
    WarehouseObjectProjection,
)
from app.services.settings_service import SettingsService

STEP_NAMES = {
    1: "生产默认 Doris 与最小权限身份",
    2: "当前 ontology version Deployment/Projection",
    3: "物化 Doris ODS/DIM/DWD/DWS/ADS",
    4: "全量同步 ODS",
    5: "执行 Doris transform",
    6: "执行 Doris metric/tag/rule",
    7: "业务与技术对账",
    8: "CDC 延迟、水位、更新删除与恢复",
    9: "Data Agent shadow query",
    10: "审批切换 Doris-only",
    11: "稳定观察窗口",
    12: "停止旧周期 DAG",
    13: "删除运行时兼容路径",
    14: "历史 Artifact/receipt 只读审计",
    15: "更新 as-built 文档",
}

_REQUIRED_REPORT_KEYS: dict[int, tuple[str, ...]] = {
    1: ("doris_version", "fe_nodes", "be_nodes", "identities", "preflight"),
    2: ("deployment_id", "ontology_version", "projection_count"),
    3: ("layers", "artifact_ids", "airflow_final_state"),
    4: ("tables", "rows_read", "rows_written", "watermarks", "airflow_final_state"),
    5: ("tables", "artifact_ids", "airflow_final_state"),
    6: ("metric", "tag", "rule", "artifact_ids", "airflow_final_state"),
    7: ("row_count", "primary_key", "null_rate", "business_time", "amount_sum", "quantity_count", "dimension_distribution", "metric_results"),
    8: ("lag_seconds", "watermarks", "update", "delete", "checkpoint", "restart_recovery"),
    9: ("cases", "matched", "different", "blocking_differences"),
    11: ("started_at", "ended_at", "airflow", "flink", "doris", "incidents"),
    13: ("static_audit", "removed", "remaining"),
    14: ("artifact_count", "receipt_count", "read_only_verified"),
    15: ("documents", "test_results", "execution_matrix"),
}

_ACTIVE = {"preparing", "blocked", "cutover", "observing", "legacy_stopped", "cleaning"}
_TERMINAL = {"completed", "rolled_back", "aborted"}


class MigrationGateError(ValueError):
    pass


def _loads(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> datetime:
    # SQLAlchemy's SQLite DateTime returns naive values; use naive UTC in DB.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _checksum(report: dict[str, Any]) -> str:
    return hashlib.sha256(_dumps(report).encode("utf-8")).hexdigest()


def _receipt(artifact: GovernanceArtifact) -> dict[str, Any]:
    return _loads(artifact.execution_receipt_json, {})


def _artifacts(db: Session, ids: list[str]) -> list[GovernanceArtifact]:
    rows = [db.get(GovernanceArtifact, artifact_id) for artifact_id in ids]
    missing = [artifact_id for artifact_id, row in zip(ids, rows) if row is None]
    if missing:
        raise MigrationGateError(f"Artifact 不存在：{', '.join(missing)}")
    return [row for row in rows if row is not None]


def _successful_artifacts(
    db: Session, *, ids: list[str], kinds: set[str], engine: str | None = None
) -> list[GovernanceArtifact]:
    if not ids:
        raise MigrationGateError("必须关联至少一个成功 Artifact")
    rows = _artifacts(db, ids)
    for row in rows:
        if row.kind not in kinds:
            raise MigrationGateError(f"Artifact {row.id} kind={row.kind} 不属于 {sorted(kinds)}")
        if row.status != "succeeded":
            raise MigrationGateError(f"Artifact {row.id} 尚未最终 succeeded")
        receipt = _receipt(row)
        if receipt.get("execute_mode") != "orchestrated":
            raise MigrationGateError(f"Artifact {row.id} 未真实编排执行（禁止 handoff 假绿）")
        batches = receipt.get("batches") or []
        if batches:
            if any(not item.get("dag_id") or not item.get("dag_run_id") for item in batches):
                raise MigrationGateError(f"Artifact {row.id} 缺少真实 DagRun 标识")
        elif not receipt.get("dag_id") or not receipt.get("dag_run_id"):
            raise MigrationGateError(f"Artifact {row.id} 缺少真实 DagRun 标识")
        # The receipt is an immutable submission record and may legitimately say
        # queued. Final state is supplied by the Phase 6 evidence after Airflow
        # reconciliation; never rewrite historical receipt JSON to make it green.
        if engine and (receipt.get("compute_engine") or receipt.get("engine")) != engine:
            raise MigrationGateError(f"Artifact {row.id} 执行引擎不是 {engine}")

    runtime = SettingsService().get_airflow_runtime(db)
    if not runtime.available:
        raise MigrationGateError("Airflow 不可用，无法验证 Artifact 最终态")
    client = AirflowClient(
        runtime.endpoint, username=runtime.username, password=runtime.password
    )
    try:
        for row in rows:
            receipt = _receipt(row)
            batches = receipt.get("batches") or [{
                "dag_id": receipt.get("dag_id"),
                "dag_run_id": receipt.get("dag_run_id"),
            }]
            for batch in batches:
                try:
                    run = client.get_dag_run(batch["dag_id"], batch["dag_run_id"])
                except (AirflowError, KeyError) as exc:
                    raise MigrationGateError(
                        f"Artifact {row.id} 的 Airflow DagRun 无法验证：{exc}"
                    ) from exc
                if str(run.get("state") or "").lower() != "success":
                    raise MigrationGateError(
                        f"Artifact {row.id} 的 Airflow 最终态为 {run.get('state')!r}，不是 success"
                    )
            # Reconcile metadata from the verified external final state without
            # mutating the immutable submission receipt.
            if row.kind == "materialize":
                params = receipt.get("deployment_reconciliation") or {}
                if not params:
                    raise MigrationGateError(
                        f"Artifact {row.id} 缺少 Deployment 对账参数"
                    )
                from app.services.doris_deployment import publish_schema_ready

                publish_schema_ready(db, **params)
            elif row.kind == "transform":
                from app.services.transform_reconciliation import reconcile_transform_receipt

                reconcile_transform_receipt(db, receipt=receipt, airflow_state="success")
            elif row.kind == "metric":
                from app.services.metric_reconciliation import reconcile_metric_receipt

                reconcile_metric_receipt(db, receipt=receipt, airflow_state="success")
    finally:
        client.close()
    return rows


class WarehouseMigrationService:
    def create_batch(
        self,
        db: Session,
        *,
        ontology_id: str,
        approver: str,
        rollback_owner: str,
        observation_window_minutes: int,
        legacy_dag_ids: list[str] | None = None,
        new_dag_ids: list[str] | None = None,
        created_by: str | None = None,
    ) -> WarehouseMigrationBatch:
        ontology = db.get(Ontology, ontology_id)
        if ontology is None or ontology.status != "published":
            raise MigrationGateError("Phase 6 只能迁移当前已发布本体")
        if not approver.strip() or not rollback_owner.strip():
            raise MigrationGateError("必须明确审批人与回滚负责人")
        from app.models import Principal

        publisher_ids = {
            row.id
            for row in db.query(Principal).filter(
                Principal.active.is_(True), Principal.role == "publisher"
            )
        }
        valid_actors = publisher_ids | {"bootstrap-admin"}
        if approver.strip() not in valid_actors:
            raise MigrationGateError("审批人必须是已启用的 publisher principal id")
        if rollback_owner.strip() not in valid_actors:
            raise MigrationGateError("回滚负责人必须是已启用的 publisher principal id")
        if observation_window_minutes <= 0:
            raise MigrationGateError("观察窗口必须大于 0 分钟")
        existing = (
            db.query(WarehouseMigrationBatch)
            .filter(
                WarehouseMigrationBatch.ontology_id == ontology_id,
                WarehouseMigrationBatch.status.in_(_ACTIVE),
            )
            .first()
        )
        if existing:
            raise MigrationGateError(f"本体已有未结束迁移批次 {existing.id}")
        doris = self._default_doris(db)
        batch = WarehouseMigrationBatch(
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            doris_datasource_id=doris.id if doris else None,
            approver=approver.strip(),
            rollback_owner=rollback_owner.strip(),
            observation_window_minutes=observation_window_minutes,
            legacy_dag_ids_json=_dumps(sorted(set(legacy_dag_ids or []))),
            new_dag_ids_json=_dumps(sorted(set(new_dag_ids or []))),
            created_by=created_by,
        )
        db.add(batch)
        db.commit()
        db.refresh(batch)
        return batch

    @staticmethod
    def _default_doris(db: Session) -> DataSource | None:
        rows = (
            db.query(DataSource)
            .filter(
                DataSource.purpose == "warehouse",
                DataSource.kind == "doris",
                DataSource.is_default_warehouse.is_(True),
                DataSource.enabled.is_(True),
            )
            .all()
        )
        return rows[0] if len(rows) == 1 else None

    def _batch(self, db: Session, batch_id: str) -> WarehouseMigrationBatch:
        batch = db.get(WarehouseMigrationBatch, batch_id)
        if batch is None:
            raise MigrationGateError("迁移批次不存在")
        ontology = db.get(Ontology, batch.ontology_id)
        if ontology is None or ontology.status != "published" or ontology.version != batch.ontology_version:
            raise MigrationGateError("本体已变更或不再发布；原迁移批次必须终止并重新建立")
        return batch

    def _record(
        self,
        db: Session,
        batch: WarehouseMigrationBatch,
        *,
        step: int,
        passed: bool,
        report: dict[str, Any],
        artifact_ids: list[str],
        operator: str,
    ) -> WarehouseMigrationEvidence:
        attempt = (
            db.query(func.max(WarehouseMigrationEvidence.attempt))
            .filter(
                WarehouseMigrationEvidence.batch_id == batch.id,
                WarehouseMigrationEvidence.step == step,
            )
            .scalar()
            or 0
        ) + 1
        evidence = WarehouseMigrationEvidence(
            batch_id=batch.id,
            step=step,
            attempt=attempt,
            status="pass" if passed else "fail",
            report_json=_dumps(report),
            artifact_ids_json=_dumps(artifact_ids),
            checksum=_checksum(report),
            recorded_by=operator,
        )
        db.add(evidence)
        if passed:
            batch.current_step = step
            batch.blocked_reason = None
            if step < 10:
                batch.status = "preparing"
            elif step == 11:
                batch.status = "observing"
            elif step == 12:
                batch.status = "legacy_stopped"
                batch.legacy_stopped_at = _now()
            elif step in (13, 14):
                batch.status = "cleaning"
            elif step == 15:
                batch.status = "completed"
                batch.completed_at = _now()
        else:
            batch.status = "blocked"
            batch.blocked_reason = f"步骤 {step} {STEP_NAMES[step]} 阻断失败"
        db.commit()
        db.refresh(evidence)
        return evidence

    def record_step(
        self,
        db: Session,
        *,
        batch_id: str,
        step: int,
        passed: bool,
        report: dict[str, Any],
        artifact_ids: list[str] | None,
        operator: str,
    ) -> WarehouseMigrationEvidence:
        batch = self._batch(db, batch_id)
        if batch.status in _TERMINAL:
            raise MigrationGateError(f"批次已终止：{batch.status}")
        if step in {10, 12}:
            raise MigrationGateError(f"步骤 {step} 必须调用专用受控动作，不能手工记通过")
        expected = batch.current_step + 1
        if step != expected:
            raise MigrationGateError(f"必须严格执行步骤 {expected}，不能提交步骤 {step}")
        if step == 11:
            if not batch.observation_ends_at or _now() < batch.observation_ends_at:
                raise MigrationGateError("稳定观察窗口尚未结束")
        missing = [key for key in _REQUIRED_REPORT_KEYS.get(step, ()) if key not in report]
        if passed and missing:
            raise MigrationGateError(f"步骤 {step} 报告缺字段：{', '.join(missing)}")
        ids = list(dict.fromkeys(artifact_ids or report.get("artifact_ids") or []))
        if passed:
            self._validate_step(db, batch, step=step, report=report, artifact_ids=ids)
        return self._record(
            db, batch, step=step, passed=passed, report=report,
            artifact_ids=ids, operator=operator,
        )

    def _validate_step(
        self,
        db: Session,
        batch: WarehouseMigrationBatch,
        *,
        step: int,
        report: dict[str, Any],
        artifact_ids: list[str],
    ) -> None:
        if step == 1:
            doris = self._default_doris(db)
            if doris is None or not (doris.dsn_secret_ref or "").strip() or doris.status != "ok":
                raise MigrationGateError("不存在唯一、已测试且配置 reader DSN 的默认 Doris")
            config = (
                db.query(DorisWarehouseConfig)
                .filter(
                    DorisWarehouseConfig.warehouse_datasource_id == doris.id,
                    DorisWarehouseConfig.enabled.is_(True),
                )
                .first()
            )
            if not config or not all((
                config.reader_dsn_secret_ref,
                config.airflow_ddl_conn_id,
                config.airflow_etl_conn_id,
                config.airflow_flink_conn_id,
                config.fenodes_json,
            )):
                raise MigrationGateError(
                    "Doris reader DSN、9030/8030 与 DDL/ETL/Flink Connection 配置不完整"
                )
            identities = report.get("identities") or {}
            required = {"ontometa_reader", "ontometa_ddl", "ontometa_etl", "ontometa_flink_sink"}
            if not required <= set(identities) or not all(identities.get(name) == "pass" for name in required):
                raise MigrationGateError("四种最小权限身份未全部验证通过")
            if report.get("preflight") != "pass":
                raise MigrationGateError("真实 Doris/Airflow/Flink preflight 未通过")
            batch.doris_datasource_id = doris.id
        elif step == 2:
            rows = db.query(OntologyWarehouseDeployment).filter(
                OntologyWarehouseDeployment.ontology_id == batch.ontology_id,
                OntologyWarehouseDeployment.ontology_version == batch.ontology_version,
                OntologyWarehouseDeployment.doris_datasource_id == batch.doris_datasource_id,
                OntologyWarehouseDeployment.status == "pending",
            ).all()
            if len(rows) != 1:
                raise MigrationGateError("步骤 2 要求唯一 pending 的当前版本 Doris Deployment")
            deployment = rows[0]
            count = db.query(WarehouseObjectProjection).filter(
                WarehouseObjectProjection.deployment_id == deployment.id
            ).count()
            if count == 0 or count != int(report.get("projection_count") or 0):
                raise MigrationGateError("Deployment Projection 数量为空或与报告不一致")
            if report.get("deployment_id") != deployment.id or int(report.get("ontology_version") or -1) != batch.ontology_version:
                raise MigrationGateError("报告未绑定当前 ontology version 的 Deployment")
        elif step == 3:
            artifacts = _successful_artifacts(
                db, ids=artifact_ids, kinds={"materialize"}, engine="doris"
            )
            if report.get("airflow_final_state") != "success":
                raise MigrationGateError("Doris 物化 Airflow 最终态不是 success")
            actual_tables = {
                table
                for artifact in artifacts
                for table in (_receipt(artifact).get("tables") or [])
            }
            actual_layers = {
                table.split(".", 1)[0].split("_", 1)[0].lower()
                for table in actual_tables if "." in table
            }
            required_layers = {"ods", "dim", "dwd", "dws", "ads"}
            if not required_layers <= actual_layers:
                raise MigrationGateError(
                    "materialize Artifact 回执未证明 ODS/DIM/DWD/DWS/ADS 全部建表"
                )
            deployment = self._current_deployment(db, batch)
            not_ready = db.query(WarehouseObjectProjection).filter(
                WarehouseObjectProjection.deployment_id == deployment.id,
                WarehouseObjectProjection.schema_status != "ready",
            ).count()
            if not_ready:
                raise MigrationGateError(f"仍有 {not_ready} 个 Projection schema 未 ready")
            if set(report.get("layers") or []) != required_layers:
                raise MigrationGateError("物化报告必须覆盖 ODS/DIM/DWD/DWS/ADS")
        elif step == 4:
            _successful_artifacts(db, ids=artifact_ids, kinds={"sync"})
            if report.get("airflow_final_state") != "success":
                raise MigrationGateError("ODS 全量同步 Airflow/Flink 最终态不是 success")
            contracts = db.query(IngestionContract).filter(
                IngestionContract.ontology_id == batch.ontology_id,
                IngestionContract.ontology_version == batch.ontology_version,
            ).all()
            if not contracts or any(c.status != "ready" for c in contracts if c.mode != "cdc"):
                raise MigrationGateError("全量/增量 IngestionContract 未全部 ready")
            if int(report.get("rows_read") or 0) != int(report.get("rows_written") or -1):
                raise MigrationGateError("ODS rows_read 与 rows_written 不一致")
        elif step == 5:
            _successful_artifacts(db, ids=artifact_ids, kinds={"transform"}, engine="doris")
            if report.get("airflow_final_state") != "success":
                raise MigrationGateError("Doris transform Airflow 最终态不是 success")
        elif step == 6:
            _successful_artifacts(db, ids=artifact_ids, kinds={"metric"}, engine="doris")
            if report.get("airflow_final_state") != "success":
                raise MigrationGateError("Doris metric/tag/rule Airflow 最终态不是 success")
            deployment = self._current_deployment(db, batch)
            failed = db.query(WarehouseLogicProjection).filter(
                WarehouseLogicProjection.deployment_id == deployment.id,
                WarehouseLogicProjection.queryable.is_(False),
            ).count()
            if failed:
                raise MigrationGateError(f"仍有 {failed} 个 metric/tag/rule Projection 不可查询")
        elif step == 7:
            if report.get("blocking_differences"):
                raise MigrationGateError("对账仍有阻断差异")
            for section in _REQUIRED_REPORT_KEYS[7]:
                if report.get(section) in (None, [], {}):
                    raise MigrationGateError(f"对账维度 {section} 为空")
        elif step == 8:
            if any(report.get(key) != "pass" for key in ("update", "delete", "checkpoint", "restart_recovery")):
                raise MigrationGateError("CDC UPDATE/DELETE/checkpoint/重启恢复未全部通过")
            if report.get("blocking_differences"):
                raise MigrationGateError("CDC 验证存在阻断差异")
            cdc_contracts = db.query(IngestionContract).filter(
                IngestionContract.ontology_id == batch.ontology_id,
                IngestionContract.ontology_version == batch.ontology_version,
                IngestionContract.mode == "cdc",
            ).all()
            if not cdc_contracts:
                raise MigrationGateError("没有当前版本 CDC IngestionContract，无法验证步骤 8")
            from app.services.flink_health import FlinkHealthError, check_ingestion_job

            for contract in cdc_contracts:
                try:
                    health = check_ingestion_job(db, contract.id)
                except FlinkHealthError as exc:
                    raise MigrationGateError(
                        f"CDC Contract {contract.id} Flink 健康检查失败：{exc}"
                    ) from exc
                if not health.get("healthy"):
                    raise MigrationGateError(
                        f"CDC Contract {contract.id} Flink 最终态不健康"
                    )
        elif step == 9:
            if int(report.get("cases") or 0) <= 0:
                raise MigrationGateError("shadow query 必须至少有一个用例")
            if int(report.get("different") or 0) or report.get("blocking_differences"):
                raise MigrationGateError("shadow query 存在未审批差异")
            if int(report.get("matched") or 0) != int(report.get("cases") or 0):
                raise MigrationGateError("shadow query 匹配数与用例数不一致")
            if any(key in report for key in ("rows", "legacy_rows", "doris_rows")):
                raise MigrationGateError("shadow 报告不得保存或返回原始业务结果")
        elif step == 11:
            if report.get("incidents"):
                raise MigrationGateError("观察窗口存在未关闭事故")
            for component in ("airflow", "flink", "doris"):
                if report.get(component) != "pass":
                    raise MigrationGateError(f"观察窗口 {component} 最终态未通过")
        elif step == 13:
            audit = runtime_compatibility_inventory()
            report["static_audit"] = audit
            if audit["blocking"]:
                raise MigrationGateError("仍有运行时 Hive/StarRocks/source fallback：" + "; ".join(audit["blocking"]))
            batch.compatibility_items_json = _dumps(report.get("remaining") or audit["remaining"])
        elif step == 14:
            if report.get("read_only_verified") is not True:
                raise MigrationGateError("历史 Artifact/receipt 只读审计未验证")
            if int(report.get("artifact_count") or 0) < int(report.get("receipt_count") or 0):
                raise MigrationGateError("receipt 数不能大于 Artifact 数")
        elif step == 15:
            documents = set(report.get("documents") or [])
            required_docs = {
                "docs/DW_IMPLEMENTATION.md",
                "docs/UNIFIED_EXECUTION_ARCHITECTURE.md",
                "docs/MATERIALIZE_SYNC_STABILITY.md",
                "docs/TASK_PIPELINE_PLAN.md",
                "docs/DORIS_WAREHOUSE_REFACTOR_PLAN.md",
            }
            if not required_docs <= documents:
                raise MigrationGateError("as-built 文档未全部更新")
            tests = report.get("test_results") or {}
            if not tests or any(v.get("status") != "pass" for v in tests.values() if isinstance(v, dict)):
                raise MigrationGateError("全量测试结果不完整或未通过")
            matrix = report.get("execution_matrix") or {}
            expected = {"materialize": "doris", "sync": "flink", "transform": "doris", "metric": "doris", "query": "doris"}
            if any(matrix.get(k) != v for k, v in expected.items()):
                raise MigrationGateError("最终执行矩阵不符合 Doris-only 架构")

    def _current_deployment(self, db: Session, batch: WarehouseMigrationBatch) -> OntologyWarehouseDeployment:
        rows = db.query(OntologyWarehouseDeployment).filter(
            OntologyWarehouseDeployment.ontology_id == batch.ontology_id,
            OntologyWarehouseDeployment.ontology_version == batch.ontology_version,
            OntologyWarehouseDeployment.doris_datasource_id == batch.doris_datasource_id,
            OntologyWarehouseDeployment.status.in_(("schema_ready", "ready")),
        ).all()
        if len(rows) != 1:
            raise MigrationGateError("当前本体版本没有唯一就绪的 Doris Deployment")
        return rows[0]

    def record_rollback_drill(
        self, db: Session, *, batch_id: str, report: dict[str, Any], operator: str
    ) -> WarehouseMigrationBatch:
        batch = self._batch(db, batch_id)
        required = {"performed_at", "owner", "stop_new_dags", "restore_old_read_only", "watermark_resume", "rto_seconds", "result"}
        missing = sorted(required - set(report))
        if missing:
            raise MigrationGateError("回滚演练报告缺字段：" + ", ".join(missing))
        if report.get("owner") != batch.rollback_owner or report.get("result") != "pass":
            raise MigrationGateError("回滚负责人不匹配或演练未通过")
        if report.get("fallback_to_business_source"):
            raise MigrationGateError("回滚演练不得让 Data Agent fallback 到业务源")
        report = {**report, "recorded_by": operator}
        batch.rollback_drill_json = _dumps(report)
        db.commit()
        db.refresh(batch)
        return batch

    def approve_cutover(
        self,
        db: Session,
        *,
        batch_id: str,
        approver: str,
        note: str,
    ) -> WarehouseMigrationEvidence:
        batch = self._batch(db, batch_id)
        if batch.current_step != 9:
            raise MigrationGateError("只有步骤 1–9 全部通过后才能审批切流")
        if approver.strip() != batch.approver:
            raise MigrationGateError("审批人身份与批次指定审批人不匹配")
        if not note.strip():
            raise MigrationGateError("审批备注不能为空")
        drill = _loads(batch.rollback_drill_json, {})
        if drill.get("result") != "pass":
            raise MigrationGateError("切流前必须完成并通过回滚演练")
        self._current_deployment(db, batch)
        latest = self._latest_evidence(db, batch.id)
        if any(latest.get(step) is None or latest[step].status != "pass" for step in range(1, 10)):
            raise MigrationGateError("步骤 1–9 的最新证据并非全部通过")
        now = _now()
        batch.approved_by = approver.strip()
        batch.approval_note = note.strip()
        batch.approved_at = now
        batch.cutover_at = now
        batch.observation_ends_at = now + timedelta(minutes=batch.observation_window_minutes)
        batch.status = "cutover"
        report = {
            "approved_by": batch.approved_by,
            "approved_at": now.isoformat(),
            "approval_note": batch.approval_note,
            "observation_ends_at": batch.observation_ends_at.isoformat(),
            "query_mode": "doris_only",
        }
        return self._record(db, batch, step=10, passed=True, report=report, artifact_ids=[], operator=approver)

    def stop_legacy_dags(
        self, db: Session, *, batch_id: str, operator: str
    ) -> WarehouseMigrationEvidence:
        batch = self._batch(db, batch_id)
        if batch.current_step != 11:
            raise MigrationGateError("观察窗口通过后才能停止旧周期 DAG")
        dag_ids = _loads(batch.legacy_dag_ids_json, [])
        if not dag_ids:
            raise MigrationGateError("批次未登记旧周期 DAG 清单")
        airflow = SettingsService().get_airflow_runtime(db)
        if not airflow.available:
            raise MigrationGateError("Airflow 不可用，不能验证旧 DAG 已停止")
        client = AirflowClient(airflow.endpoint, username=airflow.username, password=airflow.password)
        results: list[dict[str, Any]] = []
        try:
            for dag_id in dag_ids:
                try:
                    payload = client.pause_dag(dag_id)
                    results.append({"dag_id": dag_id, "status": "paused", "is_paused": payload.get("is_paused", True)})
                except AirflowError as exc:
                    results.append({"dag_id": dag_id, "status": "failed", "error": str(exc)})
        finally:
            client.close()
        passed = all(item["status"] == "paused" and item["is_paused"] is True for item in results)
        return self._record(
            db, batch, step=12, passed=passed,
            report={"dag_ids": dag_ids, "results": results}, artifact_ids=[], operator=operator,
        )

    def rollback(
        self, db: Session, *, batch_id: str, operator: str, reason: str
    ) -> WarehouseMigrationBatch:
        batch = self._batch(db, batch_id)
        if batch.current_step < 10 or batch.current_step >= 13:
            raise MigrationGateError("实际回滚只允许在已切流且旧运行时清理前执行")
        if operator != batch.rollback_owner:
            raise MigrationGateError("只有批次指定回滚负责人可执行回滚")
        if not reason.strip():
            raise MigrationGateError("回滚原因不能为空")
        airflow = SettingsService().get_airflow_runtime(db)
        new_dags = _loads(batch.new_dag_ids_json, [])
        pause_results: list[dict[str, Any]] = []
        if new_dags and airflow.available:
            client = AirflowClient(airflow.endpoint, username=airflow.username, password=airflow.password)
            try:
                for dag_id in new_dags:
                    try:
                        client.pause_dag(dag_id)
                        pause_results.append({"dag_id": dag_id, "status": "paused"})
                    except AirflowError as exc:
                        pause_results.append({"dag_id": dag_id, "status": "failed", "error": str(exc)})
            finally:
                client.close()
        deployment = self._current_deployment(db, batch)
        for projection in db.query(WarehouseObjectProjection).filter(WarehouseObjectProjection.deployment_id == deployment.id):
            projection.queryable = False
        for projection in db.query(WarehouseLogicProjection).filter(WarehouseLogicProjection.deployment_id == deployment.id):
            projection.queryable = False
        batch.status = "rolled_back"
        batch.rolled_back_at = _now()
        batch.blocked_reason = reason.strip()
        batch.approval_note = _dumps({"original": batch.approval_note, "rollback_operator": operator, "new_dag_pause": pause_results})
        db.commit()
        db.refresh(batch)
        return batch

    @staticmethod
    def _latest_evidence(db: Session, batch_id: str) -> dict[int, WarehouseMigrationEvidence]:
        rows = db.query(WarehouseMigrationEvidence).filter(
            WarehouseMigrationEvidence.batch_id == batch_id
        ).order_by(WarehouseMigrationEvidence.step, WarehouseMigrationEvidence.attempt).all()
        return {row.step: row for row in rows}

    def serialize(self, db: Session, batch: WarehouseMigrationBatch) -> dict[str, Any]:
        evidence = db.query(WarehouseMigrationEvidence).filter(
            WarehouseMigrationEvidence.batch_id == batch.id
        ).order_by(WarehouseMigrationEvidence.recorded_at, WarehouseMigrationEvidence.step).all()
        return {
            "id": batch.id,
            "ontology_id": batch.ontology_id,
            "ontology_version": batch.ontology_version,
            "doris_datasource_id": batch.doris_datasource_id,
            "status": batch.status,
            "current_step": batch.current_step,
            "next_step": batch.current_step + 1 if batch.current_step < 15 else None,
            "approver": batch.approver,
            "rollback_owner": batch.rollback_owner,
            "observation_window_minutes": batch.observation_window_minutes,
            "legacy_dag_ids": _loads(batch.legacy_dag_ids_json, []),
            "new_dag_ids": _loads(batch.new_dag_ids_json, []),
            "blocked_reason": batch.blocked_reason,
            "approved_by": batch.approved_by,
            "approval_note": batch.approval_note,
            "approved_at": batch.approved_at,
            "cutover_at": batch.cutover_at,
            "observation_ends_at": batch.observation_ends_at,
            "legacy_stopped_at": batch.legacy_stopped_at,
            "completed_at": batch.completed_at,
            "rolled_back_at": batch.rolled_back_at,
            "rollback_drill": _loads(batch.rollback_drill_json, None),
            "compatibility_items": _loads(batch.compatibility_items_json, []),
            "created_by": batch.created_by,
            "created_at": batch.created_at,
            "updated_at": batch.updated_at,
            "timeline": [
                {
                    "step": row.step,
                    "name": STEP_NAMES[row.step],
                    "attempt": row.attempt,
                    "status": row.status,
                    "report": _loads(row.report_json, {}),
                    "artifact_ids": _loads(row.artifact_ids_json, []),
                    "checksum": row.checksum,
                    "recorded_by": row.recorded_by,
                    "recorded_at": row.recorded_at,
                }
                for row in evidence
            ],
        }

    def final_report(self, db: Session, batch_id: str) -> dict[str, Any]:
        batch = self._batch(db, batch_id)
        data = self.serialize(db, batch)
        latest = {item["step"]: item for item in data["timeline"]}
        deployment = None
        if batch.doris_datasource_id:
            deployment = db.query(OntologyWarehouseDeployment).filter(
                OntologyWarehouseDeployment.ontology_id == batch.ontology_id,
                OntologyWarehouseDeployment.ontology_version == batch.ontology_version,
                OntologyWarehouseDeployment.doris_datasource_id == batch.doris_datasource_id,
            ).first()
        contracts = db.query(IngestionContract).filter(
            IngestionContract.ontology_id == batch.ontology_id,
            IngestionContract.ontology_version == batch.ontology_version,
        ).all()
        artifacts = db.query(GovernanceArtifact).filter(
            GovernanceArtifact.ontology_id == batch.ontology_id
        ).all()
        return {
            "batch": {k: data[k] for k in ("id", "ontology_id", "ontology_version", "status", "current_step", "approver", "rollback_owner", "observation_window_minutes")},
            "timeline": data["timeline"],
            "reconciliation_report": (latest.get(7) or {}).get("report"),
            "shadow_query_difference_report": (latest.get(9) or {}).get("report"),
            "final_state": {
                "airflow": (latest.get(11) or {}).get("report", {}).get("airflow"),
                "flink": {
                    "contracts": len(contracts),
                    "running_cdc": sum(c.mode == "cdc" and c.status == "running" for c in contracts),
                    "failed": sum(c.status in {"failed", "stale"} for c in contracts),
                },
                "doris": {
                    "datasource_id": batch.doris_datasource_id,
                    "deployment_id": deployment.id if deployment else None,
                    "deployment_status": deployment.status if deployment else None,
                },
            },
            "rollback_drill": data["rollback_drill"],
            "remaining_compatibility_items": data["compatibility_items"],
            "full_test_results": (latest.get(15) or {}).get("report", {}).get("test_results"),
            "execution_matrix": (latest.get(15) or {}).get("report", {}).get("execution_matrix"),
            "artifact_audit": {
                "count": len(artifacts),
                "successful": sum(a.status == "succeeded" for a in artifacts),
                "receipts": sum(bool(a.execution_receipt_json) for a in artifacts),
                "historical_records_mutated": False,
            },
            "cutover_allowed": batch.current_step >= 10 and batch.status not in {"blocked", "rolled_back", "aborted"},
        }


def cutover_error(db: Session, ontology_ids: list[str]) -> str | None:
    """Block user-visible results while an ontology has an active Phase 6 batch.

    Deployments that have not entered Phase 6 keep their existing readiness
    semantics. Once a migration batch exists, however, shadow results remain
    hidden until that exact ontology version has an approved step-10 cut-over.
    """
    for ontology_id in dict.fromkeys(ontology_ids):
        ontology = db.get(Ontology, ontology_id)
        if ontology is None:
            return f"本体 {ontology_id} 不存在"
        rows = db.query(WarehouseMigrationBatch).filter(
            WarehouseMigrationBatch.ontology_id == ontology_id,
            WarehouseMigrationBatch.ontology_version == ontology.version,
        ).order_by(WarehouseMigrationBatch.created_at.desc()).all()
        if not rows:
            continue
        batch = rows[0]
        visible = (
            batch.current_step >= 10
            and batch.approved_at is not None
            and batch.status in {"cutover", "observing", "legacy_stopped", "cleaning", "completed"}
        )
        if not visible:
            return f"本体 {ontology_id} 尚未完成 Phase 6 审批切流；shadow 结果不得返回最终用户"
    return None


def shadow_difference_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare shadow results without retaining or returning business rows."""
    summaries: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        name = str(case.get("name") or f"case-{index}")
        legacy = case.get("legacy_result")
        doris = case.get("doris_result")
        legacy_hash = hashlib.sha256(_dumps(legacy).encode()).hexdigest()
        doris_hash = hashlib.sha256(_dumps(doris).encode()).hexdigest()
        summaries.append({
            "name": name,
            "matched": legacy_hash == doris_hash,
            "legacy_hash": legacy_hash,
            "doris_hash": doris_hash,
            "legacy_count": len(legacy) if isinstance(legacy, list) else None,
            "doris_count": len(doris) if isinstance(doris, list) else None,
        })
    different = sum(not item["matched"] for item in summaries)
    return {
        "cases": len(summaries),
        "matched": len(summaries) - different,
        "different": different,
        "blocking_differences": [item["name"] for item in summaries if not item["matched"]],
        "case_hashes": summaries,
        "raw_results_retained": False,
        "user_visible": False,
    }


def runtime_compatibility_inventory() -> dict[str, list[str]]:
    """Static Phase 6 architecture audit; history-only adapters are non-blocking."""
    root = Path(__file__).resolve().parents[1]
    checks = {
        "agent_list_catalogs": (root / "services/chat_bi_tool_schemas.py", '"name": "list_catalogs"'),
        "agent_run_sql_target": (root / "services/chat_bi_tool_schemas.py", '"target": {'),
        "transform_flink_import": (root / "agents/executors/transform.py", "flink_job_runner"),
        "metric_flink_import": (root / "agents/executors/metric.py", "flink_job_runner"),
        "materialize_legacy_runtime": (root / "services/materialization_runner.py", "Legacy rows without purpose=warehouse remain executable"),
    }
    blocking: list[str] = []
    for label, (path, token) in checks.items():
        if path.exists() and token in path.read_text(encoding="utf-8"):
            blocking.append(label)
    return {
        "blocking": blocking,
        "remaining": [
            "Hive/StarRocks dialect adapters retained for historical Artifact/receipt rendering only",
            "historical GovernanceArtifact and execution_receipt_json retained immutable/read-only",
        ],
    }
