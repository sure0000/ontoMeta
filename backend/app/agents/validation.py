"""Validation Gate：数据标准规范的强制执行点。

标准不是一份文档，而是**代码里的闸门**——每个制品在人工确认**之前**必须过这里，
不过闸门就不能确认、更不能执行。

复用而非另写：``ValidationIssue`` / ``DraftConsistencyError`` 直接取自
``services/draft_consistency.py``，本体范围的校验直接调用其 ``validate_ontology``。
若在此另立一套 issue 类型，两份校验语义迟早分叉。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import ObjectType, Ontology, Property
from app.models.agent import ArtifactKind
from app.services.draft_consistency import ValidationIssue, validate_ontology
from app.warehouse import UnknownEngineError, get_adapter

# error 级阻断确认；warning 级必须呈现但不阻断。
_WARNING_CODES = frozenset(
    {
        "engine_unverified",
        "ontology_issue",
    }
)


def is_blocking(issue: ValidationIssue) -> bool:
    return issue.code not in _WARNING_CODES


def validate_spec(
    db: Session, *, kind: str, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """校验一个治理制品的 Spec。返回问题清单（可能为空）。"""
    issues: list[ValidationIssue] = []

    if not isinstance(spec, dict) or not spec:
        issues.append(
            ValidationIssue(
                code="spec_empty",
                message="Spec 为空或不是对象，无法校验",
                entity_type="artifact",
            )
        )
        return issues

    issues.extend(_check_engines(spec))
    issues.extend(_check_ontology_refs(db, spec, ontology_id))
    issues.extend(_check_required_metadata(kind, spec))

    # 本体范围的制品：连带跑既有的发布前一致性校验（warning 级，不阻断制品本身）。
    if ontology_id and db.query(Ontology).filter(Ontology.id == ontology_id).first():
        for issue in validate_ontology(db, ontology_id):
            issues.append(
                ValidationIssue(
                    code="ontology_issue",
                    message=f"本体一致性：{issue.message}",
                    entity_type=issue.entity_type,
                    entity_id=issue.entity_id,
                    entity_name=issue.entity_name,
                )
            )
    return issues


def _check_engines(spec: dict[str, Any]) -> list[ValidationIssue]:
    """目标引擎必须存在；未核实能力矩阵的引擎给 warning。"""
    issues: list[ValidationIssue] = []
    engines = spec.get("engines") or ([spec["engine"]] if spec.get("engine") else [])
    for engine in engines:
        try:
            adapter = get_adapter(str(engine))
        except UnknownEngineError as exc:
            issues.append(
                ValidationIssue(
                    code="engine_unknown",
                    message=str(exc),
                    entity_type="engine",
                    entity_name=str(engine),
                )
            )
            continue
        if not adapter.capabilities().verified:
            issues.append(
                ValidationIssue(
                    code="engine_unverified",
                    message=f"引擎 {engine} 的能力矩阵尚未逐项核实，需实施前验证",
                    entity_type="engine",
                    entity_name=str(engine),
                )
            )
    return issues


def _check_ontology_refs(
    db: Session, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """防 LLM 幻觉：Spec 引用的对象/字段必须在本体中真实存在。

    这是 Gate 最重要的一条——LLM 可能凭空造出不存在的表名或列名，
    若不在此拦下，会一路走到执行器才炸，且可能已产生副作用。
    """
    issues: list[ValidationIssue] = []
    object_names = [str(n) for n in (spec.get("object_types") or []) if n]
    property_names = [str(n) for n in (spec.get("properties") or []) if n]
    if not object_names and not property_names:
        return issues

    if not ontology_id:
        issues.append(
            ValidationIssue(
                code="ontology_missing",
                message="Spec 引用了本体对象，但制品未绑定 ontology_id",
                entity_type="artifact",
            )
        )
        return issues

    known_objects = {
        name
        for (name,) in db.query(ObjectType.name).filter(
            ObjectType.ontology_id == ontology_id
        )
    }
    for name in object_names:
        if name not in known_objects:
            issues.append(
                ValidationIssue(
                    code="unknown_object",
                    message=f"Spec 引用的对象 {name} 不在本体中（疑似 LLM 幻觉）",
                    entity_type="object_type",
                    entity_name=name,
                )
            )

    if property_names:
        known_properties = {
            name
            for (name,) in db.query(Property.name)
            .join(ObjectType, Property.object_type_id == ObjectType.id)
            .filter(ObjectType.ontology_id == ontology_id)
        }
        for name in property_names:
            if name not in known_properties:
                issues.append(
                    ValidationIssue(
                        code="unknown_property",
                        message=f"Spec 引用的字段 {name} 不在本体中（疑似 LLM 幻觉）",
                        entity_type="property",
                        entity_name=name,
                    )
                )
    return issues


# 各制品类型的必填字段。缺失即阻断——宁可退回重拟，不可带着窟窿执行。
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    ArtifactKind.CLUSTER.value: ("hosts", "services"),
    ArtifactKind.SYNC.value: ("source", "target"),
    ArtifactKind.TRANSFORM.value: ("target_table", "ontology_id"),
    ArtifactKind.METRIC.value: ("metric_name",),
}


def _check_required_metadata(kind: str, spec: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field in _REQUIRED_FIELDS.get(kind, ()):
        if not spec.get(field):
            issues.append(
                ValidationIssue(
                    code="missing_required_field",
                    message=f"{kind} 制品缺少必填字段 {field}",
                    entity_type="artifact",
                    entity_name=field,
                )
            )
    # 指标必须绑定主对象：聚合 SQL 的 FROM 需要一张源表，缺失时以往会生成看似合法、
    # 实则无法执行的 `FROM <主对象未绑定>`（遗留1）。在起草期就阻断，不带窟窿执行。
    if kind == ArtifactKind.METRIC.value and not (
        spec.get("subject_objects") or spec.get("object_types")
    ):
        issues.append(
            ValidationIssue(
                code="missing_required_field",
                message="metric 制品未绑定主对象（subject_objects/object_types 均为空），"
                "无法确定聚合 SQL 的 FROM 源表",
                entity_type="artifact",
                entity_name="subject_objects",
            )
        )
    # 凭据绝不应出现在 Spec 里——LLM 上下文与制品存储都不得承载密钥。
    # 但 ``*_ref`` / ``*_alias`` 是**指向**密钥存储的引用，正是要鼓励的写法，须放行。
    for key in spec:
        lowered = str(key).lower()
        if lowered.endswith(("_ref", "_alias")):
            continue
        if any(
            token in lowered
            for token in ("password", "secret", "token", "private_key", "credential")
        ):
            issues.append(
                ValidationIssue(
                    code="credential_in_spec",
                    message=f"Spec 中出现疑似凭据字段 {key}：凭据必须独立存储，"
                    f"Spec 只能承载主机别名",
                    entity_type="artifact",
                    entity_name=str(key),
                )
            )
    return issues
