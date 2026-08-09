"""G1：规约接进 Validation Gate 的行为测试。

钉两件事：
1. **enforced 规则行为不变**——必填字段缺失、Spec 带凭据仍是**阻断级**，且沿用原 code。
2. **advisory 规则呈现不阻断**——命名不合规约产 warning，``is_blocking`` 判为 False。

判据已全部来自 ``active_standard``，故这些断言同时验证「闸门读规约」这条接线正确。
"""

from __future__ import annotations

import pytest

from app.agents.validation import is_blocking, validate_spec
from app.database import SessionLocal


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _codes(issues):
    return {i.code for i in issues}


def _blocking_codes(issues):
    return {i.code for i in issues if is_blocking(i)}


# ---------- enforced：必填字段（行为不变，阻断级） ----------


def test_missing_required_field_blocks(db):
    # transform 缺 ontology_id
    issues = validate_spec(
        db, kind="transform", spec={"target_table": "dim_customer"}, ontology_id=None
    )
    assert "missing_required_field" in _blocking_codes(issues)


def test_all_required_fields_present_no_missing(db):
    issues = validate_spec(
        db,
        kind="sync",
        spec={"source": "s", "target": "t"},
        ontology_id=None,
    )
    assert "missing_required_field" not in _codes(issues)


def test_metric_without_subject_blocks(db):
    issues = validate_spec(
        db, kind="metric", spec={"metric_name": "gmv"}, ontology_id=None
    )
    blocking = _blocking_codes(issues)
    assert "missing_required_field" in blocking


def test_metric_with_subject_ok(db):
    issues = validate_spec(
        db,
        kind="metric",
        spec={"metric_name": "gmv", "subject_objects": ["Order"]},
        ontology_id=None,
    )
    assert "missing_required_field" not in _codes(issues)


# ---------- enforced：凭据（行为不变，阻断级） ----------


def test_credential_in_spec_blocks(db):
    issues = validate_spec(
        db,
        kind="sync",
        spec={"source": "s", "target": "t", "password": "hunter2"},
        ontology_id=None,
    )
    assert "credential_in_spec" in _blocking_codes(issues)


def test_credential_ref_is_allowed(db):
    # *_ref 指向密钥存储，须放行
    issues = validate_spec(
        db,
        kind="sync",
        spec={"source": "s", "target": "t", "password_ref": "vault://db"},
        ontology_id=None,
    )
    assert "credential_in_spec" not in _codes(issues)


# ---------- advisory：命名（呈现但不阻断） ----------


def test_non_snake_case_table_warns_not_blocks(db):
    issues = validate_spec(
        db,
        kind="transform",
        spec={"target_table": "DimCustomer", "ontology_id": "x"},
        ontology_id=None,
    )
    assert "naming_snake_case" in _codes(issues)
    assert "naming_snake_case" not in _blocking_codes(issues)  # advisory：不阻断


def test_reserved_word_table_warns_not_blocks(db):
    issues = validate_spec(
        db,
        kind="transform",
        spec={"target_table": "order", "ontology_id": "x"},
        ontology_id=None,
    )
    assert "naming_reserved_word" in _codes(issues)
    assert "naming_reserved_word" not in _blocking_codes(issues)


def test_snake_case_table_clean(db):
    issues = validate_spec(
        db,
        kind="transform",
        spec={"target_table": "dwd_order_line", "ontology_id": "x"},
        ontology_id=None,
    )
    assert "naming_snake_case" not in _codes(issues)
    assert "naming_reserved_word" not in _codes(issues)


def test_qualified_table_name_checks_last_segment(db):
    # 「库.表」只校验表名段；合规表名不应因带库前缀而误报
    issues = validate_spec(
        db,
        kind="transform",
        spec={"target_table": "dwd_erp.dwd_order_line", "ontology_id": "x"},
        ontology_id=None,
    )
    assert "naming_snake_case" not in _codes(issues)


# ---------- P2：物化的提交前自检并入闸门 ----------


def test_materialize_preflight_blocks_when_airflow_unavailable(db):
    """未配 Airflow 时物化过不了闸门。

    物化弹窗强制「自检通过才让提交」，而 Data Agent 那条路原先直接 validate→confirm→execute，
    同一件破坏性操作走了两套门槛。闸门是两条路唯一的公共必经点，判据放这里两边才守同一条线。
    """
    from app.models import DataSource

    db.add(DataSource(id="ds-pf", name="仓库", kind="hive", status="ok",
                      dsn_secret_ref="ref://pf"))
    db.commit()
    try:
        issues = validate_spec(
            db,
            kind="materialize",
            spec={"ontology_id": "onto-pf", "target_datasource_id": "ds-pf", "engine": "hive"},
            ontology_id="onto-pf",
        )
    finally:
        db.query(DataSource).filter(DataSource.id == "ds-pf").delete()
        db.commit()
    assert "preflight_blocked" in _blocking_codes(issues)
    assert any("Airflow" in i.message for i in issues if i.code == "preflight_blocked")


def test_materialize_preflight_skipped_without_datasource(db):
    """缺目标数据源时不跑自检——那由 missing_required_field 报，不在这里重复喊一遍。"""
    issues = validate_spec(
        db, kind="materialize", spec={"ontology_id": "onto-pf"}, ontology_id="onto-pf"
    )
    assert "preflight_blocked" not in _codes(issues)
    assert "missing_required_field" in _blocking_codes(issues)


def test_preflight_warning_codes_do_not_block():
    """自检的提醒项与「自检没跑成」呈现但不拦——「验了不通过」和「没验成」是两回事。"""
    from app.services.draft_consistency import ValidationIssue

    for code in ("preflight_warning", "preflight_unavailable"):
        assert not is_blocking(ValidationIssue(code=code, message="m", entity_type="artifact"))


# ---------- 本体一致性问题的作用域 ----------


def test_unrelated_ontology_issues_are_folded(db, monkeypatch):
    """只留与本任务相关的本体问题，其余折成一条计数。

    由来：ERP 本体一次校验产出 188 条，185 条与本次任务无关。全量抄进每份报告，
    等于把唯一那条该看的埋进噪声里。
    """
    import app.agents.validation as validation
    from app.models import DomainContext, Ontology, OntologyStatus
    from app.services.draft_consistency import ValidationIssue

    domain = DomainContext(datahub_domain_id="urn:li:domain:scope", name="scope")
    db.add(domain)
    db.flush()
    onto = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
    )
    db.add(onto)
    db.commit()

    monkeypatch.setattr(
        validation,
        "validate_ontology",
        lambda _db, _oid: [
            ValidationIssue(code="x", message="本任务的表有问题",
                            entity_type="object_type", entity_name="customer"),
            ValidationIssue(code="x", message="别的表有问题",
                            entity_type="object_type", entity_name="other_a"),
            ValidationIssue(code="x", message="又一张别的表",
                            entity_type="object_type", entity_name="other_b"),
        ],
    )
    issues = validate_spec(
        db,
        kind="transform",
        spec={"ontology_id": onto.id, "target_table": "customer"},
        ontology_id=onto.id,
    )
    ontology_issues = [i for i in issues if i.code == "ontology_issue"]
    assert len(ontology_issues) == 2  # 1 条相关 + 1 条折叠汇总
    assert any("本任务的表有问题" in i.message for i in ontology_issues)
    assert any("另有 2 条" in i.message for i in ontology_issues)
    # 折叠的仍是 warning 级，不会把不相关的问题变成阻断项
    assert not any(is_blocking(i) for i in ontology_issues)
