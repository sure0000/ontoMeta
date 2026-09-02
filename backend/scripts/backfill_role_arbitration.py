"""一次性脚本：按新的角色仲裁口径重算**存量**对象的待复核标记。

背景：生成侧的仲裁改了三处（见 ``draft_generator._resolve_role`` 与
``evidence_builder._reclassify_bridge_to_object``）——

1. **子表事实不再被推断覆盖**：Frappe 的 ``parent``/``parenttype`` 锚点是源 schema 的
   事实。塌缩失败只说明「这条关系表达不出来」，不说明明细行变成了独立业务对象；
   原先重判时把这条硬事实连同软信号一起抑制，「单列业务主键 +2.0」独自把它顶成业务
   对象，再与 LLM 的语义判定全面冲突。
2. **弃权 ≠ 判定**：分类器证据不足时兜底成业务对象（理由写着「信号不足，暂按业务
   对象保留」），那不是一方观点。拿它去跟 LLM 对撞会凭空造出「两源分歧」。
3. **非业务分歧不占人工队列**：发布只提升 ``business_object``（``publish.py``），
   data_table / technical / bridge 之间怎么判对产出零影响。

这些只对**新生成**的草稿生效。本脚本把同样的口径应用到已入库的行上：不重跑分类器、
不调 LLM，只用已落库的 ``role_reason`` / ``role_signals`` / ``properties`` 重放仲裁。

**只会清除待复核，绝不新增；也绝不把任何对象提升为 business_object**——
提升是会被发布的方向，必须有人点头。唯一会改 ``table_role`` 的情形是把「子表被误提
为业务对象」改回 ``data_table``（有 parent/parenttype 锚点为证）。

安全边界：
- **默认 dry-run**，只打印分桶；``--apply`` 才落库。
- 默认只处理 ``status='draft'`` 本体；本仓「一域一本体」的工作本体常年是 published，
  处理它需要显式 ``--include-published``（审核工作台本身也在写这一行）。

用法：
    cd backend && source .venv/bin/activate
    python -m scripts.backfill_role_arbitration --ontology-id <id> --include-published
    python -m scripts.backfill_role_arbitration --ontology-id <id> --include-published --apply

执行前建议备份：cp ontometa.db ontometa.db.$(date +%Y%m%d_%H%M%S).bak
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter

from app.database import SessionLocal
from app.models import ObjectType, Ontology, Property
from app.models.ontology import OntologyStatus
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    ROLE_TECHNICAL,
)

# 分歧句式里出现的角色词 → 角色值。旧代码只给三个可投票角色配了中文标签，
# data_table 是原样落进句子的，两种写法都要认。
_LABEL_TO_ROLE = {
    "业务对象": ROLE_BUSINESS_OBJECT,
    "技术/系统表": ROLE_TECHNICAL,
    "业务事实/关系表": ROLE_BRIDGE,
    "data_table": ROLE_DATA_TABLE,
    "business_object": ROLE_BUSINESS_OBJECT,
    "technical": ROLE_TECHNICAL,
    "bridge": ROLE_BRIDGE,
}

_DISAGREE = "启发式↔LLM 角色分歧"
_ABSTAIN_MARK = "信号不足，暂按业务对象保留"
# Frappe 子表锚点列：出现即说明这张表是某父 DocType 的明细（与 source_profile 同源）。
_CHILD_MARKERS = {"parenttype", "parentfield"}


def parse_disagreement(reason: str | None) -> tuple[str, str] | None:
    """从 role_reason 还原 (启发式角色, LLM 角色)。解析不出就返回 None（不动它）。"""
    if not reason or _DISAGREE not in reason:
        return None
    llm = re.match(r"启发式↔LLM 角色分歧：LLM 判为([^（(；]+)", reason)
    heur = re.search(r"；启发式判为([^（(；]+)", reason)
    if not llm or not heur:
        return None
    llm_role = _LABEL_TO_ROLE.get(llm.group(1).strip())
    heur_role = _LABEL_TO_ROLE.get(heur.group(1).strip())
    if not llm_role or not heur_role:
        return None
    return heur_role, llm_role


def _child_anchor(prop_names: set[str]) -> bool:
    return bool(_CHILD_MARKERS & prop_names)


def replay(obj: ObjectType, prop_names: set[str]) -> tuple[str, dict] | None:
    """按新口径重放这一行的仲裁。返回 (分桶名, 要写回的字段) 或 None（不动）。"""
    if not obj.needs_review:
        return None
    reason = obj.role_reason or ""
    child = _child_anchor(prop_names)
    signals = {}
    if obj.role_signals:
        try:
            signals = json.loads(obj.role_signals) if isinstance(obj.role_signals, str) else obj.role_signals
        except (TypeError, ValueError):
            signals = {}
    signals = signals or {}

    dis = parse_disagreement(reason)
    if dis is None:
        # 非分歧行里唯一确定错了的：子表被「智能重判」提成了业务对象。
        # 改回数据表（明细），但**保留待复核**——没有第二个源为它背书。
        if (
            child
            and obj.table_role == ROLE_BUSINESS_OBJECT
            and signals.get("reclassified_from") == "bridge"
        ):
            return (
                "R3 子表误提为业务对象 → 数据表(仍待复核)",
                {
                    "table_role": ROLE_DATA_TABLE,
                    "role_reason": (
                        f"{reason}；【重算】含 parent/parenttype 子表锚点（源 schema 事实）："
                        "它是父表的明细行，塌缩不成也不构成独立业务对象，改判数据表"
                    ),
                },
            )
        return None

    heur_role, llm_role = dis
    bucket_prefix = ""
    # R3：子表锚点是事实，启发式那一侧本不该是业务对象。
    if child and heur_role == ROLE_BUSINESS_OBJECT:
        heur_role = ROLE_DATA_TABLE
        bucket_prefix = "R3 子表 + "
    abstained = _ABSTAIN_MARK in reason

    if llm_role == ROLE_BUSINESS_OBJECT:
        return None  # 提升方向，永远交给人
    if not (abstained or heur_role != ROLE_BUSINESS_OBJECT):
        return None  # 跨越业务边界的真分歧，留给人

    why = (
        "启发式证据不足未作判定"
        if abstained
        else "两源同属非业务对象，差异不影响发布（只有业务对象会被发布）"
    )
    bucket = bucket_prefix + (
        "R1 启发式弃权 → 采纳 LLM" if abstained else "R2 非业务分歧 → 采纳 LLM"
    )
    return (
        bucket,
        {
            "needs_review": False,
            "role_confidence": 0.7,
            "role_reason": f"{reason}；【重算】{why}，自动结案，采纳 LLM 语义判定",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ontology-id", help="只处理这一个本体（默认全部）")
    parser.add_argument("--include-published", action="store_true", help="含 published 本体")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id == args.ontology_id)
        if not args.include_published:
            q = q.filter(Ontology.status == OntologyStatus.DRAFT)
        ontologies = q.all()
        if not ontologies:
            print("没有匹配的本体（published 本体需要 --include-published）")
            return

        for ont in ontologies:
            objs = (
                db.query(ObjectType)
                .filter(
                    ObjectType.ontology_id == ont.id,
                    ObjectType.deleted_by_user.is_(False),
                )
                .all()
            )
            pending = [o for o in objs if o.needs_review]
            if not pending:
                continue
            prop_names: dict[str, set[str]] = {}
            for oid, pname in (
                db.query(Property.object_type_id, Property.name)
                .filter(Property.object_type_id.in_([o.id for o in objs]))
                .all()
            ):
                prop_names.setdefault(oid, set()).add((pname or "").lower())

            buckets: Counter[str] = Counter()
            changed = 0
            for obj in pending:
                out = replay(obj, prop_names.get(obj.id, set()))
                if not out:
                    continue
                bucket, updates = out
                buckets[bucket] += 1
                changed += 1
                if args.apply:
                    for key, value in updates.items():
                        setattr(obj, key, value)

            print(f"\n本体 {ont.id}（{ont.status}）：待复核 {len(pending)} / 对象 {len(objs)}")
            for name, count in buckets.most_common():
                print(f"  {count:5d}  {name}")
            cleared = sum(c for n, c in buckets.items() if "仍待复核" not in n)
            print(f"  ——— 可自动结案 {cleared}，剩余人工 {len(pending) - cleared}")
            if args.apply:
                db.commit()
                print(f"  已写库：{changed} 行")
            else:
                print("  dry-run，未写库（加 --apply 落库）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
