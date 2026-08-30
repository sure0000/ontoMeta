"""Source-to-Doris ODS ingestion contract service."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.ods_naming import target_ods_table_name
from app.services.source_ref import source_platform_of
from app.warehouse.policy import require_doris_datasource

MODES = frozenset({"full", "incremental", "cdc"})
DELETE_POLICIES = frozenset({"ignore", "soft_delete", "hard_delete"})
CDC_PLATFORMS = frozenset({"mysql", "mariadb", "postgres", "postgresql"})


class IngestionContractError(ValueError):
    pass


# 契约状态 → Projection 同步态。两套登记并存是历史事实（同步写契约、物化/清洗写
# Projection），但**推进只能有一处**，否则「本体工作区显示已落地、清洗却说 ODS 没就绪」。
_PROJECTION_SYNC_STATUS = {
    "ready": "ready",
    "running": "syncing",
    "failed": "failed",
}


def mirror_contract_to_projection(
    db: Session, contract: IngestionContract
) -> WarehouseObjectProjection | None:
    """把接入契约的落数状态镜像到同版本对象 Projection。**不提交事务。**

    两条对账路径都得走这里：``IngestionContractService.reconcile_task_result``（仓库 API
    回传 Airflow task 结果）和 ``sync_reconciliation.reconcile_sync_receipt``（制品流水线
    按 DagRun 对账）。同步成功即完成源表对象的直接 serving：如果当前版本还没有
    Deployment/Projection，这里会用已验证的同步目标表创建它；如果已有独立 transform
    serving，则只更新 ODS 状态，由 transform 继续拥有查询权。
    """
    if contract.status == "ready":
        # Sync is the materialization boundary for source-backed objects.  Keep
        # this in the shared mirror path so API and artifact reconciliation agree.
        from app.services.doris_deployment import publish_direct_sync_ready

        return publish_direct_sync_ready(db, contract=contract)

    deployment = (
        db.query(OntologyWarehouseDeployment)
        .filter(
            OntologyWarehouseDeployment.ontology_id == contract.ontology_id,
            OntologyWarehouseDeployment.ontology_version == contract.ontology_version,
            OntologyWarehouseDeployment.doris_datasource_id == contract.doris_datasource_id,
        )
        .first()
    )
    if deployment is None:
        return None
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(
            WarehouseObjectProjection.deployment_id == deployment.id,
            WarehouseObjectProjection.object_type_id == contract.object_type_id,
        )
        .first()
    )
    if projection is None:
        return None

    sync_status = _PROJECTION_SYNC_STATUS.get(contract.status)
    if sync_status is None:
        # draft/submitted 等中间态不推进：没跑完的搬运不该改写上一次的落数结论。
        return projection
    projection.sync_status = sync_status
    if contract.status == "ready":
        projection.last_sync_at = contract.last_success_at
        projection.sync_watermark = contract.sync_watermark
    # A non-ready contract must never make an existing serving projection
    # queryable.  Ready contracts are handled above so direct sync can create
    # and align the serving target before this mirror runs.
    projection.queryable = False
    return projection


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _validate(
    db: Session,
    ontology: Ontology,
    data: dict[str, Any],
) -> tuple[ObjectType, DataSource, DataSource]:
    obj = db.get(ObjectType, data.get("object_type_id"))
    if obj is None or obj.ontology_id != ontology.id:
        raise IngestionContractError("接入对象不属于当前本体")
    source = db.get(DataSource, data.get("source_datasource_id"))
    if source is None or source.purpose != "business_source" or not source.enabled:
        raise IngestionContractError("源 DataSource 必须是启用的 business_source")
    from app.services.source_datasource import source_datasource_candidates

    allowed_sources = {candidate.id for candidate in source_datasource_candidates(db, obj)}
    if source.id not in allowed_sources:
        raise IngestionContractError(
            "源 DataSource 与本体 source_ref 的平台/库/表来源不匹配"
        )
    doris = db.get(DataSource, data.get("doris_datasource_id"))
    try:
        require_doris_datasource(doris, operation="Flink ODS 同步")
    except ValueError as exc:
        raise IngestionContractError(str(exc)) from exc
    if not doris.is_default_warehouse:
        raise IngestionContractError("Flink ODS 同步只能写默认 Doris")
    if not (doris.dsn_secret_ref or "").strip():
        raise IngestionContractError("默认 Doris 尚未配置连接，不能执行同步")

    mode = str(data.get("mode") or "full").lower()
    if mode not in MODES:
        raise IngestionContractError("mode 只能是 full / incremental / cdc")
    target_db = str(data.get("target_ods_database") or "").strip()
    if not target_db.startswith("ods"):
        raise IngestionContractError("同步目标数据库必须是 ODS（名称以 ods 开头）")
    # target_ods_table 不是调用方配置项；校验前按本体事实重新生成，传入任何自定义值都覆盖。
    try:
        data["target_ods_table"] = target_ods_table_name(db, ontology.id, obj)
    except ValueError as exc:
        raise IngestionContractError(str(exc)) from exc

    from app.models import Property

    known_columns = {
        name for (name,) in db.query(Property.name).filter(
            Property.object_type_id == obj.id
        ).all()
    }
    pks = [str(x).strip() for x in data.get("primary_keys") or [] if str(x).strip()]
    unknown = [name for name in pks if name not in known_columns]
    if unknown:
        raise IngestionContractError(
            f"primary_keys 不在本体字段中：{', '.join(unknown)}"
        )
    if mode in {"incremental", "cdc"} and not pks:
        raise IngestionContractError(f"{mode} 同步必须配置业务主键")
    if mode == "incremental":
        if not data.get("incremental_column"):
            raise IngestionContractError("incremental 同步必须配置增量字段")
        if data.get("incremental_column") not in known_columns:
            raise IngestionContractError("incremental_column 不在本体字段中")
        if data.get("initial_watermark") in (None, ""):
            raise IngestionContractError("incremental 同步必须配置 initial_watermark")
    if mode == "cdc":
        platform = (source_platform_of(obj.source_ref) or source.kind).lower()
        if platform not in CDC_PLATFORMS:
            raise IngestionContractError(f"源平台 {platform} 不支持 CDC")
        if not data.get("sequence_column"):
            raise IngestionContractError("CDC 必须配置 sequence_column")
        if data.get("sequence_column") not in known_columns:
            raise IngestionContractError("sequence_column 不在本体字段中")
        checkpoint = str((data.get("flink_params") or {}).get("flink_checkpoint_dir") or "")
        if not checkpoint:
            raise IngestionContractError("CDC 必须配置 flink_checkpoint_dir")
    delete_policy = str(data.get("delete_policy") or "ignore")
    if delete_policy not in DELETE_POLICIES:
        raise IngestionContractError(
            "delete_policy 只能是 ignore / soft_delete / hard_delete"
        )
    if delete_policy == "hard_delete" and mode != "cdc":
        raise IngestionContractError("hard_delete 只允许 CDC 契约")
    if delete_policy == "soft_delete":
        raise IngestionContractError(
            "soft_delete 需要显式 tombstone 列映射，当前能力未实现，拒绝静默降级"
        )
    return obj, source, doris


class IngestionContractService:
    def for_execution(
        self, db: Session, ontology_id: str, object_type_id: str
    ) -> IngestionContract | None:
        ontology = db.get(Ontology, ontology_id)
        if ontology is None:
            return None
        return (
            db.query(IngestionContract)
            .filter(
                IngestionContract.ontology_id == ontology.id,
                IngestionContract.ontology_version == ontology.version,
                IngestionContract.object_type_id == object_type_id,
                IngestionContract.status.in_(("active", "draft")),
            )
            .first()
        )

    def list(self, db: Session, ontology_id: str) -> list[IngestionContract]:
        return (
            db.query(IngestionContract)
            .filter(IngestionContract.ontology_id == ontology_id)
            .order_by(IngestionContract.target_ods_database, IngestionContract.target_ods_table)
            .all()
        )

    def upsert(
        self, db: Session, ontology_id: str, data: dict[str, Any]
    ) -> IngestionContract:
        ontology = db.get(Ontology, ontology_id)
        if ontology is None:
            raise IngestionContractError("本体不存在")
        obj, _source, doris = _validate(db, ontology, data)
        contract = (
            db.query(IngestionContract)
            .filter(
                IngestionContract.ontology_id == ontology.id,
                IngestionContract.ontology_version == ontology.version,
                IngestionContract.object_type_id == obj.id,
            )
            .first()
        )
        if contract is None:
            contract = IngestionContract(
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                object_type_id=obj.id,
            )
            db.add(contract)
        contract.source_datasource_id = str(data["source_datasource_id"])
        contract.source_physical_table = str(data["source_physical_table"])
        contract.source_mapping_json = _dump(data.get("source_mapping") or {})
        contract.doris_datasource_id = doris.id
        contract.target_ods_database = str(data["target_ods_database"])
        contract.target_ods_table = str(data["target_ods_table"])
        contract.mode = str(data.get("mode") or "full").lower()
        contract.primary_keys_json = _dump(data.get("primary_keys") or [])
        contract.sequence_column = data.get("sequence_column")
        contract.incremental_column = data.get("incremental_column")
        contract.initial_watermark = data.get("initial_watermark")
        contract.late_arrival_policy = str(data.get("late_arrival_policy") or "strict")
        contract.idempotency_strategy = str(
            data.get("idempotency_strategy") or "primary_key_upsert"
        )
        contract.delete_policy = str(data.get("delete_policy") or "ignore")
        contract.refresh_cron = data.get("refresh_cron")
        contract.flink_params_json = _dump(data.get("flink_params") or {})
        contract.status = str(data.get("status") or "draft")
        contract.checkpoint_path = (data.get("flink_params") or {}).get(
            "flink_checkpoint_dir"
        )
        db.commit()
        db.refresh(contract)
        return contract

    def reconcile_task_result(
        self,
        db: Session,
        contract_id: str,
        *,
        task_state: str | None,
        result: dict[str, Any] | None,
    ) -> IngestionContract:
        """Advance ingestion/projection state from an Airflow task final state.

        Submission is not success. Batch readiness requires task=success; CDC
        requires a real Flink job id and remains running rather than ready.
        """
        contract = db.get(IngestionContract, contract_id)
        if contract is None:
            raise IngestionContractError("IngestionContract 不存在")
        state = (task_state or "").lower()
        payload = result or {}
        if state in {"failed", "upstream_failed"}:
            contract.status = "failed"
            # 失败也要落到 Projection：否则上一次成功留下的 ready 会让工作区显示「已落地」、
            # 让 transform 从一张没搬成的表上继续加工。
            mirror_contract_to_projection(db, contract)
            db.commit()
            db.refresh(contract)
            return contract
        if state != "success":
            return contract

        if contract.mode == "cdc":
            job_id = str(payload.get("flink_job_id") or payload.get("job_id") or "").strip()
            if not job_id:
                raise IngestionContractError(
                    "CDC task 已成功但没有真实 flink_job_id，拒绝推进 running"
                )
            contract.flink_job_id = job_id
            contract.checkpoint_path = payload.get("checkpoint_path") or contract.checkpoint_path
            contract.savepoint_path = payload.get("savepoint_path") or contract.savepoint_path
            contract.status = "running"
        else:
            contract.status = "ready"
            contract.last_success_at = datetime.now(timezone.utc).replace(tzinfo=None)
            next_watermark = payload.get("watermark_after")
            if contract.mode == "incremental" and next_watermark not in (None, ""):
                contract.sync_watermark = str(next_watermark)

        mirror_contract_to_projection(db, contract)
        db.commit()
        db.refresh(contract)
        return contract

    @staticmethod
    def serialize(contract: IngestionContract) -> dict[str, Any]:
        return {
            "id": contract.id,
            "ontology_id": contract.ontology_id,
            "ontology_version": contract.ontology_version,
            "object_type_id": contract.object_type_id,
            "source_datasource_id": contract.source_datasource_id,
            "source_physical_table": contract.source_physical_table,
            "source_mapping": _load(contract.source_mapping_json, {}),
            "doris_datasource_id": contract.doris_datasource_id,
            "target_ods_database": contract.target_ods_database,
            "target_ods_table": contract.target_ods_table,
            "mode": contract.mode,
            "primary_keys": _load(contract.primary_keys_json, []),
            "sequence_column": contract.sequence_column,
            "incremental_column": contract.incremental_column,
            "initial_watermark": contract.initial_watermark,
            "late_arrival_policy": contract.late_arrival_policy,
            "idempotency_strategy": contract.idempotency_strategy,
            "delete_policy": contract.delete_policy,
            "refresh_cron": contract.refresh_cron,
            "flink_params": _load(contract.flink_params_json, {}),
            "status": contract.status,
            "last_success_at": contract.last_success_at,
            "sync_watermark": contract.sync_watermark,
            "flink_job_id": contract.flink_job_id,
            "checkpoint_path": contract.checkpoint_path,
            "savepoint_path": contract.savepoint_path,
            "created_at": contract.created_at,
            "updated_at": contract.updated_at,
        }
