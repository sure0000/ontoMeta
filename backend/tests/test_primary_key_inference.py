"""身份属性推断：``primary_key_name`` 与 ``primary_key_is_confident`` 必须在说同一列。

真实事故（Odoo 本体，483 个对象都是这个形状）：``sale_order`` 有 ``id``（真主键），
另有 20 个 ``*_id`` 外键列全被标成 identifier 语义。旧实现里——

- ``primary_key_is_confident`` 看到 ``id`` → True（"可以据以发强制约束"）
- ``primary_key_name`` 没找到 ``sale_order_id`` → 直接跳到字典序第一个标识字段
  ``analytic_account_id``（一个大量为空的外键）

于是 ODS 表被建成 ``PRIMARY KEY (analytic_account_id)``——装不进自己的源数据。
两个函数一个说"有把握"、一个给出另一列，这种错位比"猜不准"危险得多。
"""

from __future__ import annotations

import pytest

from app.services.ontology_projection import primary_key_is_confident, primary_key_name


def test_bare_id_beats_first_identifier_column():
    """真实 Odoo sale_order 的形状：有 id，外加一堆 *_id 外键都是 identifier 语义。"""
    names = ["id", "name", "analytic_account_id", "partner_id", "warehouse_id"]
    identifiers = ["analytic_account_id", "partner_id", "warehouse_id"]
    assert primary_key_name("sale_order", names, identifiers) == "id"
    assert primary_key_is_confident("sale_order", names, identifiers) is True


def test_object_scoped_id_still_wins_over_bare_id():
    names = ["customer_id", "id", "ref_id"]
    assert primary_key_name("customer", names, ["ref_id"]) == "customer_id"


@pytest.mark.parametrize(
    "obj,names,identifiers",
    [
        # 只有一个自定义扩展字段像 id：既不唯一也大量为空，不能据以发约束。
        ("bank", ["custom_external_id", "name"], ["custom_external_id"]),
        # 一个标识字段都没有。
        ("order_line", ["qty", "price"], []),
    ],
)
def test_no_convention_means_not_confident(obj, names, identifiers):
    confident = primary_key_is_confident(obj, names, identifiers)
    assert confident is False
    # 仍给出身份属性供语义导航/去重使用——猜错顶多结果不准；
    # 但既然不 confident，就不会被拿去发强制约束。
    assert primary_key_name(obj, names, identifiers) == (identifiers[0] if identifiers else None)


def test_confident_implies_the_named_column_is_the_conventional_one():
    """不变式：一旦判为 confident，给出的列必须是命名约定里的那个。

    这条正是旧实现破掉的那条——错位一旦发生，建出来的表永远装不进数据。
    """
    cases = [
        ("sale_order", ["id", "analytic_account_id"], ["analytic_account_id"]),
        ("customer", ["customer_id", "other_id"], ["other_id"]),
        ("invoice", ["id"], []),
    ]
    for obj, names, identifiers in cases:
        if primary_key_is_confident(obj, names, identifiers):
            assert primary_key_name(obj, names, identifiers) in (f"{obj}_id", "id")
