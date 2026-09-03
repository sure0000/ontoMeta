"""通用 SQL 血缘解析单元测试。

钉住四件事：
1. 落点的三种写法（INSERT INTO / CREATE TABLE AS / CREATE VIEW）都能认出来；
2. CTE 不算上游表，但它内部引用的真实表算，且引用 CTE 的关联键能追一层投影；
3. **关联键解析不出来就不给**——宁可只有表级边，也不给编出来的键；
4. 野生代码包里的失败形态（存储过程、空文件、方言不符）返回**能给人看的原因**，
   不是抛异常。
"""

from __future__ import annotations

from app.services.sql_lineage_extractor import (
    EMPTY_FILE_HINT,
    STORED_PROCEDURE_HINT,
    extract,
)

DB = "_d71df877e93eac81"


def _keys(lineage) -> set[str]:
    return {key.render() for key in lineage.join_keys}


# --------------------------------------------------------------------------- 落点


def test_insert_into_select_target_and_sources():
    result = extract(
        f"""
        INSERT INTO `{DB}`.`ext_item_price_index`
        SELECT it.name, ip.price_list_rate
        FROM `{DB}`.`tabItem` it
        JOIN `{DB}`.`tabItem Price` ip ON ip.item_code = it.name
        """
    )

    assert result.error is None
    assert result.statements == 1
    (lineage,) = result.lineages
    assert lineage.target == f"{DB}.ext_item_price_index"
    assert lineage.sources == (f"{DB}.tabItem", f"{DB}.tabItem Price")
    assert _keys(lineage) == {f"{DB}.tabItem Price.item_code = {DB}.tabItem.name"}


def test_create_table_as_select():
    result = extract(
        f"""
        CREATE TABLE `{DB}`.`stg_shipment` AS
        SELECT dn.name FROM `{DB}`.`tabDelivery Note` dn
        JOIN `{DB}`.`tabDelivery Note Item` dni ON dni.parent = dn.name
        """
    )

    (lineage,) = result.lineages
    assert lineage.target == f"{DB}.stg_shipment"
    assert set(lineage.sources) == {f"{DB}.tabDelivery Note", f"{DB}.tabDelivery Note Item"}


def test_create_view():
    result = extract(
        f"CREATE VIEW `{DB}`.`v_flag` AS SELECT d.customer_id FROM `{DB}`.`ext_credit` d"
    )

    (lineage,) = result.lineages
    assert lineage.target == f"{DB}.v_flag"
    assert lineage.sources == (f"{DB}.ext_credit",)


def test_multiple_statements_in_one_file():
    result = extract(
        f"""
        INSERT INTO `{DB}`.`a` SELECT x.id FROM `{DB}`.`src_a` x;
        CREATE VIEW `{DB}`.`b` AS SELECT y.id FROM `{DB}`.`a` y;
        """
    )

    assert result.statements == 2
    assert [item.target for item in result.lineages] == [f"{DB}.a", f"{DB}.b"]


# --------------------------------------------------------------------------- 没有落点


def test_plain_select_has_no_landing():
    """裸 SELECT 不是解析失败，只是没有可推的落点。"""
    result = extract(f"SELECT * FROM `{DB}`.`tabItem`")

    assert result.error is None
    assert result.statements == 1
    assert result.lineages == []


def test_update_has_no_landing():
    result = extract(f"UPDATE `{DB}`.`tabItem` SET disabled = 1 WHERE name = 'x'")

    assert result.error is None
    assert result.lineages == []


# --------------------------------------------------------------------------- CTE


def test_cte_is_not_a_source_table_but_its_tables_are():
    result = extract(
        f"""
        INSERT INTO `{DB}`.`ext_credit`
        WITH pay AS (
          SELECT p.party AS customer, SUM(p.paid_amount) AS paid_amt
          FROM `{DB}`.`tabPayment Entry` p WHERE p.docstatus = 1 GROUP BY p.party
        )
        SELECT c.name, pay.paid_amt
        FROM `{DB}`.`tabCustomer` c
        LEFT JOIN pay ON pay.customer = c.name
        """
    )

    (lineage,) = result.lineages
    assert "pay" not in lineage.sources
    assert set(lineage.sources) == {f"{DB}.tabCustomer", f"{DB}.tabPayment Entry"}
    # 引用 CTE 别名的键追一层投影：pay.customer 即 tabPayment Entry.party
    assert _keys(lineage) == {f"{DB}.tabPayment Entry.party = {DB}.tabCustomer.name"}


