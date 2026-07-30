"""一次性脚本：把存量本体里「未物化的关系表(bridge)」塌缩为业务对象间的关系。

背景：table_role=bridge 的对象语义是「一张关系表」，它必须落地为某条 RelationType 的
``mapping_object``，否则发布时被静默丢弃、它连接的业务对象随之在图谱中消失（见
``draft_consistency`` 的 ``bridge_object_not_materialized`` 校验）。生成侧已在
``EvidenceBuilder._collapse_bridge_relations`` 自动塌缩；本脚本对**已入库**的桥表做同样的事，
用已落库的 ``properties``(name+data_type) 离线重建 Frappe Link 推断外键，与生成侧共用
``bridge_collapse.select_bridge_endpoints`` 选端点。

作用对象：某桥表若已被任一 RelationType 以它为 mapping_object，则视为已物化、跳过（幂等）。
选不出两个业务对象端点的桥表（典型：只引用父表的明细/子表）保持不动，交由后续 parent 端解析。

安全边界：
- **默认只处理 status='draft' 本体**；published 是不可变快照，默认跳过（--include-published 强制）。
- **默认 dry-run**，仅打印将新建的关系条数与分桶；--apply 才落库。

用法：
    cd backend && source .venv/bin/activate
    python -m scripts.backfill_bridge_collapse                     # dry-run，全部 draft
    python -m scripts.backfill_bridge_collapse --apply             # 落库
    python -m scripts.backfill_bridge_collapse --ontology-id <id> --apply

执行前建议备份：cp ontometa.db ontometa.db.$(date +%Y%m%d_%H%M%S).bak
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from app.database import SessionLocal
from app.models import ObjectType, Ontology, RelationType
from app.models.ontology import OntologyStatus
from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    DomainInput,
    FieldInput,
)
from app.services.bridge_collapse import select_bridge_endpoints
from app.services.source_profile import FrappeProfile, SourceProfile


def _pick_profile(bundle: DataHubDomainBundle) -> SourceProfile:
    """选源画像。注意：入库对象名在摄取时已被剥去 `tab` 前缀，故不能用
    ``detect_source_profile``（它按 tab 前缀判 Frappe）。改按 Frappe 框架标准列
    (name/creation/modified) 过半来判定，命中即用 FrappeProfile 做 Link 推断。
    """
    datasets = bundle.datasets
    if not datasets:
        return SourceProfile()
    frappe_std = sum(
        1
        for d in datasets
        if {"name", "creation", "modified"} <= {f.name.lower() for f in d.fields}
    )
    return FrappeProfile() if frappe_std >= len(datasets) * 0.5 else SourceProfile()


def _build_bundle(ontology_id: str, objects: list[ObjectType], props_by_obj) -> DataHubDomainBundle:
    """用已入库对象+属性重建一个源画像可消费的 bundle。

    对象名(``ObjectType.name``)直接作 ``DatasetInput.name``；属性名/类型作字段。
    Frappe 以 ``name`` 列为主键，据此标 is_primary_key，便于 profile 识别为 Frappe 源。
    """
    datasets: list[DatasetInput] = []
    for obj in objects:
        fields = [
            FieldInput(
                name=p.name,
                data_type=p.data_type,
                is_primary_key=(p.name or "").lower() == "name",
            )
            for p in props_by_obj.get(obj.id, [])
        ]
        datasets.append(
            DatasetInput(urn=obj.id, name=obj.name, display_name=obj.display_name, fields=fields)
        )
    return DataHubDomainBundle(
        domain=DomainInput(id=ontology_id, name=ontology_id), datasets=datasets
    )


def backfill_ontology(db, ontology: Ontology, *, apply: bool) -> Counter:
    stats: Counter = Counter()
    objects = (
        db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).all()
    )
    if not objects:
        return stats

    props_by_obj = defaultdict(list)
    for obj in objects:
        for p in obj.properties:
            props_by_obj[obj.id].append(p)

    bundle = _build_bundle(ontology.id, objects, props_by_obj)
    profile = _pick_profile(bundle)
    table_index = profile.build_table_index(bundle)
    inferred_by_ds = {
        ds.name: profile.inferred_fks(ds, table_index) for ds in bundle.datasets
    }

    # 以对象名为键（=DatasetInput.name，也是推断外键的 target_table）。
    role_by_object = {obj.name: obj.table_role for obj in objects}
    id_by_object = {obj.name: obj.id for obj in objects}
    label_by_object = {obj.name: (obj.display_name or obj.name) for obj in objects}

    # 主数据度：某对象被多少张表通过推断外键指向（入度）。
    in_degree: Counter = Counter()
    for edges in inferred_by_ds.values():
        for e in edges:
            in_degree[e.target_table] += 1

    # 已物化桥表（已是某关系的 mapping_object）→ 幂等跳过。
    existing = (
        db.query(RelationType).filter(RelationType.ontology_id == ontology.id).all()
    )
    materialized_ids = {r.mapping_object_type_id for r in existing if r.mapping_object_type_id}
    existing_names = {r.name for r in existing}

    for obj in objects:
        if obj.table_role != "bridge":
            continue
        stats["bridge_total"] += 1
        if obj.id in materialized_ids:
            stats["already_materialized"] += 1
            continue

        ref_targets = [e.target_table for e in inferred_by_ds.get(obj.name, [])]
        endpoints = select_bridge_endpoints(
            ref_targets, role_by_object, dict(in_degree), self_name=obj.name
        )
        if endpoints is None:
            stats["skipped_no_endpoints"] += 1
            continue

        source, target = endpoints
        source_id, target_id = id_by_object.get(source), id_by_object.get(target)
        if not source_id or not target_id:
            stats["skipped_no_endpoints"] += 1
            continue

        # 关系名：一桥一关系用桥表名；极少数与既有关系撞名时加后缀。
        rel_name = obj.name if obj.name not in existing_names else f"{obj.name}__collapsed"
        existing_names.add(rel_name)
        stats["collapsed"] += 1
        source_label = label_by_object.get(source, source)
        target_label = label_by_object.get(target, target)
        bridge_label = obj.display_name or obj.name
        if apply:
            db.add(
                RelationType(
                    ontology_id=ontology.id,
                    name=rel_name,
                    display_name=bridge_label,
                    description=(
                        f"{source_label} 与 {target_label} 通过关系表 {bridge_label} 关联"
                        "（桥表塌缩回填，待复核）"
                    ),
                    source_object_type_id=source_id,
                    target_object_type_id=target_id,
                    cardinality="many_to_many",
                    structure_type="bridge_table",
                    mapping_object_type_id=obj.id,
                    source_confidence=0.5,
                    status="suggested",
                )
            )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="存量桥表塌缩为业务关系")
    parser.add_argument("--ontology-id", help="仅处理该本体；缺省处理全部 draft")
    parser.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    parser.add_argument(
        "--include-published", action="store_true", help="纳入 published 本体（谨慎）"
    )
    args = parser.parse_args()

    total: Counter = Counter()
    with SessionLocal() as db:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id == args.ontology_id)
        elif not args.include_published:
            q = q.filter(Ontology.status == OntologyStatus.DRAFT.value)
        ontologies = q.all()

        for ontology in ontologies:
            stats = backfill_ontology(db, ontology, apply=args.apply)
            if stats.get("bridge_total"):
                print(
                    f"[{ontology.id}] bridges={stats['bridge_total']} "
                    f"already={stats['already_materialized']} "
                    f"collapsed={stats['collapsed']} "
                    f"skipped_no_endpoints={stats['skipped_no_endpoints']}"
                )
            total.update(stats)

        if args.apply:
            db.commit()
            print("== APPLIED ==")
        else:
            db.rollback()
            print("== DRY-RUN（未落库，加 --apply 生效）==")

    print(
        f"合计：bridges={total['bridge_total']} already={total['already_materialized']} "
        f"collapsed={total['collapsed']} skipped_no_endpoints={total['skipped_no_endpoints']}"
    )


if __name__ == "__main__":
    main()
