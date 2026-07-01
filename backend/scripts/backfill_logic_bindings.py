"""一次性脚本：把旧的文本命中关联回填为显式的 inferred 业务逻辑绑定。

适用场景：在引入 business_logic_object_bindings /
business_logic_property_bindings 表之前已生成的本体草稿，没有显式绑定记录。
本脚本复用 query.py 的文本兜底命中逻辑，把命中的对象 / 字段固化为 source=inferred
的绑定，让运行时查询不再依赖文本兜底。

用法：
    cd backend
    source .venv/bin/activate
    python -m scripts.backfill_logic_bindings
"""

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ObjectType,
    Property,
)
from app.services.query import _logic_relates_to_object, _logic_text_blob


def _backfill_object_bindings(db: Session) -> int:
    created = 0
    logics = db.query(BusinessLogic).all()
    for logic in logics:
        objects = (
            db.query(ObjectType).filter(ObjectType.ontology_id == logic.ontology_id).all()
        )
        for obj in objects:
            if not _logic_relates_to_object(logic, obj):
                continue
            exists = (
                db.query(BusinessLogicObjectBinding)
                .filter(
                    BusinessLogicObjectBinding.business_logic_id == logic.id,
                    BusinessLogicObjectBinding.object_type_id == obj.id,
                    BusinessLogicObjectBinding.role == "subject",
                )
                .first()
            )
            if exists:
                continue
            db.add(
                BusinessLogicObjectBinding(
                    business_logic_id=logic.id,
                    object_type_id=obj.id,
                    role="subject",
                    source="inferred",
                    confidence=0.5,
                )
            )
            created += 1
    return created


def _backfill_property_bindings(db: Session) -> int:
    created = 0
    logics = db.query(BusinessLogic).all()
    for logic in logics:
        blob = _logic_text_blob(logic)
        objects = (
            db.query(ObjectType).filter(ObjectType.ontology_id == logic.ontology_id).all()
        )
        for obj in objects:
            for prop in obj.properties:
                tokens = [t for t in (prop.name, prop.display_name) if t]
                if not any(t.lower() in blob for t in tokens):
                    continue
                exists = (
                    db.query(BusinessLogicPropertyBinding)
                    .filter(
                        BusinessLogicPropertyBinding.business_logic_id == logic.id,
                        BusinessLogicPropertyBinding.property_id == prop.id,
                        BusinessLogicPropertyBinding.role == "input",
                    )
                    .first()
                )
                if exists:
                    continue
                db.add(
                    BusinessLogicPropertyBinding(
                        business_logic_id=logic.id,
                        property_id=prop.id,
                        role="input",
                        source="inferred",
                        confidence=0.5,
                    )
                )
                created += 1
    return created


def main() -> None:
    with SessionLocal() as db:
        obj_n = _backfill_object_bindings(db)
        prop_n = _backfill_property_bindings(db)
        db.commit()
        print(f"backfilled object bindings: {obj_n}")
        print(f"backfilled property bindings: {prop_n}")


if __name__ == "__main__":
    main()
