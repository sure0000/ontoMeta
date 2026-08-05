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
