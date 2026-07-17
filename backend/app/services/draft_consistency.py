"""草稿/发布前一致性校验。"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ObjectType,
    Ontology,
    Property,
    RelationType,
)


@dataclass
class ValidationIssue:
    code: str
    message: str
    entity_type: str
    entity_id: str | None = None
    entity_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class DraftConsistencyError(ValueError):
    """发布前一致性校验失败。"""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        summary = "; ".join(i.message for i in issues[:5])
        if len(issues) > 5:
            summary += f"；另有 {len(issues) - 5} 项"
        super().__init__(f"一致性校验失败（{len(issues)}）: {summary}")


def validate_ontology(db: Session, ontology_id: str) -> list[ValidationIssue]:
    """校验关系端点、属性归属、逻辑绑定是否落在同一本体内。"""
    ontology = db.get(Ontology, ontology_id)
    if not ontology:
        return [
            ValidationIssue(
                code="ontology_not_found",
                message="本体不存在",
                entity_type="ontology",
                entity_id=ontology_id,
            )
        ]

    issues: list[ValidationIssue] = []
    objects = (
        db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
    )
    object_ids = {o.id for o in objects}
    object_by_id = {o.id: o for o in objects}

    # 对象名重复
    seen_obj_names: dict[str, str] = {}
    for obj in objects:
        if obj.name in seen_obj_names:
            issues.append(
                ValidationIssue(
                    code="duplicate_object_name",
                    message=f"对象标识重复: {obj.name}",
                    entity_type="object_type",
                    entity_id=obj.id,
                    entity_name=obj.name,
                )
            )
        else:
            seen_obj_names[obj.name] = obj.id

    # 属性归属：object_type 必须属于本本体
    props = (
        db.query(Property)
        .join(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    )
    prop_ids = {p.id for p in props}
    for prop in props:
        if prop.object_type_id not in object_ids:
            issues.append(
                ValidationIssue(
                    code="property_orphan_object",
                    message=f"属性 {prop.name} 的归属对象不在本体内",
                    entity_type="property",
                    entity_id=prop.id,
                    entity_name=prop.name,
                )
            )

    # 同对象内属性名重复
    props_by_object: dict[str, list[Property]] = {}
    for prop in props:
        props_by_object.setdefault(prop.object_type_id, []).append(prop)
    for object_type_id, plist in props_by_object.items():
        seen: dict[str, str] = {}
        obj = object_by_id.get(object_type_id)
        for prop in plist:
            if prop.name in seen:
                issues.append(
                    ValidationIssue(
                        code="duplicate_property_name",
                        message=(
                            f"对象 {obj.name if obj else object_type_id} "
                            f"下属性标识重复: {prop.name}"
                        ),
                        entity_type="property",
                        entity_id=prop.id,
                        entity_name=prop.name,
                    )
                )
            else:
                seen[prop.name] = prop.id

    # 关系端点必须存在且属于本本体
    relations = (
        db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
    )
    seen_rel_names: dict[str, str] = {}
    for rel in relations:
        if rel.name in seen_rel_names:
            issues.append(
                ValidationIssue(
                    code="duplicate_relation_name",
                    message=f"关系标识重复: {rel.name}",
                    entity_type="relation_type",
                    entity_id=rel.id,
                    entity_name=rel.name,
                )
            )
        else:
            seen_rel_names[rel.name] = rel.id

        if rel.source_object_type_id not in object_ids:
            issues.append(
                ValidationIssue(
                    code="relation_source_missing",
                    message=f"关系 {rel.name} 的源对象不存在或不属于本本体",
                    entity_type="relation_type",
                    entity_id=rel.id,
                    entity_name=rel.name,
                )
            )
        if rel.target_object_type_id not in object_ids:
            issues.append(
                ValidationIssue(
                    code="relation_target_missing",
                    message=f"关系 {rel.name} 的目标对象不存在或不属于本本体",
                    entity_type="relation_type",
                    entity_id=rel.id,
                    entity_name=rel.name,
                )
            )
        if rel.mapping_object_type_id and rel.mapping_object_type_id not in object_ids:
            issues.append(
                ValidationIssue(
                    code="relation_mapping_missing",
                    message=f"关系 {rel.name} 的映射对象不存在或不属于本本体",
                    entity_type="relation_type",
                    entity_id=rel.id,
                    entity_name=rel.name,
                )
            )

    # 逻辑绑定有效
    logics = (
        db.query(BusinessLogic).filter(BusinessLogic.ontology_id == ontology_id).all()
    )
    logic_ids = {logic.id for logic in logics}
    seen_logic_names: dict[str, str] = {}
    for logic in logics:
        if logic.name in seen_logic_names:
            issues.append(
                ValidationIssue(
                    code="duplicate_logic_name",
                    message=f"业务逻辑标识重复: {logic.name}",
                    entity_type="business_logic",
                    entity_id=logic.id,
                    entity_name=logic.name,
                )
            )
        else:
            seen_logic_names[logic.name] = logic.id

    obj_bindings = (
        db.query(BusinessLogicObjectBinding)
        .join(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology_id)
        .all()
    )
    for binding in obj_bindings:
        if binding.business_logic_id not in logic_ids:
            issues.append(
                ValidationIssue(
                    code="logic_object_binding_logic_missing",
                    message="对象绑定引用的业务逻辑不在本体内",
                    entity_type="business_logic_object_binding",
                    entity_id=binding.id,
                )
            )
        if binding.object_type_id not in object_ids:
            logic = db.get(BusinessLogic, binding.business_logic_id)
            issues.append(
                ValidationIssue(
                    code="logic_object_binding_object_missing",
                    message=(
                        f"业务逻辑 {logic.name if logic else binding.business_logic_id} "
                        "绑定的对象不存在或不属于本本体"
                    ),
                    entity_type="business_logic_object_binding",
                    entity_id=binding.id,
                    entity_name=logic.name if logic else None,
                )
            )

    prop_bindings = (
        db.query(BusinessLogicPropertyBinding)
        .join(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology_id)
        .all()
    )
    for binding in prop_bindings:
        if binding.business_logic_id not in logic_ids:
            issues.append(
                ValidationIssue(
                    code="logic_property_binding_logic_missing",
                    message="属性绑定引用的业务逻辑不在本体内",
                    entity_type="business_logic_property_binding",
                    entity_id=binding.id,
                )
            )
        if binding.property_id not in prop_ids:
            logic = db.get(BusinessLogic, binding.business_logic_id)
            issues.append(
                ValidationIssue(
                    code="logic_property_binding_property_missing",
                    message=(
                        f"业务逻辑 {logic.name if logic else binding.business_logic_id} "
                        "绑定的属性不存在或不属于本本体"
                    ),
                    entity_type="business_logic_property_binding",
                    entity_id=binding.id,
                    entity_name=logic.name if logic else None,
                )
            )

    return issues


def assert_ontology_consistent(db: Session, ontology_id: str) -> None:
    issues = validate_ontology(db, ontology_id)
    if issues:
        raise DraftConsistencyError(issues)
