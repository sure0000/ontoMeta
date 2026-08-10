"""mapping_json 的表位置替换：对象名同时是别的表的列名时不得误伤。

真实业务库里这不是边角情况——ERPNext 的 724 个对象里 203 个（`sales_order` /
`customer` / `item` …）同时是子表的外键列名。原来的整词替换会把列也改成表名，
产出一条语法合法、语义全错的 SQL；那种错答不会报错，只会悄悄给出错数。
"""

from __future__ import annotations

from app.services.data_app_executor import _apply_mapping

MAP = {"tables": {"sales_order": "`tabSales Order`", "customer": "`tabCustomer`"}}


def test_table_position_is_rewritten():
    assert _apply_mapping("SELECT COUNT(*) FROM sales_order", MAP) == (
        "SELECT COUNT(*) FROM `tabSales Order`"
    )


def test_same_name_used_as_a_column_is_left_alone():
    """`customer` 既是对象名也是子表的外键列——列位置的那个不许动。"""
    out = _apply_mapping("SELECT customer, status FROM sales_order", MAP)
    assert out == "SELECT customer, status FROM `tabSales Order`"


def test_join_and_alias():
    out = _apply_mapping(
        "SELECT s.name FROM sales_order s JOIN customer c ON s.customer = c.name", MAP
    )
    assert out == (
        "SELECT s.name FROM `tabSales Order` s JOIN `tabCustomer` c ON s.customer = c.name"
    )


def test_comma_separated_table_list():
    out = _apply_mapping("SELECT 1 FROM sales_order, customer", MAP)
    assert out == "SELECT 1 FROM `tabSales Order`, `tabCustomer`"


def test_subquery_not_mangled():
    out = _apply_mapping("SELECT * FROM (SELECT customer FROM sales_order) t", MAP)
    assert out == "SELECT * FROM (SELECT customer FROM `tabSales Order`) t"


def test_columns_mapping_still_whole_word():
    out = _apply_mapping("SELECT amt FROM t", {"columns": {"amt": "grand_total"}})
    assert out == "SELECT grand_total FROM t"


def test_empty_mapping_is_identity():
    assert _apply_mapping("SELECT 1 FROM x", None) == "SELECT 1 FROM x"
    assert _apply_mapping("SELECT 1 FROM x", {}) == "SELECT 1 FROM x"


# ---------------------------------------------- StarRocks 多目录三段式限定名


def test_catalog_qualified_table_name():
    """StarRocks 多目录：物理表名可带 catalog.db 前缀,整体替换,不拆段。"""
    map3 = {"tables": {"customer": "erp.erp_db.tabCustomer"}}
    out = _apply_mapping("SELECT * FROM customer", map3)
    assert out == "SELECT * FROM erp.erp_db.tabCustomer"


def test_catalog_qualified_with_join_and_column():
    """三段式下,对象名作为列的场合依旧不误伤。"""
    map3 = {
        "tables": {"sales_order": "erp.erp_db.tabSalesOrder",
                   "customer": "erp.erp_db.tabCustomer"},
    }
    out = _apply_mapping(
        "SELECT s.name FROM sales_order s JOIN customer c ON s.customer = c.name", map3
    )
    assert out == (
        "SELECT s.name FROM erp.erp_db.tabSalesOrder s "
        "JOIN erp.erp_db.tabCustomer c ON s.customer = c.name"
    )


def test_catalog_qualified_internal_plain_name():
    """internal 目录下可以不写前缀(默认目录),二段式照常工作。"""
    map2 = {"tables": {"customer": "dw.dim_customer"}}
    out = _apply_mapping("SELECT * FROM customer", map2)
    assert out == "SELECT * FROM dw.dim_customer"


# ------------------------------------------------------- 驱动值的 JSON 安全化


def test_json_safe_converts_driver_types():
    """MySQL 金额列返回 Decimal，直接进响应体会让 /chat-bi/ask 当场 500。"""
    import datetime
    import decimal

    from app.services.data_app_executor import _json_safe

    assert _json_safe(decimal.Decimal("23364.10")) == 23364.1
    assert isinstance(_json_safe(decimal.Decimal("1")), float)
    assert _json_safe(datetime.date(2026, 8, 7)) == "2026-08-07"
    assert _json_safe(datetime.datetime(2026, 8, 7, 1, 2, 3)) == "2026-08-07T01:02:03"
    assert _json_safe(b"abc") == "abc"
    for v in (None, True, 1, 1.5, "s"):
        assert _json_safe(v) is v


def test_executed_rows_are_json_serializable(tmp_path):
    """端到端：execute_sql 出来的行必须能直接 json.dumps，不靠 default=str 兜。"""
    import json

    from sqlalchemy import create_engine, text

    from app.services.data_app_executor import execute_sql

    dsn = f"sqlite:///{tmp_path / 'j.db'}"
    with create_engine(dsn).begin() as conn:
        conn.execute(text("CREATE TABLE t (d DATE, n NUMERIC)"))
        conn.execute(text("INSERT INTO t VALUES ('2026-08-07', 1.25)"))
    _cols, rows = execute_sql(dsn=dsn, sql="SELECT d, n FROM t", limit=10)
    json.dumps(rows)  # 不抛即通过
