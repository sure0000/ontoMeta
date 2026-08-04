"""一次性存量清洗（F1）：把 cardinality / semantic_type / data_type 归一到受控枚举。

形式化校验（F2/F3/F4）依赖这三个字段是**枚举**而非自由文本。历史数据里它们是
LLM/规则写入的自由文本（``1:N`` / ``many_to_one`` / ``amount`` / ``金额`` …）。本脚本
用 ``app.ontology_types`` 的归一函数把存量值统一到枚举字面量：
  - cardinality  → one_to_one/one_to_many/many_to_one/many_to_many；认不出置 None 并计入待复核
  - semantic_type→ identifier/measure/temporal/categorical/textual/technical；认不出置 unknown
  - data_type    → 仅去空白/小写归一（保留原物理类型语义，不强枚举）

semantic_type 认不出时优先用 object_classifier 的字段画像兜底（它已按度量/描述/时间
占比打分），仍认不出才置 unknown。unknown/None 项列入报告，供后续人工复核。

幂等、可重跑、不删数据。默认 dry-run，--apply 落库。执行前务必备份：
    cp ontometa.db ontometa.db.pre-formalize.$(date +%Y%m%d_%H%M%S).bak
用法：
    cd backend && source .venv/bin/activate
    python -m scripts.formalize_ontology_fields                 # dry-run 全部
    python -m scripts.formalize_ontology_fields --apply
    python -m scripts.formalize_ontology_fields --ontology-id <id> --apply
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from app.database import SessionLocal
from app.models import ObjectType, Ontology, Property, RelationType
from app.ontology_types import (
    SemanticType,
    normalize_cardinality,
    normalize_semantic_type,
)


@dataclass
class Report:
    card_normalized: int = 0
    card_unknown: list[str] = field(default_factory=list)      # relation_id
    sem_normalized: int = 0
    sem_unknown: list[str] = field(default_factory=list)       # property_id
    dtype_normalized: int = 0

    def summary(self) -> str:
        return (
            f"基数归一 {self.card_normalized}（未识别 {len(self.card_unknown)}）；"
            f"语义类型归一 {self.sem_normalized}（unknown {len(self.sem_unknown)}）；"
            f"数据类型归一 {self.dtype_normalized}"
        )


def _normalize_data_type(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    return v or None


def formalize_ontology(db, ontology_id: str, report: Report) -> None:
    # 关系基数
    for rel in db.query(RelationType).filter(RelationType.ontology_id == ontology_id):
        card = normalize_cardinality(rel.cardinality)
        new_value = card.value if card is not None else None
        if new_value != rel.cardinality:
            rel.cardinality = new_value
            report.card_normalized += 1
        if new_value is None and rel.cardinality is not None:
            report.card_unknown.append(rel.id)
        elif card is None and rel.cardinality:  # 原有值但认不出
            report.card_unknown.append(rel.id)

    # 属性语义类型 + 数据类型
    props = (
        db.query(Property)
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
    )
    for prop in props:
        st = normalize_semantic_type(prop.semantic_type)
        if st is SemanticType.UNKNOWN:
            st = _classifier_fallback(prop)
        if st.value != prop.semantic_type:
            prop.semantic_type = st.value
            report.sem_normalized += 1
        if st is SemanticType.UNKNOWN:
            report.sem_unknown.append(prop.id)

        dt = _normalize_data_type(prop.data_type)
        if dt != prop.data_type:
            prop.data_type = dt
            report.dtype_normalized += 1


def _classifier_fallback(prop: Property) -> SemanticType:
    """认不出的语义类型：用简单画像兜底（字段名/数据类型信号）。

    不引入 object_classifier 的重逻辑（它面向对象角色、需全表上下文），这里只做
    字段级轻量推断，保守——拿不准仍返回 UNKNOWN，绝不臆断为 measure（否则 F3 会
    放行本不该聚合的字段）。
    """
    name = (prop.name or "").lower()
    dtype = (prop.data_type or "").lower()
    if name.endswith(("_id", "_no", "_code")) or name in ("id", "pk"):
        return SemanticType.IDENTIFIER
    if any(k in dtype for k in ("date", "time", "timestamp")) or name.endswith(
        ("_date", "_time", "_at")
    ):
        return SemanticType.TEMPORAL
    if any(k in dtype for k in ("int", "decimal", "numeric", "float", "double", "money")):
        # 数值型但无更强语义：可能是度量、也可能是编号——保守不判 measure
        if any(k in name for k in ("amount", "amt", "price", "qty", "quantity", "total", "sum", "金额", "数量", "价")):
            return SemanticType.MEASURE
        return SemanticType.UNKNOWN
    if any(k in dtype for k in ("bool", "bit")):
        return SemanticType.CATEGORICAL
    return SemanticType.UNKNOWN


def main() -> None:
    parser = argparse.ArgumentParser(description="存量字段枚举化清洗（F1）")
    parser.add_argument("--ontology-id", help="仅处理该本体；缺省处理全部")
    parser.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    args = parser.parse_args()

    report = Report()
    with SessionLocal() as db:
        q = db.query(Ontology)
        if args.ontology_id:
            q = q.filter(Ontology.id == args.ontology_id)
        for ontology in q.all():
            formalize_ontology(db, ontology.id, report)
        if args.apply:
            db.commit()
            print("[applied] 已落库。")
        else:
            db.rollback()
            print("[dry-run] 未落库（加 --apply 生效）。")

    print(report.summary())
    if report.card_unknown:
        print(f"未识别基数的关系 id（需复核）：{report.card_unknown[:20]}"
              + ("…" if len(report.card_unknown) > 20 else ""))
    if report.sem_unknown:
        print(f"语义类型仍为 unknown 的属性 id（需复核）：{report.sem_unknown[:20]}"
              + ("…" if len(report.sem_unknown) > 20 else ""))


if __name__ == "__main__":
    main()
