"""一次性脚本：修正与物理类型矛盾的存量 semantic_type。

背景：语义类型**决定目标列的物理类型**（datetime→TIMESTAMP、amount→DECIMAL、
flag→BOOLEAN，见各 Dialect Adapter 的 map_type）。而此前的推断只看字段名，于是
``date_format``（VARCHAR，存 "dd-mm-yyyy"）被判 datetime、``_user_tags``（TEXT，
存 ``["a"]``）被判 flag。物化出来的目标表**装不下自己的源数据**，搬运每次都挂在
类型转换上（psycopg 的 "invalid input syntax for type timestamp" 之类），而报错
指向数据、不指向那条语义判断，极难回溯。

生成侧已修（``EvidenceBuilder._infer_semantic_type`` 现在会与物理类型和样例对账），
本脚本对**已入库**的属性做同样的事，判据直接复用生成侧的 ``_samples_support`` /
``_is_text_physical``——两处各写一份必然分叉。

判定：物理类型是文本 + 当前语义属于 datetime/amount/flag + 样例值不支持该语义
→ 改为 ``attribute``。没有样例即视为不支持（无证据不认）。

安全边界：
- **默认只处理 status='draft' 本体**；published 是不可变快照，默认跳过（--include-published 强制）。
- **默认 dry-run**，只打印将改的条数与分布；--apply 才落库。
- 只改机器推断出来的值：``origin='user'`` 或 ``overridden_fields`` 里含 semantic_type
  的属性一律跳过——人工改过的判断不被脚本覆盖。

用法::

    cd backend && source .venv/bin/activate
    python -m scripts.backfill_semantic_types                      # dry-run
    python -m scripts.backfill_semantic_types --apply
    python -m scripts.backfill_semantic_types --ontology-id <id> --apply

执行前建议备份：cp ontometa.db ontometa.db.$(date +%Y%m%d_%H%M%S).bak
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from app.database import SessionLocal
from app.models import ObjectType, Ontology, Property
from app.models.ontology import OntologyStatus
from app.services.evidence_builder import (
    _PHYSICAL_SEMANTICS,
    _is_text_physical,
    _samples_support,
)

FALLBACK = "attribute"


def _samples_of(prop: Property) -> list[str]:
    raw = getattr(prop, "sample_values_json", None)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in values] if isinstance(values, list) else []


def _is_user_owned(prop: Property) -> bool:
    """人工定过的语义类型不被脚本覆盖。"""
    if (getattr(prop, "origin", "") or "") == "user":
        return True
    raw = getattr(prop, "overridden_fields", None)
    return bool(raw) and "semantic_type" in raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ontology-id")
    parser.add_argument("--include-published", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    changed: list[tuple[str, str, str, str]] = []  # (对象, 属性, 物理类型, 原语义)
    skipped_user = 0
    with SessionLocal() as db:
        q = (
            db.query(Property, ObjectType, Ontology)
            .join(ObjectType, Property.object_type_id == ObjectType.id)
            .join(Ontology, Ontology.id == ObjectType.ontology_id)
        )
        if args.ontology_id:
            q = q.filter(ObjectType.ontology_id == args.ontology_id)
        if not args.include_published:
            q = q.filter(Ontology.status != OntologyStatus.PUBLISHED.value)

        for prop, obj, _onto in q.all():
            semantic = (prop.semantic_type or "").lower()
            if semantic not in _PHYSICAL_SEMANTICS:
                continue
            if not _is_text_physical(prop.data_type):
                continue
            if _samples_support(semantic, _samples_of(prop)):
                continue  # 样例支持该语义（字符串里真的存的是日期/金额/布尔）
            if _is_user_owned(prop):
                skipped_user += 1
                continue
            changed.append((obj.name, prop.name, prop.data_type or "", semantic))
            if args.apply:
                prop.semantic_type = FALLBACK
        if args.apply:
            db.commit()

    by_semantic = Counter(s for _, _, _, s in changed)
    objects = {o for o, _, _, _ in changed}
    print(f"将改 {len(changed)} 个属性，涉及 {len(objects)} 个对象")
    for semantic, count in by_semantic.most_common():
        print(f"  {semantic} → {FALLBACK}：{count} 个")
    if skipped_user:
        print(f"  （跳过 {skipped_user} 个人工定过语义类型的属性）")
    for obj, prop, data_type, semantic in changed[:15]:
        print(f"    {obj}.{prop}  {data_type}  {semantic} → {FALLBACK}")
    if len(changed) > 15:
        print(f"    …另有 {len(changed) - 15} 个")
    print("\n已落库。" if args.apply else "\n（dry-run；加 --apply 才落库）")


if __name__ == "__main__":
    main()
