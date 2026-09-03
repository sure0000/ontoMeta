"""把存量本体的板块划分补成全覆盖分区：每个对象恰好属于一个板块。

背景见 ``app/services/segment_kinds``。规矩是：业务对象/业务关系表一定在某个业务板块
下，其余的落系统表。业务板块（kind=business）由聚类 + LLM 命名产生，重跑代价高；
这个脚本**不动业务板块的既有成员**，只做三件事：

1. 拆掉已废弃的「待归类业务对象」「技术表」两个板块，成员腾成未归属；
2. 把所有未归属对象按级联规则重新落位（邻居 → 命名族 → 系统表）；
3. 把落错位的成员挪到与当前角色相符的板块（非业务角色一律回系统表）。

新生成的草稿走 ``segment_generator.build_fallback_segments`` 自带这些，
本脚本只用于**存量**本体。

用法::

    python -m scripts.backfill_segment_partition --dry-run          # 只看会怎么分
    python -m scripts.backfill_segment_partition                    # 全部本体
    python -m scripts.backfill_segment_partition --ontology <id>    # 指定本体
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import ObjectType, Ontology, OntologySegment
from app.services.segment_kinds import (
    FALLBACK_KIND_ORDER,
    FALLBACK_SEGMENT_META,
    LEGACY_KINDS,
    SEGMENT_KIND_BUSINESS,
)
from app.services.segment_placement import (
    AffinityIndex,
    classify_fallback_kind,
    dissolve_legacy_segments,
    place_unsegmented,
    resettle_by_role,
    segment_kind_of,
)

#: 打印用的板块名。计数里还会出现 business（被亲和收编进业务模块的）。
_KIND_LABELS = {
    SEGMENT_KIND_BUSINESS: "业务模块",
    **{k: meta["display_name"] for k, meta in FALLBACK_SEGMENT_META.items()},
}
_COUNT_ORDER = (SEGMENT_KIND_BUSINESS, *FALLBACK_KIND_ORDER)


def dissolve_ontology(db: Session, ontology_id: str, *, dry_run: bool) -> int:
    """拆掉已废弃的「待归类业务对象」「技术表」板块，返回被腾空的对象数。"""
    if dry_run:
        return (
            db.query(ObjectType)
            .join(OntologySegment, ObjectType.segment_id == OntologySegment.id)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.deleted_by_user == False,  # noqa: E712
                OntologySegment.kind.in_(LEGACY_KINDS),
            )
            .count()
        )
    return dissolve_legacy_segments(db, ontology_id)


def backfill_ontology(db: Session, ontology_id: str, *, dry_run: bool) -> Counter:
    """给一个本体补齐板块归属，返回各去处的落位计数。

    真正的落库逻辑在 :mod:`app.services.segment_placement`——生成、创建、回填三条路
    共用同一份判定，脚本这边只负责 dry-run 预览与打印。dry-run 只按兜底规则估算，
    不预演亲和归位（那要滚动更新投票池，预演出来的数跟真跑不是一回事）。
    """
    if not dry_run:
        return Counter(place_unsegmented(db, ontology_id))

    orphans = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.segment_id.is_(None),
            ObjectType.deleted_by_user == False,  # noqa: E712
        )
        .all()
    )
    return Counter(classify_fallback_kind(obj) for obj in orphans)


def resettle_ontology(db: Session, ontology_id: str, *, dry_run: bool) -> Counter:
    """把**落错位**的成员挪到与当前角色相符的板块。

    漂移是这么来的：对象先按角色落位，之后被重判，板块却没跟着走。存量里两种都有——
    业务模块里躺着技术表，系统表里躺着业务对象。规矩是角色决定板块，这里把它对齐。

    判定与落库都复用 :mod:`app.services.segment_placement`，脚本只负责预览与打印。
    人工钉过板块的对象不动（``resettle_by_role`` 自己会挡）。
    """
    members = (
        db.query(ObjectType)
        .join(OntologySegment, ObjectType.segment_id == OntologySegment.id)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.deleted_by_user == False,  # noqa: E712
        )
        .all()
    )
    moves: Counter = Counter()
    # 索引建一次全本体共用：逐个建等于每个对象扫一遍全表。建索引是只读的，
    # dry-run 也用同一个，这样预演出来的数就等于真跑出来的数。
    affinity = AffinityIndex(db, ontology_id)
    for obj in members:
        current = segment_kind_of(db, obj.segment_id)
        target = resettle_by_role(db, obj, affinity, apply=not dry_run)
        if target is None:
            continue
        moves[f"{current}→{target}"] += 1
    if moves and not dry_run:
        db.flush()
        for segment in (
            db.query(OntologySegment)
            .filter(
                OntologySegment.ontology_id == ontology_id,
                OntologySegment.kind.in_(FALLBACK_KIND_ORDER),
            )
            .all()
        ):
            segment.member_count = (
                db.query(ObjectType)
                .filter(
                    ObjectType.ontology_id == ontology_id,
                    ObjectType.segment_id == segment.id,
                    ObjectType.deleted_by_user == False,  # noqa: E712
                )
                .count()
            )
    return moves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ontology", help="只处理这一个本体 id（缺省：全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印分派结果，不写库")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = db.query(Ontology.id)
        if args.ontology:
            query = query.filter(Ontology.id == args.ontology)
        ontology_ids = [row[0] for row in query.all()]
        if not ontology_ids:
            print("没有匹配的本体")
            return 1

        total: Counter = Counter()
        moved: Counter = Counter()
        dissolved = 0
        for ontology_id in ontology_ids:
            freed = dissolve_ontology(db, ontology_id, dry_run=args.dry_run)
            if freed:
                dissolved += freed
                # dry-run 不真拆，这批对象的去向由下面那行「落位漂移」一并报出来，
                # 两行说的是同一批表，别当成两笔账加起来。
                print(f"{ontology_id}  拆掉已废弃板块，腾出 {freed} 个对象")
            counts = backfill_ontology(db, ontology_id, dry_run=args.dry_run)
            if counts:
                total.update(counts)
                head = f"{ontology_id}  未归属 {sum(counts.values())} 个 →"
                detail = "  ".join(
                    f"{_KIND_LABELS[k]} {counts[k]}"
                    for k in _COUNT_ORDER
                    if counts.get(k)
                )
                print(f"{head} {detail}")
            drift = resettle_ontology(db, ontology_id, dry_run=args.dry_run)
            if drift:
                moved.update(drift)
                detail = "  ".join(f"{k} {v}" for k, v in sorted(drift.items()))
                print(f"{ontology_id}  落位漂移 {sum(drift.values())} 个 → {detail}")

        if not args.dry_run:
            db.commit()

        if total or moved or dissolved:
            print()
            if dissolved:
                print(f"拆掉已废弃板块，共腾出 {dissolved} 个对象")
            if total:
                print("补齐合计：", "  ".join(f"{k}={v}" for k, v in total.items()))
            if moved:
                print("挪位合计：", "  ".join(f"{k}={v}" for k, v in sorted(moved.items())))
            print("（dry-run，未写库）" if args.dry_run else "已写入。")
        else:
            print("所有本体的划分已经是全覆盖分区、且落位与角色相符，无需回填。")

        # 校验：写完之后不应再有未归属对象
        if not args.dry_run:
            leftover = (
                db.query(ObjectType)
                .filter(
                    ObjectType.segment_id.is_(None),
                    ObjectType.deleted_by_user == False,  # noqa: E712,
                    ObjectType.ontology_id.in_(ontology_ids),
                )
                .count()
            )
            print(f"校验：仍未归属 {leftover} 个对象（应为 0）")
            if leftover:
                return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
