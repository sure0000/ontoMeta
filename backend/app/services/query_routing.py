"""Doris-only query routing, ontology-version and Projection readiness gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.warehouse.policy import WAREHOUSE_ENGINE, require_doris_datasource


@dataclass(frozen=True)
class QueryTarget:
    datasource: DataSource
    deployment: OntologyWarehouseDeployment | None


class QueryRoutingError(ValueError):
    """A query cannot be proven to target ready Doris projections."""


def resolve_default_doris(db: Session) -> DataSource | None:
    """Return exactly one enabled configured default Doris, otherwise None."""
    rows = (
        db.query(DataSource)
        .filter(
            DataSource.purpose == "warehouse",
            DataSource.kind == WAREHOUSE_ENGINE,
            DataSource.is_default_warehouse.is_(True),
            DataSource.enabled.is_(True),
        )
        .all()
    )
    if len(rows) != 1 or not (rows[0].dsn_secret_ref or "").strip():
        return None
    try:
        require_doris_datasource(rows[0], operation="查询")
    except ValueError:
        return None
    return rows[0]


def _deployments(
    db: Session, *, datasource: DataSource, ontology_ids: list[str]
) -> tuple[dict[str, OntologyWarehouseDeployment], str | None]:
    deployments: dict[str, OntologyWarehouseDeployment] = {}
    for ontology_id in dict.fromkeys(ontology_ids):
        ontology = db.get(Ontology, ontology_id)
        if ontology is None:
            return {}, f"本体 {ontology_id} 不存在"
        if ontology.status != "published":
            return {}, f"本体 {ontology_id} 尚未发布"
        rows = (
            db.query(OntologyWarehouseDeployment)
            .filter(
                OntologyWarehouseDeployment.ontology_id == ontology.id,
                OntologyWarehouseDeployment.ontology_version == ontology.version,
                OntologyWarehouseDeployment.doris_datasource_id == datasource.id,
                OntologyWarehouseDeployment.status.in_(("schema_ready", "ready")),
            )
            .all()
        )
        if len(rows) != 1:
            return {}, f"本体 {ontology_id} 当前版本的 Doris Deployment 尚未唯一就绪"
        deployments[ontology_id] = rows[0]
    return deployments, None


def owning_ontology_ids(
    db: Session, *, ontology_ids: list[str], object_names: list[str]
) -> list[str]:
    """在场本体里，真正拥有被引用对象的那些。

    会话可以不选域（「全域通盘」），此时在场本体是**全部**已发布本体。若就绪校验对
    在场本体一视同仁地要求 Doris Deployment，那么只要任何一个域还没建仓，跨域会话里
    的**任何** SQL 都查不了——哪怕它只碰了一张早已就绪的表。实测踩到的就是这条：
    一个未绑定域的会话把一个 2 对象的测试域也算进在场本体，于是 erpnext 里就绪多时的
    客户分组被判「未就绪」。

    收窄到「这条 SQL 真的碰了的本体」。解析不到任何归属时退回原列表，让下游按老路
    报「对象未覆盖」，而不是在这里静默放行。
    """
    wanted = {str(n).strip().lower() for n in object_names if str(n).strip()}
    if not wanted or not ontology_ids:
        return list(ontology_ids)
    scoped = list(dict.fromkeys(ontology_ids))
    owners = {
        obj.ontology_id
        for obj in db.query(ObjectType)
        .filter(
            ObjectType.ontology_id.in_(scoped),
            ObjectType.status == "published",
        )
        .all()
        if (obj.name or "").strip().lower() in wanted
    }
    return [oid for oid in scoped if oid in owners] or scoped


def readiness_error(
    db: Session,
    *,
    datasource: DataSource,
    ontology_ids: list[str] | None,
    object_names: list[str] | None = None,
) -> str | None:
    """Return a fail-closed reason, including per-referenced-object coverage."""
    if datasource.kind != WAREHOUSE_ENGINE or datasource.purpose != "warehouse":
        return "查询目标不是 Doris warehouse"
    if not datasource.is_default_warehouse or not datasource.enabled:
        return "查询目标不是启用的默认 Doris"
    if not ontology_ids:
        return "查询未绑定已发布本体，禁止执行 SQL"

    scoped = (
        owning_ontology_ids(db, ontology_ids=ontology_ids, object_names=object_names)
        if object_names
        else list(ontology_ids)
    )
    deployments, error = _deployments(
        db, datasource=datasource, ontology_ids=scoped
    )
    if error:
        return error
    if object_names is None:
        return None
    try:
        projection_mapping(
            db,
            datasource=datasource,
            ontology_ids=scoped,
            object_names=object_names,
            deployments=deployments,
        )
    except QueryRoutingError as exc:
        return str(exc)
    return None


def referenced_table_names(sql: str) -> list[str]:
    """Extract logical table tokens; parsing failure is a routing failure."""
    try:
        tree = sqlglot.parse_one(sql, read="doris")
    except Exception as exc:  # noqa: BLE001
        raise QueryRoutingError(f"SQL 无法按 Doris 方言解析：{exc}") from exc
    if tree is None:
        raise QueryRoutingError("SQL 为空或无法解析")
    names = sorted({table.name for table in tree.find_all(exp.Table) if table.name})
    if not names:
        raise QueryRoutingError("SQL 未引用任何本体对象")
    return names


def projection_mapping(
    db: Session,
    *,
    datasource: DataSource,
    ontology_ids: list[str],
    object_names: list[str],
    deployments: dict[str, OntologyWarehouseDeployment] | None = None,
) -> dict[str, Any]:
    """Build the only authoritative logical→physical mapping for a query.

    Every table proven by the SQL soundness certificate must resolve to exactly
    one current-version, queryable Projection. ``DataSource.mapping_json`` is
    intentionally ignored: it is historical metadata, not a query authority.
    """
    deployments = deployments or _deployments(
        db,
        datasource=datasource,
        # 与 readiness_error 同口径：只要求这条 SQL 真的碰了的本体已建仓，
        # 否则跨域会话里一个未建仓的域会连坐所有查询（见 owning_ontology_ids）。
        ontology_ids=owning_ontology_ids(
            db, ontology_ids=ontology_ids, object_names=object_names
        ),
    )[0]
    wanted = {str(name).strip().lower() for name in object_names if str(name).strip()}
    if not wanted:
        raise QueryRoutingError("SQL 未引用可解析的本体对象")

    candidates: dict[str, list[tuple[ObjectType, WarehouseObjectProjection]]] = {
        name: [] for name in wanted
    }
    for ontology_id, deployment in deployments.items():
        objects = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.status == "published",
            )
            .all()
        )
        by_id = {obj.id: obj for obj in objects}
        for projection in (
            db.query(WarehouseObjectProjection)
            .filter(WarehouseObjectProjection.deployment_id == deployment.id)
            .all()
        ):
            obj = by_id.get(projection.object_type_id)
            if obj and obj.name.strip().lower() in wanted:
                candidates[obj.name.strip().lower()].append((obj, projection))

    tables: dict[str, str] = {}
    columns: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    for logical in sorted(wanted):
        rows = candidates.get(logical) or []
        if len(rows) != 1:
            raise QueryRoutingError(
                f"对象 {logical} 的当前版本 Doris Projection 数量为 {len(rows)}，要求恰好 1 个"
            )
        obj, projection = rows[0]
        if projection.schema_status != "ready":
            raise QueryRoutingError(f"对象 {obj.name} 的 Doris schema 尚未 ready")
        if not projection.queryable:
            raise QueryRoutingError(f"对象 {obj.name} 尚未同步或加工完成，不可查询")
        # Default freshness policy is strict: stale never executes. A future
        # explicit warn policy may relax this, but there is no silent fallback.
        if projection.sync_status != "ready":
            raise QueryRoutingError(
                f"对象 {obj.name} 的同步状态为 {projection.sync_status}，不可查询"
            )
        if projection.transform_status not in {"not_required", "ready"}:
            raise QueryRoutingError(
                f"对象 {obj.name} 的加工状态为 {projection.transform_status}，不可查询"
            )
        if not projection.serving_database or not projection.serving_table:
            raise QueryRoutingError(f"对象 {obj.name} 缺少 serving_database/serving_table")
        physical = f"{projection.serving_database}.{projection.serving_table}"
        tables[obj.name] = physical
        try:
            column_map = json.loads(projection.column_mapping_json or "{}")
        except (TypeError, ValueError):
            raise QueryRoutingError(f"对象 {obj.name} 的字段映射不是合法 JSON") from None
        if column_map and not isinstance(column_map, dict):
            raise QueryRoutingError(f"对象 {obj.name} 的字段映射必须是对象")
        # Existing executor accepts a flat map. Require consistency if two
        # objects use the same logical property name with different physical names.
        for source, target in (column_map or {}).items():
            if source in columns and columns[source] != target:
                raise QueryRoutingError(f"字段 {source} 的跨对象物理映射冲突")
            columns[str(source)] = str(target)
        evidence.append(
            {
                "object_type_id": obj.id,
                "logical_table": obj.name,
                "physical_table": physical,
                "projection_id": projection.id,
                "last_sync_at": (
                    projection.last_sync_at.isoformat()
                    if projection.last_sync_at else None
                ),
                "sync_watermark": projection.sync_watermark,
                "stale": (
                    projection.sync_status == "stale"
                    or projection.transform_status == "stale"
                ),
            }
        )
    return {"tables": tables, "columns": columns, "projections": evidence}


def target_receipt(
    db: Session,
    *,
    datasource: DataSource,
    ontology_ids: list[str] | None,
    object_names: list[str] | None = None,
) -> dict:
    """Credential-free target evidence for query receipts."""
    deployments, _ = _deployments(
        db, datasource=datasource, ontology_ids=ontology_ids or []
    ) if ontology_ids else ({}, None)
    projection_evidence: list[dict[str, Any]] = []
    if object_names and ontology_ids:
        projection_evidence = projection_mapping(
            db,
            datasource=datasource,
            ontology_ids=ontology_ids,
            object_names=object_names,
            deployments=deployments,
        )["projections"]
    return {
        "engine": WAREHOUSE_ENGINE,
        "datasource_id": datasource.id,
        "datasource_name": datasource.name,
        "deployments": [
            {
                "deployment_id": deployment.id,
                "ontology_id": deployment.ontology_id,
                "ontology_version": deployment.ontology_version,
                "status": deployment.status,
            }
            for deployment in deployments.values()
        ],
        "physical_tables": [p["physical_table"] for p in projection_evidence],
        "projections": projection_evidence,
    }
