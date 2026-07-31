"""一次性存量修复：把本体内**撞名**的对象消歧（改名，不删除）。

场景：不同源表被命名管线压成同一 ``name``（如 Frappe 的
``tabProcess Period Closing Voucher`` 与 ``tabPeriod Closing Voucher`` 都成
``period_closing_voucher``），发布期触发「对象标识重复」。它们是**不同**对象、
各带属性与关系，不能删——应改名区分。

与 dedupe_ontology_duplicates 的区别：那个删多余的**重复份**；本脚本对**不同表**
撞名做改名消歧，复用与生成/合并端一致的 resolve_duplicate_object_names，改名规则
完全一致（撞名成员改用源表名 snake，尊重人工命名不动）。

默认 dry-run，--apply 落库。执行前务必备份：
    cp ontometa.db ontometa.db.pre-disambiguate.$(date +%Y%m%d_%H%M%S).bak
用法：
    cd backend && source .venv/bin/activate
    python -m scripts.disambiguate_object_names                 # dry-run 全部
    python -m scripts.disambiguate_object_names --apply
    python -m scripts.disambiguate_object_names --ontology-id <id> --apply
"""

from __future__ import annotations

import argparse

from app.database import SessionLocal
from app.models import Ontology
from app.services.ontology_merge import resolve_duplicate_object_names


def main() -> None:
    parser = argparse.ArgumentParser(description="对象撞名消歧（改名，不删除）")
    parser.add_argument("--ontology-id", help="仅处理该本体；缺省处理全部")
    parser.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    args = parser.parse_args()

    total = 0
    with SessionLocal() as db:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id == args.ontology_id)
        for ontology in q.all():
            changes = resolve_duplicate_object_names(db, ontology.id)
            if changes:
                print(f"[{ontology.id}] 改名 {len(changes)} 处：")
                for _oid, old, new in changes:
                    print(f"    {old}  ->  {new}")
            total += len(changes)

        if args.apply:
            db.commit()
            print("== APPLIED ==")
        else:
            db.rollback()
            print("== DRY-RUN（未落库，加 --apply 生效）==")

    print(f"合计改名：{total} 处")


if __name__ == "__main__":
    main()