def test_unresolvable_cte_column_drops_the_key_not_the_edge():
    """CTE 里是聚合表达式，追不到真实列——丢键，但表级边仍在。"""
    result = extract(
        f"""
        INSERT INTO `{DB}`.`ext_credit`
        WITH agg AS (
          SELECT SUM(p.paid_amount) AS total FROM `{DB}`.`tabPayment Entry` p
        )
        SELECT c.name FROM `{DB}`.`tabCustomer` c
        LEFT JOIN agg ON agg.total = c.name
        """
    )

    (lineage,) = result.lineages
    assert set(lineage.sources) == {f"{DB}.tabCustomer", f"{DB}.tabPayment Entry"}
    assert lineage.join_keys == ()


# --------------------------------------------------------------------------- 关联键


def test_constant_predicate_is_not_a_join_key():
    result = extract(
        f"""
        INSERT INTO `{DB}`.`a`
        SELECT i.name FROM `{DB}`.`tabSales Invoice` i
        JOIN `{DB}`.`tabCustomer` c ON i.customer = c.name
        WHERE i.docstatus = 1
        """
    )

    (lineage,) = result.lineages
    assert _keys(lineage) == {f"{DB}.tabSales Invoice.customer = {DB}.tabCustomer.name"}


def test_where_style_join_counts_as_a_key():
    """老 SQL 用逗号连表、条件写在 WHERE 里，那也是关联键。"""
    result = extract(
        f"""
        INSERT INTO `{DB}`.`a`
        SELECT i.name FROM `{DB}`.`tabSales Invoice` i, `{DB}`.`tabCustomer` c
        WHERE i.customer = c.name
        """
    )

    (lineage,) = result.lineages
    assert _keys(lineage) == {f"{DB}.tabSales Invoice.customer = {DB}.tabCustomer.name"}


def test_unqualified_column_is_not_guessed():
    """列没有表限定就不认——两张表都可能有同名列，猜错的键会被当 FK 证据用。"""
    result = extract(
        f"""
        INSERT INTO `{DB}`.`a`
        SELECT i.name FROM `{DB}`.`tabSales Invoice` i
        JOIN `{DB}`.`tabCustomer` c ON customer = name
        """
    )

    (lineage,) = result.lineages
    assert lineage.join_keys == ()
    assert set(lineage.sources) == {f"{DB}.tabSales Invoice", f"{DB}.tabCustomer"}


def test_self_reference_is_not_an_edge():
    result = extract(
        f"INSERT INTO `{DB}`.`a` SELECT x.id FROM `{DB}`.`a` x WHERE x.flag = 1"
    )

    assert result.lineages == []


# --------------------------------------------------------------------------- 失败形态


def test_stored_procedure_reports_a_readable_reason():
    result = extract(
        """
        DELIMITER $$
        CREATE PROCEDURE rebuild() BEGIN SELECT 1; END $$
        DELIMITER ;
        """
    )

    assert result.error == STORED_PROCEDURE_HINT
    assert result.lineages == []


def test_empty_file_reports_a_readable_reason():
    assert extract("   \n  \n").error == EMPTY_FILE_HINT


def test_broken_sql_reports_a_readable_reason():
    result = extract("INSERT INTO ((((")

    assert result.error is not None
    assert result.error.startswith("解析失败")


def test_dialect_is_honoured():
    """Postgres 的 ILIKE 在 mysql 方言下解析不出来，指定方言就能过。"""
    sql = 'CREATE TABLE public.a AS SELECT s.id FROM public.src s WHERE s.name ILIKE \'%x%\''

    result = extract(sql, dialect="postgres")

    assert result.error is None
    (lineage,) = result.lineages
    assert lineage.target == "public.a"
