"""把存量本体的板块划分补成全覆盖分区：每个对象恰好属于一个板块。

背景见 ``app/services/segment_kinds``。业务板块（kind=business）由聚类 + LLM 命名产生，
重跑代价高；这个脚本**不动业务板块**，只把它们没认领的对象按原因补进四个兜底板块，
让「未接入」这个隐式垃圾桶归零。

新生成的草稿走 ``segment_generator.build_fallback_segments`` 自带这一步，
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
from app.services.segment_kinds import FALLBACK_KIND_ORDER, FALLBACK_SEGMENT_META
from app.services.segment_placement import (
    classify_fallback_kind,
    place_unsegmented,
    resettle_fallback_member,
    segment_kind_of,
)


def backfill_ontology(db: Session, ontology_id: str, *, dry_run: bool) -> Counter:
    """给一个本体补齐兜底板块，返回各 kind 的落位计数。

    真正的落库逻辑在 :mod:`app.services.segment_placement`——生成、创建、回填三条路
    共用同一份判定，脚本这边只负责 dry-run 预览与打印。
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
    """把兜底板块里**落错位**的对象挪到与当前角色相符的那个板块。

    漂移是这么来的：对象先按「业务对象连不成簇」落进待归类，之后被重判成技术表，
    板块却没跟着走。它卡在待归类板块里，而「改判技术表」对它是空操作——审核台上
    既确认不了也归类不动（实测 erpnext 待归类板块里 6 张 technical）。

    判定与落库都复用 :mod:`app.services.segment_placement`，脚本只负责预览与打印。
    """
    members = (
        db.query(ObjectType)
        .join(OntologySegment, ObjectType.segment_id == OntologySegment.id)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.deleted_by_user == False,  # noqa: E712
            OntologySegment.kind.in_(FALLBACK_KIND_ORDER),
        )
        .all()
    )
    moves: Counter = Counter()
    for obj in members:
        current = segment_kind_of(db, obj.segment_id)
        target = classify_fallback_kind(obj)
        if current == target:
            continue
        moves[f"{current}→{target}"] += 1
        if not dry_run:
            resettle_fallback_member(db, obj)
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
        for ontology_id in ontology_ids:
            counts = backfill_ontology(db, ontology_id, dry_run=args.dry_run)
            if counts:
                total.update(counts)
                head = f"{ontology_id}  未归属 {sum(counts.values())} 个 →"
                detail = "  ".join(
                    f"{FALLBACK_SEGMENT_META[k]['display_name']} {counts[k]}"
                    for k in FALLBACK_KIND_ORDER
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

        if total or moved:
            print()
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
