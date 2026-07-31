"""对象标识名去碰撞纯函数。"""

from __future__ import annotations

from app.services.object_naming import dedupe_object_names, table_name_from_ref

_U = "urn:li:dataset:(urn:li:dataPlatform:mariadb,_3214abce8e7be3d7.{},PROD)"


def test_table_name_strips_frappe_tab_prefix():
    assert table_name_from_ref(_U.format("tabProcess Period Closing Voucher")) == (
        "process_period_closing_voucher"
    )
    assert table_name_from_ref(_U.format("tabWorkflow Action")) == "workflow_action"


def test_table_name_keeps_non_frappe_tab():
    # tab 后非大写不剥离，避免误伤真实表名
    assert table_name_from_ref(_U.format("tabular_report")) == "tabular_report"


def test_table_name_plain_and_empty():
    assert table_name_from_ref("plain.orders") == "orders"
    assert table_name_from_ref(None) == ""


def test_dedupe_renames_collision_members_to_table_name():
    entries = [
        ("c1", "period_closing_voucher", _U.format("tabPeriod Closing Voucher")),
        ("c2", "period_closing_voucher", _U.format("tabProcess Period Closing Voucher")),
        ("c3", "workflow_action", _U.format("tabWorkflow Action")),
        ("c4", "workflow_action", _U.format("tabWorkflow Action Master")),
        ("c5", "customer", _U.format("tabCustomer")),
    ]
    out = dedupe_object_names(entries)
    # 自然属主保留短名，Process/Master 兄弟改用源表名
    assert out["c1"] == "period_closing_voucher"
    assert out["c2"] == "process_period_closing_voucher"
    assert out["c3"] == "workflow_action"
    assert out["c4"] == "workflow_action_master"
    assert out["c5"] == "customer"  # 单例不动
    assert len(set(out.values())) == len(out)  # 全唯一


def test_dedupe_numeric_fallback_when_table_names_also_collide():
    # 源表名也一样（真正同名不同 urn env）→ 数字后缀兜底，仍保证唯一
    entries = [
        ("a", "invoice", "urn:li:dataset:(p,s.Invoice,PROD)"),
        ("b", "invoice", "urn:li:dataset:(p,s.Invoice,DEV)"),
    ]
    out = dedupe_object_names(entries)
    assert set(out.values()) == {"invoice", "invoice_2"}
