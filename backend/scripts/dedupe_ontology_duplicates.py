"""一次性清理：去除本体内已累积的重复属性(同对象同字段名多份)与重复对象名。

配合 ontology_merge 的「按字段名去重 + 陈旧机器属性清理」(治本)使用：治本让以后重跑
不再累积重复，此脚本清理**存量**已累积的重复。

- 属性：按 (对象, 归一化字段名) 分组，保留一条(优先人工创建/编辑 > 有业务显示名 >
  有 last_generation_id)，其余**仅删机器且未编辑**的多余份；人工创建/编辑的绝不删。
- 对象：按 (本体, name) 分组报告重复；仅当多余对象是机器且未编辑、且无属性、无关系时
  才安全删除，否则仅报告交人工处理(避免误删)。

默认 dry-run，--apply 落库。执行前建议备份：cp ontometa.db ontometa.db.$(date +%s).bak
用法：
    cd backend && source .venv/bin/activate
    python -m scripts.dedupe_ontology_duplicates                 # dry-run 全部
    python -m scripts.dedupe_ontology_duplicates --apply
    python -m scripts.dedupe_ontology_duplicates --ontology-id <id> --apply
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from app.database import SessionLocal
from app.models import EntityStatus, ObjectType, Ontology, Property, RelationType
from app.services.ontology_merge import _loads, _prop_key

_KEEP_STATUSES = {EntityStatus.SUGGESTED.value, EntityStatus.DEPRECATED.value}


def _is_user_touched(entity) -> bool:
    """人工创建/编辑/已确认 → 不可自动删除。"""
    return (
        bool(getattr(entity, "user_created", False))
        or bool(_loads(getattr(entity, "overridden_fields", None), []))
        or getattr(entity, "status", None) not in _KEEP_STATUSES
    )


def _prop_rank(p: Property) -> tuple:
    """属性保留优先级(越大越优先保留)。"""
    return (
        1 if _is_user_touched(p) else 0,
        1 if (p.display_name and p.display_name != p.name) else 0,
        p.last_generation_id or "",
        p.id,  # 稳定兜底
    )


def dedupe_ontology(db, ontology: Ontology, *, apply: bool) -> Counter:
    stats: Counter = Counter()
    objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).all()
    obj_ids = [o.id for o in objects]
    if not obj_ids:
        return stats

    # ---- 属性去重（同对象同字段名） ----
    props_by_obj: dict[str, list[Property]] = defaultdict(list)
    for p in db.query(Property).filter(Property.object_type_id.in_(obj_ids)).all():
        props_by_obj[p.object_type_id].append(p)

    for oid, plist in props_by_obj.items():
        groups: dict[str, list[Property]] = defaultdict(list)
        for p in plist:
            groups[_prop_key(p.name)].append(p)
        for name_key, dups in groups.items():
            if len(dups) < 2:
                continue
            dups.sort(key=_prop_rank, reverse=True)
            keep, extras = dups[0], dups[1:]
            for e in extras:
                if _is_user_touched(e):
                    stats["prop_dup_kept_user"] += 1  # 人工编辑的重复份，保留交人工
                    continue
                stats["prop_removed"] += 1
                if apply:
                    db.delete(e)

    # ---- 对象重名报告/安全删除 ----
    by_name: dict[str, list[ObjectType]] = defaultdict(list)
    for o in objects:
        by_name[o.name].append(o)
    prop_count = {oid: len(props_by_obj.get(oid, [])) for oid in obj_ids}
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        stats["obj_dup_groups"] += 1
        group.sort(
            key=lambda o: (1 if _is_user_touched(o) else 0, prop_count[o.id], o.id),
            reverse=True,
        )
        for extra in group[1:]:
            referenced = (
                db.query(RelationType)
                .filter(
                    (RelationType.source_object_type_id == extra.id)
                    | (RelationType.target_object_type_id == extra.id)
                    | (RelationType.mapping_object_type_id == extra.id)
                )
                .first()
                is not None
            )
            if (
                not _is_user_touched(extra)
                and prop_count[extra.id] == 0
                and not referenced
            ):
                stats["obj_removed"] += 1
                if apply:
                    db.delete(extra)
            else:
                stats["obj_dup_manual"] += 1  # 有属性/关系/人工痕迹 → 交人工

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="清理本体重复属性/对象")
    parser.add_argument("--ontology-id", help="仅处理该本体；缺省处理全部")
    parser.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    args = parser.parse_args()

    total: Counter = Counter()
    with SessionLocal() as db:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id == args.ontology_id)
        for ontology in q.all():
            stats = dedupe_ontology(db, ontology, apply=args.apply)
            if stats:
                print(
                    f"[{ontology.id}] prop_removed={stats['prop_removed']} "
                    f"prop_dup_kept_user={stats['prop_dup_kept_user']} "
                    f"obj_dup_groups={stats['obj_dup_groups']} "
                    f"obj_removed={stats['obj_removed']} obj_dup_manual={stats['obj_dup_manual']}"
                )
            total.update(stats)
        if args.apply:
            db.commit()
            print("== APPLIED ==")
        else:
            db.rollback()
            print("== DRY-RUN（未落库，加 --apply 生效）==")

    print(
        f"合计：prop_removed={total['prop_removed']} "
        f"prop_dup_kept_user={total['prop_dup_kept_user']} "
        f"obj_removed={total['obj_removed']} obj_dup_manual={total['obj_dup_manual']}"
    )


if __name__ == "__main__":
    main()
