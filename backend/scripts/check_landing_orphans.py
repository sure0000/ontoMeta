"""体检/修复：指向已不存在对象的物理落点登记行。

成因（已在 ontology_merge 治本）：全量重跑时 ``_handle_object_removal`` 会把上游消失
的机器对象**硬删**，而落点登记（warehouse_object_projections / ingestion_contracts /
warehouse_logic_projections）是按 object_type_id / business_logic_id 挂的，于是留下一批
指向不存在 id 的孤儿行。后果是 Doris 里那张表还在、还有数据，本体里却没有任何东西
对应它——「任务建的表在建模里看不见」原样重现，而且连登记都找不回来。

治本之后新数据不会再产生孤儿（已落地的对象只降级不硬删），此脚本处理**存量**。

三种处置，按可恢复性从好到坏：

1. ``--reattach``：孤儿能按物理事实找回主人时就接回去。典型场景是对象被删后又被重新
   建模——同一张源表、同一个 ODS 落点，只是换了个新 id。接回去，落点立刻在工作区
   重新显示，数据一行不用重搬。
2. 默认（dry-run）只报告：把接不回去的孤儿连同它指向的物理表打印出来，交人判断。
   多数情况该做的是**先把那张源表重新建模**，再跑一次 ``--reattach``。
3. ``--purge``：删除接不回去的孤儿行。**这会丢掉「数仓里存在这张表」的唯一记录**，
   物理表本身不会被删。只在确认那张表已经废弃时才用。

用法：
    cd backend && source .venv/bin/activate
    python -m scripts.check_landing_orphans                    # 体检
    python -m scripts.check_landing_orphans --reattach --apply
    python -m scripts.check_landing_orphans --purge --apply
执行前备份：cp ontometa.db ontometa.db.pre-orphan.$(date +%Y%m%d_%H%M%S).bak
"""

from __future__ import annotations

import argparse
from collections import Counter

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    IngestionContract,
    ObjectType,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)
from app.services.ods_naming import OdsNamingError, target_ods_table_name
from app.services.source_ref import source_table_of


def _live_object_ids(db) -> set[str]:
    return {row[0] for row in db.query(ObjectType.id).all()}


def _live_logic_ids(db) -> set[str]:
    return {row[0] for row in db.query(BusinessLogic.id).all()}


def _reattach_projection(db, projection: WarehouseObjectProjection) -> ObjectType | None:
    """按 ODS 落点表名把孤儿 Projection 接回对象。

    ODS 表名是确定性的（``ods_{数据域}_{原表}``，见 ods_naming），所以「同一张表」这件
    事可以从名字反推——不必猜，也不会接错到另一张表上。
    """
    deployment = db.get(OntologyWarehouseDeployment, projection.deployment_id)
    if deployment is None or not projection.ods_table:
        return None
    candidates = (
        db.query(ObjectType).filter(ObjectType.ontology_id == deployment.ontology_id).all()
    )
    for obj in candidates:
        try:
            expected = target_ods_table_name(db, deployment.ontology_id, obj)
        except OdsNamingError:
            continue
        if expected == projection.ods_table:
            return obj
    return None


def _reattach_contract(db, contract: IngestionContract) -> ObjectType | None:
    """按源物理表把孤儿契约接回对象。契约记着 ``库.表``，对象的 source_ref 也能解析出
    同一个 ``库.表``——两边是同一个物理事实，对得上就是同一张表。"""
    if not contract.source_physical_table:
        return None
    candidates = (
        db.query(ObjectType).filter(ObjectType.ontology_id == contract.ontology_id).all()
    )
    for obj in candidates:
        if source_table_of(obj.source_ref) == contract.source_physical_table:
            return obj
    return None


def scan(db, *, reattach: bool, purge: bool) -> Counter:
    stats: Counter = Counter()
    live_objects = _live_object_ids(db)
    live_logics = _live_logic_ids(db)

    for projection in db.query(WarehouseObjectProjection).all():
        if projection.object_type_id in live_objects:
            continue
        stats["projection_orphans"] += 1
        owner = _reattach_projection(db, projection) if reattach else None
        if owner is not None:
            print(f"  [接回] 物化落点 {projection.ods_database}.{projection.ods_table} -> {owner.name}")
            projection.object_type_id = owner.id
            stats["projection_reattached"] += 1
        elif purge:
            print(f"  [删除] 物化落点 {projection.ods_database}.{projection.ods_table}（无法接回）")
            db.delete(projection)
            stats["projection_purged"] += 1
        else:
            print(
                f"  [孤儿] 物化落点 {projection.ods_database}.{projection.ods_table}"
                f" 服务层={projection.serving_database}.{projection.serving_table}"
                "（该物理表在数仓里仍存在；建议先把源表重新建模，再 --reattach）"
            )

    for contract in db.query(IngestionContract).all():
        if contract.object_type_id in live_objects:
            continue
        stats["contract_orphans"] += 1
        owner = _reattach_contract(db, contract) if reattach else None
        if owner is not None:
            print(f"  [接回] 接入契约 {contract.source_physical_table} -> {owner.name}")
            contract.object_type_id = owner.id
            stats["contract_reattached"] += 1
        elif purge:
            print(f"  [删除] 接入契约 {contract.source_physical_table}（无法接回）")
            db.delete(contract)
            stats["contract_purged"] += 1
        else:
            print(
                f"  [孤儿] 接入契约 源表={contract.source_physical_table}"
                f" 落点={contract.target_ods_database}.{contract.target_ods_table}"
                f" 状态={contract.status}"
            )

    # 口径侧同理：指标任务的 ADS 表挂在 BusinessLogic 上。这里没有可靠的物理事实能把
    # 它接回去（口径不像源表那样有确定性表名映射），故只报告/删除，不猜。
    for projection in db.query(WarehouseLogicProjection).all():
        if projection.business_logic_id in live_logics:
            continue
        stats["logic_projection_orphans"] += 1
        if purge:
            print(f"  [删除] 口径落点 {projection.serving_database}.{projection.serving_table}")
            db.delete(projection)
            stats["logic_projection_purged"] += 1
        else:
            print(
                f"  [孤儿] 口径落点 {projection.serving_database}.{projection.serving_table}"
                f" 状态={projection.status}"
            )

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="体检/修复失联的物理落点登记")
    parser.add_argument(
        "--reattach", action="store_true", help="能按物理事实找回主人的孤儿接回对象"
    )
    parser.add_argument(
        "--purge", action="store_true", help="删除接不回去的孤儿行（丢掉落点记录，物理表不动）"
    )
    parser.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    args = parser.parse_args()

    with SessionLocal() as db:
        stats = scan(db, reattach=args.reattach, purge=args.purge)
        if args.apply:
            db.commit()
            print("== APPLIED ==")
        else:
            db.rollback()
            print("== DRY-RUN（未落库，加 --apply 生效）==")

    total = (
        stats["projection_orphans"]
        + stats["contract_orphans"]
        + stats["logic_projection_orphans"]
    )
    if total == 0:
        print("落点登记体检通过：没有指向已不存在对象的行。")
        return
    print(
        f"孤儿合计 {total}："
        f"物化落点={stats['projection_orphans']}(接回 {stats['projection_reattached']}"
        f"/删除 {stats['projection_purged']}) "
        f"接入契约={stats['contract_orphans']}(接回 {stats['contract_reattached']}"
        f"/删除 {stats['contract_purged']}) "
        f"口径落点={stats['logic_projection_orphans']}(删除 {stats['logic_projection_purged']})"
    )


if __name__ == "__main__":
    main()
