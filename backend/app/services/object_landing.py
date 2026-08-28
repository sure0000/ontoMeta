"""本体实体的**物理落点**读模型：这个对象/口径落到哪张物理表了。

任务产出的表从来不是「无主的新表」——四类任务的落点在起草期就绑死在本体实体上：

- 物化：``doris_deployment.ensure_deployment`` 按本体建 ``WarehouseObjectProjection``，
  写死 ODS 落点（``ods_naming``）与服务层落点（层/库/表）。
- 同步：``IngestionContract`` 记 (本体, 版本, 对象) → ``ods.ods_{域}_{原表}``。
- 清洗：目标必须是**已存在**的 ``ObjectType.name``（见 ``executors/transform``），
  推进的是同一条 Projection 的 ``transform_status``。
- 指标：``WarehouseLogicProjection`` 挂在 BusinessLogic 上（ADS 表是口径的物化，
  **不是**业务对象——不要为它造 ObjectType）。

登记行一直在写，只是没有任何读侧把它们喂回本体工作区，于是「任务建的表在建模里看不见」。
本模块就是那个读侧：只聚合、不写库、不调 LLM。**新表永远不重新建模，只登记为落点。**

两套登记并存是历史事实，不是 bug：同步走 ``IngestionContract``（它才有 mode/水位/
最近成功时间），物化/清洗走 ``WarehouseObjectProjection``。故这里的口径是
**契约优先、Projection 补位**，缺的字段互相填，而不是再造第三张表去做双写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    IngestionContract,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)

# 汇总态。前端只认这一列，判定逻辑留在后端——放到前端就会有第二份口径。
NOT_LANDED = "not_landed"  # 没有任何登记：本体里有这个对象，但它还没落成物理表
REGISTERED = "registered"  # 落点已登记（起草了任务/契约），但表未必建、数肯定没搬
SCHEMA_READY = "schema_ready"  # 表已建好（物化过），但还没搬过数
SYNCING = "syncing"  # 搬运/加工在跑
LANDED = "landed"  # 已落地可用
STALE = "stale"  # 落过，但上游变更后未重跑
FAILED = "failed"  # 最近一次落地失败

_RUNNING = {"running", "syncing", "submitted", "queued", "scheduled"}


@dataclass(frozen=True)
class ObjectLanding:
    """一个业务对象的物理落点快照。"""

    state: str
    ods_table: str | None = None  # 库.表
    ods_status: str | None = None
    ods_mode: str | None = None  # full / incremental / cdc
    serving_table: str | None = None  # 库.表
    serving_layer: str | None = None
    serving_status: str | None = None
    schema_status: str | None = None
    queryable: bool = False
    last_success_at: datetime | None = None
    materialization_artifact_id: str | None = None


@dataclass(frozen=True)
class LogicLanding:
    """一条业务口径（指标/标签/规则）的 ADS 落点快照。"""

    state: str
    serving_table: str | None = None
    status: str | None = None
    queryable: bool = False
    last_success_at: datetime | None = None


def qualified_table(database: str | None, table: str | None) -> str | None:
    """``库.表``；表名缺失时返回 None——半个名字比没有名字更坏。

    目录（``dataset_catalog``）判「这张表被谁认领了」也要按同一规则拼名字：两处各拼一次，
    带库名与不带库名就会被当成两张不同的表，无主表清单里于是冒出已经有主的表。
    """
    if not table:
        return None
    return f"{database}.{table}" if database else table


def derive_landing_state(
    *,
    ods_status: str | None,
    serving_status: str | None,
    schema_status: str | None,
    has_target: bool,
) -> str:
    """由各分项状态汇总出一个字。

    优先级：失败 > 在跑 > 陈旧 > 就绪 > 已建表 > 已登记 > 未落地。坏消息压过好消息是
    刻意的——「ODS 就绪但清洗失败」在界面上必须显示成失败，不能被 ODS 的绿色盖住。

    「表已建」与「已登记」分开：前者有物化过的实表（``schema_status=ready``），后者只是
    契约/Projection 记下了落点。合成一个状态会让「刚起草完同步、什么都还没跑」的对象
    对外宣称表已经建好了。

    **单个落点也走这里**：``dataset_catalog`` 给 ODS 槽位只传 ``ods_status``、给服务层
    槽位只传 ``serving_status``/``schema_status``，于是「一张表现在能不能用」在全仓只有
    这一处判定。目录若自己写一套，「ODS 就绪但清洗失败」在两个界面就会给出两种说法。
    """
    parts = [s for s in (ods_status, serving_status) if s]
    if any(s == "failed" for s in parts):
        return FAILED
    if any(s in _RUNNING for s in parts):
        return SYNCING
    if any(s == "stale" for s in parts):
        return STALE
    if any(s == "ready" for s in parts):
        return LANDED
    if schema_status == "ready":
        return SCHEMA_READY
    if has_target:
        return REGISTERED
    return NOT_LANDED


def _latest_contracts(
    db: Session, object_ids: list[str]
) -> dict[str, IngestionContract]:
    """每个对象取**本体版本最高**的那条接入契约。

    契约按 (本体, 版本, 对象) 唯一，跨版本会留下历史行；落点要显示的是当前那一条。
    """
    latest: dict[str, IngestionContract] = {}
    rows = (
        db.query(IngestionContract)
        .filter(IngestionContract.object_type_id.in_(object_ids))
        .all()
    )
    for row in rows:
        current = latest.get(row.object_type_id)
        if current is None or (row.ontology_version or 0) >= (
            current.ontology_version or 0
        ):
            latest[row.object_type_id] = row
    return latest


def _latest_projections(
    db: Session, object_ids: list[str]
) -> dict[str, tuple[WarehouseObjectProjection, OntologyWarehouseDeployment]]:
    """每个对象取**部署版本最高**的那条 Projection。"""
    latest: dict[str, tuple[WarehouseObjectProjection, OntologyWarehouseDeployment]] = {}
    rows = (
        db.query(WarehouseObjectProjection, OntologyWarehouseDeployment)
        .join(
            OntologyWarehouseDeployment,
            WarehouseObjectProjection.deployment_id == OntologyWarehouseDeployment.id,
        )
        .filter(WarehouseObjectProjection.object_type_id.in_(object_ids))
        .all()
    )
    for projection, deployment in rows:
        current = latest.get(projection.object_type_id)
        if current is None or (deployment.ontology_version or 0) >= (
            current[1].ontology_version or 0
        ):
            latest[projection.object_type_id] = (projection, deployment)
    return latest


def bulk_object_landings(
    db: Session, object_ids: list[str]
) -> dict[str, ObjectLanding]:
    """批量返回 object_type_id -> 落点。**未登记的对象不出现在结果里。**

    列表页按页调用（对象列表是分页的），故这里只做两次 ``IN`` 查询，不逐对象查。
    """
    ids = list(dict.fromkeys(object_ids))
    if not ids:
        return {}
    contracts = _latest_contracts(db, ids)
    projections = _latest_projections(db, ids)

    landings: dict[str, ObjectLanding] = {}
    for oid in ids:
        contract = contracts.get(oid)
        pair = projections.get(oid)
        projection, deployment = pair if pair else (None, None)
        if contract is None and projection is None:
            continue

        # ODS 落点：契约优先（只有它有 mode 与真实的最近成功时间），
        # 没有契约时退到 Projection 里由物化写下的确定性落点。
        ods_table = ods_status = ods_mode = None
        last_success_at: datetime | None = None
        if contract is not None:
            ods_table = qualified_table(
                contract.target_ods_database, contract.target_ods_table
            )
            ods_status = contract.status
            ods_mode = contract.mode
            last_success_at = contract.last_success_at
        if ods_table is None and projection is not None:
            ods_table = qualified_table(projection.ods_database, projection.ods_table)
            ods_status = ods_status or projection.sync_status
        if last_success_at is None and projection is not None:
            last_success_at = projection.last_sync_at

        serving_table = serving_layer = serving_status = schema_status = None
        queryable = False
        if projection is not None:
            serving_table = qualified_table(
                projection.serving_database, projection.serving_table
            )
            serving_layer = projection.serving_layer
            # not_required 是「这个对象不需要清洗」，不是一个落点状态；别让它参与汇总，
            # 否则物化完成的对象会被判成「有个非 ready 的加工步骤」而显示不出已落地。
            serving_status = (
                projection.transform_status
                if projection.transform_status != "not_required"
                else None
            )
            schema_status = projection.schema_status
            queryable = bool(projection.queryable)

        landings[oid] = ObjectLanding(
            state=derive_landing_state(
                ods_status=ods_status,
                serving_status=serving_status,
                schema_status=schema_status,
                has_target=bool(ods_table or serving_table),
            ),
            ods_table=ods_table,
            ods_status=ods_status,
            ods_mode=ods_mode,
            serving_table=serving_table,
            serving_layer=serving_layer,
            serving_status=serving_status,
            schema_status=schema_status,
            queryable=queryable,
            last_success_at=last_success_at,
            materialization_artifact_id=(
                deployment.materialization_artifact_id if deployment else None
            ),
        )
    return landings


def object_landing(db: Session, object_id: str) -> ObjectLanding | None:
    """单个对象的落点。未登记时返回 ``None``（而不是一个假的「未落地」）。"""
    return bulk_object_landings(db, [object_id]).get(object_id)


def bulk_logic_landings(db: Session, logic_ids: list[str]) -> dict[str, LogicLanding]:
    """批量返回 business_logic_id -> ADS 落点。

    指标任务产出的 ADS 表是这条口径的物化，挂在口径上；**不给它建业务对象**。
    """
    ids = list(dict.fromkeys(logic_ids))
    if not ids:
        return {}
    rows = (
        db.query(WarehouseLogicProjection, OntologyWarehouseDeployment)
        .join(
            OntologyWarehouseDeployment,
            WarehouseLogicProjection.deployment_id == OntologyWarehouseDeployment.id,
        )
        .filter(WarehouseLogicProjection.business_logic_id.in_(ids))
        .all()
    )
    latest: dict[str, tuple[WarehouseLogicProjection, OntologyWarehouseDeployment]] = {}
    for projection, deployment in rows:
        current = latest.get(projection.business_logic_id)
        if current is None or (deployment.ontology_version or 0) >= (
            current[1].ontology_version or 0
        ):
            latest[projection.business_logic_id] = (projection, deployment)

    return {
        logic_id: LogicLanding(
            state=derive_landing_state(
                ods_status=None,
                serving_status=projection.status,
                schema_status=None,
                has_target=bool(projection.serving_table),
            ),
            serving_table=qualified_table(
                projection.serving_database, projection.serving_table
            ),
            status=projection.status,
            queryable=bool(projection.queryable),
            last_success_at=projection.last_success_at,
        )
        for logic_id, (projection, _deployment) in latest.items()
    }


__all__ = [
    "FAILED",
    "LANDED",
    "NOT_LANDED",
    "REGISTERED",
    "SCHEMA_READY",
    "STALE",
    "SYNCING",
    "LogicLanding",
    "ObjectLanding",
    "bulk_logic_landings",
    "bulk_object_landings",
    "derive_landing_state",
    "qualified_table",
    "object_landing",
]
