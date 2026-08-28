"""无主表：数仓里存在、本体里没人认领的那些表。

前三步（数据集目录 / 派生对象 / 多源加工）解的都是「本体认领过的表怎么用」。剩下的那类
是反过来的——**表先在那儿，本体里却没有任何东西对应它**：

- 对象被人工删掉了，物化/同步给它建的表还在（登记行成了孤儿，见
  ``scripts/check_landing_orphans``）；
- ontoMeta 接管之前就存在的表、别的团队建的表；
- 手工建的临时表。

对这类表只给**两个**动作：认领为某个已有实体的落点，或者不管它。**不给「自动建对象」**
——照着物理表反推出来的对象正是重复对象的来源（见 ``unmodeled_tables``、
``derived_object``）。要建模就走建模，不要让扫描结果直接变成本体成员。

**「无主」只在自己管的库里才立得住。** 默认只扫本体自己写过的库（ODS + 这个本体落点所在
的服务层库）：对整个数仓宣称「这些表无主」是越权——那些表可能属于别的域、别的系统。要看
别的库，调用方显式传 ``database``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    ObjectType,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)
from app.models.warehouse import MaterializationLayer
from app.services import dataset_catalog
from app.services.data_app_executor import ExecutionError, list_tables
from app.services.object_landing import qualified_table
from app.services.ods_naming import ODS_DATABASE

logger = logging.getLogger("ontometa.unclaimed_tables")

_LAYERS = tuple(layer.value for layer in MaterializationLayer)


class UnclaimedTableError(ValueError):
    """无主表清点/认领不成立。调用方（API）转 400，不是 500。"""


@dataclass(frozen=True)
class UnclaimedTable:
    database: str
    table: str
    layer: str | None  # 由库名推断（dwd / dwd_erp → dwd）；推不出就是 None，不猜

    @property
    def physical(self) -> str:
        return f"{self.database}.{self.table}"


def layer_of_database(database: str) -> str | None:
    """库名 → 分层。``dwd`` / ``dwd_erp`` → ``dwd``；``ods`` → ``ods``；其它 → None。

    推不出来就给 None 而不是塞一个默认层：层会写进 Projection，猜错的话这张表在工作区里
    会挂在错误的分层下，而没有任何地方会说这是猜的。
    """
    name = (database or "").strip().lower()
    if name == ODS_DATABASE:
        return ODS_DATABASE
    head = name.split("_", 1)[0]
    return head if head in _LAYERS else None


def managed_databases(db: Session, ontology_id: str) -> list[str]:
    """这个本体自己写过的库：ODS + 它的落点所在的服务层库。"""
    databases = {ODS_DATABASE}
    deployment_ids = [
        row[0]
        for row in db.query(OntologyWarehouseDeployment.id)
        .filter(OntologyWarehouseDeployment.ontology_id == ontology_id)
        .all()
    ]
    if deployment_ids:
        for (database,) in (
            db.query(WarehouseObjectProjection.serving_database)
            .filter(WarehouseObjectProjection.deployment_id.in_(deployment_ids))
            .distinct()
            .all()
        ):
            if database:
                databases.add(database)
        for (database,) in (
            db.query(WarehouseLogicProjection.serving_database)
            .filter(WarehouseLogicProjection.deployment_id.in_(deployment_ids))
            .distinct()
            .all()
        ):
            if database:
                databases.add(database)
    return sorted(databases)


def _warehouse_datasource(db: Session, datasource_id: str | None) -> DataSource:
    if datasource_id:
        ds = db.get(DataSource, datasource_id)
    else:
        ds = (
            db.query(DataSource)
            .filter(
                DataSource.purpose == "warehouse",
                DataSource.is_default_warehouse.is_(True),
                DataSource.enabled.is_(True),
            )
            .first()
        )
    if ds is None:
        raise UnclaimedTableError("没有可用的默认数仓数据源，无法清点无主表")
    if not ds.dsn_secret_ref:
        raise UnclaimedTableError(f"数据源「{ds.name}」未配置连接串，无法清点无主表")
    return ds


def list_unclaimed_tables(
    db: Session,
    ontology_id: str,
    *,
    datasource_id: str | None = None,
    database: str | None = None,
) -> tuple[list[UnclaimedTable], list[str]]:
    """返回 (无主表清单, 实际扫过的库)。

    扫库是实时的：清单要反映数仓此刻的样子，缓存住只会让人认领一张刚被删掉的表。
    """
    ds = _warehouse_datasource(db, datasource_id)
    databases = [database] if database else managed_databases(db, ontology_id)
    claimed = dataset_catalog.claimed_physical_tables(db)

    unclaimed: list[UnclaimedTable] = []
    scanned: list[str] = []
    for name in databases:
        try:
            tables = list_tables(ds.dsn_secret_ref, name)
        except ExecutionError as exc:
            # 库不存在（还没物化过）不是错误：跳过并记一笔，不要让整份清单失败。
            logger.info("跳过库 %s：%s", name, exc)
            continue
        scanned.append(name)
        for table in tables:
            physical = qualified_table(name, table)
            if not physical or physical.lower() in claimed:
                continue
            unclaimed.append(
                UnclaimedTable(database=name, table=table, layer=layer_of_database(name))
            )
    unclaimed.sort(key=lambda t: (t.database.lower(), t.table.lower()))
    logger.info(
        "本体 %s 无主表 %d 张（扫描库：%s）",
        ontology_id,
        len(unclaimed),
        "、".join(scanned) or "无",
    )
    return unclaimed, scanned


def claim_table(
    db: Session,
    ontology_id: str,
    *,
    object_type_id: str,
    database: str,
    table: str,
    datasource_id: str | None = None,
) -> dataset_catalog.DatasetEntry:
    """把一张无主表登记为某个已有对象的落点，返回登记后的目录项。

    **认领只登记归属，不代表平台搬过这张表的数据。** 故：写下物理位置与
    ``schema_status=ready``（表确实在），不写 ``last_sync_at``（平台没跑过，说不出时间），
    也**不置 queryable**——查询网关放行与否由对账在一次真实成功之后决定，不由一次人工
    断言决定。

    这就是 ``check_landing_orphans --reattach`` 的人工版：那个脚本按 ODS 命名规则自动把
    孤儿登记接回对象，这里让人对着清单挑主人——命名规则认不出来的表只有人知道归谁。
    """
    obj = db.get(ObjectType, object_type_id)
    if obj is None or obj.ontology_id != ontology_id:
        raise UnclaimedTableError("对象不存在，或不属于当前本体")
    if obj.deleted_by_user:
        raise UnclaimedTableError(
            f"对象「{obj.display_name}」已被人工删除，不能作为落点主人；请先恢复或另选对象"
        )
    physical = qualified_table(database, table)
    if not physical:
        raise UnclaimedTableError("库名/表名不完整")
    if physical.lower() in dataset_catalog.claimed_physical_tables(db):
        raise UnclaimedTableError(f"表 {physical} 已经有主了，不能重复认领")

    ds = _warehouse_datasource(db, datasource_id)
    ontology_version = _ontology_version(db, ontology_id)
    deployment = (
        db.query(OntologyWarehouseDeployment)
        .filter(
            OntologyWarehouseDeployment.ontology_id == ontology_id,
            OntologyWarehouseDeployment.ontology_version == ontology_version,
            OntologyWarehouseDeployment.doris_datasource_id == ds.id,
        )
        .first()
    )
    if deployment is None:
        deployment = OntologyWarehouseDeployment(
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            doris_datasource_id=ds.id,
            status="schema_ready",
        )
        db.add(deployment)
        db.flush()
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(
            WarehouseObjectProjection.deployment_id == deployment.id,
            WarehouseObjectProjection.object_type_id == obj.id,
        )
        .first()
    )
    if projection is None:
        projection = WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="pending",
            sync_status="empty",
            transform_status="not_required",
            queryable=False,
        )
        db.add(projection)
        db.flush()

    layer = layer_of_database(database)
    if layer == ODS_DATABASE:
        if projection.ods_table:
            raise UnclaimedTableError(
                f"对象「{obj.display_name}」的 ODS 落点已经是 "
                f"{qualified_table(projection.ods_database, projection.ods_table)}；"
                "一个对象在一层只能有一个落点，请另选对象"
            )
        projection.ods_database = database
        projection.ods_table = table
        # 认领的前提就是这张表已经在那儿、也有数——否则没什么可认领的。但平台没搬过它，
        # 所以 last_sync_at 留空：说不出时间就不要编一个。
        projection.sync_status = "ready"
    else:
        if projection.serving_table:
            raise UnclaimedTableError(
                f"对象「{obj.display_name}」的服务层落点已经是 "
                f"{qualified_table(projection.serving_database, projection.serving_table)}；"
                "一个对象在一层只能有一个落点，请另选对象"
            )
        projection.serving_database = database
        projection.serving_table = table
        projection.serving_layer = layer
        projection.schema_status = "ready"
    db.commit()

    entry = dataset_catalog.resolve_dataset_ref(
        db,
        dataset_catalog.dataset_ref(
            dataset_catalog.KIND_OBJECT,
            obj.id,
            dataset_catalog.SLOT_ODS if layer == ODS_DATABASE else dataset_catalog.SLOT_SERVING,
        ),
    )
    if entry is None:  # pragma: no cover —— 刚写完就读不到属于内部不一致
        raise UnclaimedTableError("认领已写入，但落点读不回来，请刷新后确认")
    return entry


def _ontology_version(db: Session, ontology_id: str) -> int:
    from app.models import Ontology

    ontology = db.get(Ontology, ontology_id)
    if ontology is None:
        raise UnclaimedTableError("本体不存在")
    return ontology.version or 0


__all__ = [
    "UnclaimedTable",
    "UnclaimedTableError",
    "claim_table",
    "layer_of_database",
    "list_unclaimed_tables",
    "managed_databases",
]
