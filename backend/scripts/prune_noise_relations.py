"""一次性脚本：清理存量本体里的噪声关系，使「业务关系只存在于业务对象之间」。

与生成侧 `EvidenceBuilder._refine_business_relations` 同一套规则，作用于已入库的
`relation_types`：

1. rule 1：任何业务关联关系(外键/桥/事实)两端都必须是业务对象，否则删除。
2. 血缘去重：某对象对已存在业务关联结构(FK/桥/事实)时，其上的血缘边(重复表达同一
   关联)删除。
3. 血缘 rule 1：任一端非业务对象的血缘删除。
4. 反向引用翻转：仍为兜底「派生出」且两端均业务对象的血缘，按关系图关联度判主/明细，
   翻转为 明细→主数据 的引用关系(structure_type=foreign_key、命名为 位于/属于/采用/引用)，
   并重算 source_signature。判不出主/明细不对称时保留「派生出」。

安全边界：
- **默认只处理 status='draft' 本体**（可变、suggested 态）。published 是不可变版本快照，
  默认跳过；如需修复应「从发布态派生修订草稿 → 重新生成」。--include-published 可强制纳入
  （带告警），或用 --ontology-id 精确指定。
- **尊重人工编辑**：跳过 user_created / origin!='machine' / status in(edited,approved) /
  overridden_fields 含 display_name|structure_type|方向 的关系。
- **默认 dry-run**，仅打印将删除/翻转/改名的条数与前后分布；--apply 才落库。

用法：
    cd backend && source .venv/bin/activate
    python -m scripts.prune_noise_relations                # dry-run，全部 draft
    python -m scripts.prune_noise_relations --apply        # 落库
    python -m scripts.prune_noise_relations --ontology-id <id> --apply
    python -m scripts.prune_noise_relations --include-published   # 谨慎

执行前建议备份：cp ontometa.db ontometa.db.$(date +%Y%m%d_%H%M%S).bak
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

from app.database import SessionLocal
from app.models import ObjectType, Ontology, RelationType
from app.models.ontology import OntologyStatus
from app.services.object_classifier import ROLE_BUSINESS_OBJECT
from app.services.ontology_merge import relation_signature
from app.services.relation_terms import infer_relation_term, reference_term

_BUSINESS_STRUCT = {"foreign_key", "bridge_table", "fact_table"}
_PROTECTED_OVERRIDES = {
    "display_name",
    "structure_type",
    "source_object_type_id",
    "target_object_type_id",
}
_GENERIC_TERM = "派生出"

# 主数据名称线索：反向引用翻转时，比图关联度更可靠的「谁是主数据」判据。存量库无行数，
# 关联度在稠密 ERP 图上易误判（单据连接数可能超过主数据），故名称命中优先。
_MASTER_NAME_HINTS = (
    "地址", "国家", "地区", "区域", "城市", "省份", "公司", "部门", "组织", "机构",
    "币种", "货币", "客户", "供应商", "联系人", "用户", "员工", "物料", "商品",
    "会计科目", "科目", "成本中心", "项目", "银行",
)


def _hits_master_name(label: str | None) -> bool:
    return bool(label) and any(h in label for h in _MASTER_NAME_HINTS)


def _is_human_touched(rel: RelationType) -> bool:
    """人工创建/编辑过的关系不动。"""
    if rel.user_created or rel.deleted_by_user:
        return True
    if (rel.origin or "machine") != "machine":
        return True
    if rel.status in {"edited", "approved"}:
        return True
    try:
        overridden = set(json.loads(rel.overridden_fields) or [])
    except (TypeError, json.JSONDecodeError):
        overridden = set()
    return bool(overridden & _PROTECTED_OVERRIDES)


def _refine_ontology(db, ont: Ontology, apply: bool) -> dict[str, int]:
    """对单个本体执行精炼，返回统计。apply=False 时只统计不落库。"""
    objects = db.query(ObjectType).filter(ObjectType.ontology_id == ont.id).all()
    role_by_id = {o.id: o.table_role for o in objects}
    ref_by_id = {o.id: o.source_ref for o in objects}
    label_by_id = {o.id: o.display_name for o in objects}
    relations = db.query(RelationType).filter(RelationType.ontology_id == ont.id).all()

    def is_bo(oid: str | None) -> bool:
        return role_by_id.get(oid) == ROLE_BUSINESS_OBJECT

    # 已有业务关联结构的无序对（用于血缘去重）。
    fk_pairs: set[frozenset[str]] = {
        frozenset((r.source_object_type_id, r.target_object_type_id))
        for r in relations
        if r.structure_type in _BUSINESS_STRUCT
    }
    # 无向关联度（翻转定向依据）。
    degree: dict[str, int] = defaultdict(int)
    neighbors: dict[str, set[str]] = defaultdict(set)
    for r in relations:
        neighbors[r.source_object_type_id].add(r.target_object_type_id)
        neighbors[r.target_object_type_id].add(r.source_object_type_id)
    for oid, peers in neighbors.items():
        degree[oid] = len(peers)

    stats: Counter[str] = Counter()
    before = Counter(r.display_name for r in relations)

    for rel in relations:
        if _is_human_touched(rel):
            stats["skipped_human"] += 1
            continue
        both_bo = is_bo(rel.source_object_type_id) and is_bo(rel.target_object_type_id)

        if rel.structure_type != "derivation":
            if not both_bo:  # rule 1
                stats["deleted_rule1_fk"] += 1
                if apply:
                    db.delete(rel)
            continue

        # --- derivation ---
        pair = frozenset((rel.source_object_type_id, rel.target_object_type_id))
        if pair in fk_pairs:  # 与已有 FK 重复
            stats["deleted_dup_fk"] += 1
            if apply:
                db.delete(rel)
            continue
        if not both_bo:  # rule 1
            stats["deleted_rule1_deriv"] += 1
            if apply:
                db.delete(rel)
            continue
        if rel.display_name != _GENERIC_TERM:  # 已具体命名 → 保留溯源
            stats["kept_named_deriv"] += 1
            continue

        # 存量里当初落回「派生出」的血缘：先按业务语义重命名（转化/包含/加工类），
        # 与生成侧 infer_relation_term 同一套规则。
        src_label = label_by_id.get(rel.source_object_type_id)
        tgt_label = label_by_id.get(rel.target_object_type_id)
        term = infer_relation_term(
            "lineage", source_label=src_label, target_label=tgt_label
        )
        if term != _GENERIC_TERM:
            stats[f"renamed_{term}"] += 1
            stats["renamed_deriv"] += 1
            if apply:
                rel.display_name = term
            continue

        # 仍是「派生出」→ 翻转为「单据 引用 主数据」。定向优先按主数据名称线索，
        # 名称判不出再退回关联度；都判不出则保留「派生出」。
        a, b = rel.source_object_type_id, rel.target_object_type_id
        a_master, b_master = _hits_master_name(src_label), _hits_master_name(tgt_label)
        detail = master = None
        if a_master != b_master:  # 恰一端命中主数据名 → 它是主数据
            detail, master = (b, a) if a_master else (a, b)
        else:
            da, db_ = degree.get(a, 0), degree.get(b, 0)
            if da != db_:
                detail, master = (a, b) if da < db_ else (b, a)
        if detail is None:
            stats["kept_ambiguous_deriv"] += 1
            continue
        stats["flipped_to_reference"] += 1
        if apply:
            rel.source_object_type_id = detail
            rel.target_object_type_id = master
            rel.display_name = reference_term(label_by_id.get(master))
            rel.structure_type = "foreign_key"
            rel.cardinality = "many_to_one"
            rel.description = (
                f"{label_by_id.get(detail, detail)} 引用主数据 "
                f"{label_by_id.get(master, master)}（由血缘方向翻转推断，待复核）"
            )
            # 方向/结构变了，重算稳定身份键，否则下次合并会错配。
            rel.source_signature = relation_signature(
                ref_by_id.get(detail), ref_by_id.get(master), "foreign_key"
            )

    stats["_before_generic"] = before.get(_GENERIC_TERM, 0)
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="落库（默认 dry-run）")
    parser.add_argument(
        "--include-published",
        action="store_true",
        help="连同 published 本体一起处理（谨慎：会改写版本快照）",
    )
    parser.add_argument(
        "--ontology-id",
        action="append",
        default=[],
        help="仅处理指定本体 id（可重复）；给定时忽略 status 过滤",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id.in_(args.ontology_id))
        elif not args.include_published:
            q = q.filter(Ontology.status == OntologyStatus.DRAFT.value)
        ontologies = q.all()

        if not ontologies:
            print("没有匹配的本体。")
            return

        mode = "APPLY（落库）" if args.apply else "DRY-RUN（仅预览）"
        print(f"模式：{mode}；本体数：{len(ontologies)}\n")

        grand: Counter[str] = Counter()
        for ont in ontologies:
            if ont.status == OntologyStatus.PUBLISHED.value:
                print(f"⚠️  {ont.id[:8]}… 是 published 版本快照，将被改写（--include-published）")
            stats = _refine_ontology(db, ont, apply=args.apply)
            grand.update({k: v for k, v in stats.items() if not k.startswith("_")})
            deleted = (
                stats.get("deleted_rule1_fk", 0)
                + stats.get("deleted_rule1_deriv", 0)
                + stats.get("deleted_dup_fk", 0)
            )
            print(
                f"{ont.status:9s} {ont.id[:8]}…  "
                f"删除={deleted}（rule1_fk={stats.get('deleted_rule1_fk',0)}, "
                f"rule1_deriv={stats.get('deleted_rule1_deriv',0)}, "
                f"dup_fk={stats.get('deleted_dup_fk',0)}）  "
                f"重命名={stats.get('renamed_deriv',0)}"
                f"（转化={stats.get('renamed_转化',0)},包含={stats.get('renamed_包含',0)}）  "
                f"翻转引用={stats.get('flipped_to_reference',0)}  "
                f"保留派生出={stats.get('kept_ambiguous_deriv',0)}  "
                f"跳过人工={stats.get('skipped_human',0)}"
            )

        if args.apply:
            db.commit()
            print("\n已落库。")
        else:
            db.rollback()
            print("\nDRY-RUN 未改动。加 --apply 落库。")

        print(
            "\n汇总："
            f"删除 rule1(FK)={grand.get('deleted_rule1_fk',0)}，"
            f"删除 rule1(血缘)={grand.get('deleted_rule1_deriv',0)}，"
            f"删除重复血缘={grand.get('deleted_dup_fk',0)}，"
            f"重命名血缘={grand.get('renamed_deriv',0)}"
            f"（转化={grand.get('renamed_转化',0)}，包含={grand.get('renamed_包含',0)}），"
            f"翻转为引用={grand.get('flipped_to_reference',0)}，"
            f"保留具体命名血缘={grand.get('kept_named_deriv',0)}，"
            f"保留派生出(不可判)={grand.get('kept_ambiguous_deriv',0)}，"
            f"跳过人工编辑={grand.get('skipped_human',0)}"
        )


if __name__ == "__main__":
    main()
