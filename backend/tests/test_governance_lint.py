"""G2：规约 linter（agent 事前遵循 + 产物天生合规）。

覆盖三处复用面：Spec 自检、物理表体检、agent 工具入口。每条违规都必须带**可照做的修法**。
"""

from __future__ import annotations

from app.governance import DEFAULT_STANDARD, lint_against_standard, lint_spec
from app.governance.lint import lint_logical_table
from app.warehouse.logical_schema import LogicalColumn, LogicalConstraint, LogicalTable


# ---------- lint_spec：Spec 层命名自检 ----------


def test_lint_spec_flags_non_snake_with_fix():
    vios = lint_spec("transform", {"target_table": "DimCustomer"}, DEFAULT_STANDARD)
    codes = {v.code for v in vios}
    assert "naming_snake_case" in codes
    v = next(v for v in vios if v.code == "naming_snake_case")
    assert v.severity == "warning"
    assert v.fix and "snake" in v.fix.lower()  # 修法可照做
    assert "dim_customer" in v.fix  # 给出建议名


def test_lint_spec_flags_reserved_word():
    vios = lint_spec("transform", {"target_table": "order"}, DEFAULT_STANDARD)
    assert "naming_reserved_word" in {v.code for v in vios}


def test_lint_spec_clean_snake_name():
    assert lint_spec("transform", {"target_table": "dwd_order_line"}, DEFAULT_STANDARD) == []


def test_lint_spec_qualified_checks_table_segment():
    assert lint_spec(
        "transform", {"target_table": "dwd_erp.dwd_order_line"}, DEFAULT_STANDARD
    ) == []


def test_lint_spec_no_target_table_no_violation():
    assert lint_spec("sync", {"source": "s", "target": "t"}, DEFAULT_STANDARD) == []


# ---------- lint_logical_table：物理表体检 ----------


def _table(name, *, comment=None, pk=False):
    cons = (
        (LogicalConstraint(kind="primary_key", columns=("id",)),) if pk else ()
    )
    return LogicalTable(
        name=name,
        database="dim_erp",
        comment=comment,
        columns=(LogicalColumn(name="id"),),
        constraints=cons,
    )


def test_logical_table_clean_with_comment_and_pk():
    vios = lint_logical_table(_table("dim_customer", comment="客户", pk=True), DEFAULT_STANDARD)
    assert vios == []


def test_logical_table_flags_bad_name():
    vios = lint_logical_table(_table("DimCustomer", comment="c", pk=True), DEFAULT_STANDARD)
    assert "naming_snake_case" in {v.code for v in vios}


def test_logical_table_missing_comment_is_advisory():
    vios = lint_logical_table(_table("dim_customer", comment=None, pk=True), DEFAULT_STANDARD)
    comment_vios = [v for v in vios if v.code == "table_comment_missing"]
    assert comment_vios and comment_vios[0].severity == "warning"


def test_logical_table_missing_pk_is_advisory():
    vios = lint_logical_table(_table("dim_customer", comment="c", pk=False), DEFAULT_STANDARD)
    assert "primary_key_missing" in {v.code for v in vios}


# ---------- lint_against_standard：agent 工具入口 ----------


def test_lint_against_standard_returns_jsonable_dicts():
    out = lint_against_standard("transform", {"target_table": "BadName"})
    assert isinstance(out, list) and out
    row = out[0]
    assert {"code", "severity", "message", "fix"} <= set(row)
    assert isinstance(row["fix"], str)


def test_lint_against_standard_clean_is_empty():
    assert lint_against_standard("transform", {"target_table": "dim_customer"}) == []
