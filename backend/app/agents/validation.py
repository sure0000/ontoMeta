"""Validation Gate：数据标准规范的强制执行点。

标准不是一份文档，而是**代码里的闸门**——每个制品在人工确认**之前**必须过这里，
不过闸门就不能确认、更不能执行。

**判据来源（G1）**：闸门不再自持判据字面值，而是读 ``app.governance.active_standard(db)``
——必填字段、凭据词元、命名约定都由那份**声明式规约**给出。本文件只负责「怎么查」，
「查什么」归规约。这样 agent 提议（读规约当约束）与执行闸门（按规约拦截）咬同一份标准。
enforced 规则（missing_required_field / credential_in_spec）沿用原 code，行为逐字节不变。

复用而非另写：``ValidationIssue`` / ``DraftConsistencyError`` 直接取自
``services/draft_consistency.py``，本体范围的校验直接调用其 ``validate_ontology``。
若在此另立一套 issue 类型，两份校验语义迟早分叉。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.governance import GovernanceStandard, active_standard
from app.governance.lint import lint_spec
from app.models import ObjectType, Ontology, Property
from app.services.draft_consistency import ValidationIssue, validate_ontology
from app.warehouse import UnknownEngineError, get_adapter

# 与规约无关的结构性 warning（引擎未核实、本体一致性）。规约条款的 error/warning
# 由其自身 severity 决定，不在这里维护——见 is_blocking。
_WARNING_CODES = frozenset(
    {
        "engine_unverified",
        "ontology_issue",
        # 提交前自检的**提醒项**（blocking=False）与「自检本身没跑成」——呈现但不拦。
        "preflight_warning",
        "preflight_unavailable",
    }
)


def _standard_warning_codes() -> frozenset[str]:
    """规约里 severity=warning 的条款码——这些进闸门也不阻断确认。"""
    return frozenset(
        r.code for r in active_standard().all_rules() if r.severity == "warning"
    )


def is_blocking(issue: ValidationIssue) -> bool:
    """error 级阻断确认；warning 级（结构性 + 规约声明的 warning 条款）呈现但不阻断。"""
    return issue.code not in _WARNING_CODES and issue.code not in _standard_warning_codes()


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

    standard = active_standard(db)
    issues.extend(_check_engines(spec))
    issues.extend(_check_ontology_refs(db, spec, ontology_id))
    issues.extend(_check_required_metadata(kind, spec, standard))
    issues.extend(_check_standard(kind, spec, standard))
    if kind == "materialize":
        issues.extend(_check_materialize_preflight(db, spec, ontology_id))

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


# 各制品类型的必填字段与凭据规则，判据来自规约（app.governance）——此处不再自持字面值。
# enforced 规则沿用既有 code（missing_required_field / credential_in_spec），确保接线后
# 拒绝码分布与遥测不变。


def _check_required_metadata(
    kind: str, spec: dict[str, Any], standard: GovernanceStandard
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field in standard.required_metadata.per_artifact.get(kind, ()):
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
    if kind == "metric" and not (
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
    sec = standard.security
    for key in spec:
        lowered = str(key).lower()
        if lowered.endswith(sec.allowed_ref_suffixes):
            continue
        if any(token in lowered for token in sec.forbidden_tokens):
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


def _check_materialize_preflight(
    db: Session, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """物化的提交前自检并入闸门（Airflow 连通/鉴权/建表连接/DAG 目录）。

    **为什么在这里**：物化弹窗强制「跑完自检且无阻断项」才让提交，而 Data Agent 那条路
    直接 validate→confirm→execute，同一件破坏性操作走了两套门槛——agent 提的任务会在
    三分钟后的任务日志里失败，而弹窗提的当场就被拦住。闸门是两条路唯一的公共必经点，
    判据放这里两边才守同一条线。

    自检本身跑不起来（缺 target_datasource_id、报错）不算阻断：那属于「没验成」，
    与「验了不通过」是两回事，故给 warning 级并说清原因。
    """
    target_datasource_id = spec.get("target_datasource_id")
    if not ontology_id or not target_datasource_id:
        # 缺这两样另有 missing_required_field 报，不在这里重复喊一遍。
        return []
    from app.services.materialize_preflight import run_preflight

    try:
        report = run_preflight(
            db,
            ontology_id,
            target_datasource_id=str(target_datasource_id),
            engine=str(spec.get("engine") or "hive"),
            selected_targets=spec.get("selected_targets"),
        )
    except Exception as exc:  # noqa: BLE001 — 自检炸了不该把校验一起带走
        return [
            ValidationIssue(
                code="preflight_unavailable",
                message=f"提交前自检未能运行（{exc}）；执行前请自行确认编排环境可用",
                entity_type="artifact",
            )
        ]
    return [
        ValidationIssue(
            code="preflight_blocked" if (item.status == "fail" and item.blocking)
            else "preflight_warning",
            message=f"提交前自检 · {item.label}：{item.detail}"
            + (f"（下一步：{item.next_step}）" if item.next_step else ""),
            entity_type="artifact",
            entity_name=item.key,
        )
        for item in report.items
        if item.status != "pass"
    ]


def _check_standard(
    kind: str, spec: dict[str, Any], standard: GovernanceStandard
) -> list[ValidationIssue]:
    """规约里可在 Spec 层校验的条款（命名 advisory）。

    判据委托 ``governance.lint.lint_spec``——命名规则只有那一处定义（agent 自检与本闸门
    共用同一份），此处只把 ``Violation`` 投影成 ``ValidationIssue``。这些条款 severity=warning，
    经 ``is_blocking`` 判为不阻断——**呈现而非拦截**，避免误伤存量、引入回归。
    """
    return [
        ValidationIssue(
            code=v.code,
            message=v.message,
            entity_type=v.entity_type,
            entity_name=v.entity_name,
        )
        for v in lint_spec(kind, spec, standard)
    ]
