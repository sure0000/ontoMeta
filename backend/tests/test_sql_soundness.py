"""F3：SQL 语义证明器（sql_soundness）单元测试。

用手搓的 OntologyProjection（不落库），逐条验证「表存在/列归属/JOIN 有据/
不扇出/聚合合法」的拒绝与放行。核心指标：语义错误的 SQL 必被拒，合法 SQL 出证书。
"""

from __future__ import annotations

import pytest

from app.ontology_types import Cardinality, SemanticType
from app.services.ontology_projection import ObjView, OntologyProjection, PropView, RelView
from app.services.sql_soundness import SqlCertificate, SqlRejection, prove_sql_sound


def _proj() -> OntologyProjection:
    """构造一个小本体：
    订单(order): id[identifier], amount[measure], status[categorical], city[categorical], order_date[temporal]
    客户(customer): id[identifier], name[textual]
    标签(tag): id[identifier], label[textual]
    关系：order —many_to_one→ customer；order —many_to_many→ tag
    """
    order = ObjView(
        id="o1", name="order", display_name="订单",
        props={
            "id": PropView("id", "order", SemanticType.IDENTIFIER, "bigint"),
            "amount": PropView("amount", "order", SemanticType.MEASURE, "decimal"),
            "status": PropView("status", "order", SemanticType.CATEGORICAL, "varchar"),
            "city": PropView("city", "order", SemanticType.CATEGORICAL, "varchar"),
            "order_date": PropView("order_date", "order", SemanticType.TEMPORAL, "date"),
            "customer_id": PropView("customer_id", "order", SemanticType.IDENTIFIER, "bigint"),
        },
    )
    customer = ObjView(
        id="c1", name="customer", display_name="客户",
        props={
            "id": PropView("id", "customer", SemanticType.IDENTIFIER, "bigint"),
            "name": PropView("name", "customer", SemanticType.TEXTUAL, "varchar"),
        },
    )
    tag = ObjView(
        id="t1", name="tag", display_name="标签",
        props={
            "id": PropView("id", "tag", SemanticType.IDENTIFIER, "bigint"),
            "label": PropView("label", "tag", SemanticType.TEXTUAL, "varchar"),
        },
    )
    rel_cust = RelView(
        "r1", "order_belongs_to_customer", "订单归属客户", "order", "customer",
        Cardinality.MANY_TO_ONE, "foreign_key",
        src_key="customer_id", tgt_key="id",
    )
    rel_tag = RelView(
        "r2", "order_has_tag", "订单打标", "order", "tag",
        Cardinality.MANY_TO_MANY, "bridge_table",
    )
    return OntologyProjection(
        objects={"order": order, "customer": customer, "tag": tag},
        relations_by_pair={
            frozenset({"order", "customer"}): [rel_cust],
            frozenset({"order", "tag"}): [rel_tag],
        },
        mapping_tables={}, mapping_columns={},
    )


def test_unknown_table_rejected():
    r = prove_sql_sound("SELECT * FROM ghost_table", _proj())
    assert isinstance(r, SqlRejection) and r.code == "unknown_table"


def test_unknown_column_rejected():
    r = prove_sql_sound("SELECT fake_col FROM order", _proj())
    assert isinstance(r, SqlRejection) and r.code == "unknown_column"


def test_undeclared_join_rejected():
    # customer 与 tag 之间无声明关系
    r = prove_sql_sound(
        "SELECT c.name FROM customer c JOIN tag t ON c.id = t.id", _proj()
    )
    assert isinstance(r, SqlRejection) and r.code == "undeclared_join"


def test_many_to_many_fanout_rejected():
    # SUM(order.amount) 沿 order↔tag(N:M) JOIN → 扇出翻倍
    r = prove_sql_sound(
        "SELECT SUM(o.amount) FROM order o JOIN tag t ON o.id = t.id", _proj()
    )
    assert isinstance(r, SqlRejection) and r.code == "fanout_risk"


def test_many_to_one_join_safe():
    # SUM(order.amount) JOIN customer(多对一：一个 customer 一个订单不放大订单) → 安全
    r = prove_sql_sound(
        "SELECT SUM(o.amount) FROM order o JOIN customer c ON o.customer_id = c.id",
        _proj(),
    )
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)


def test_illegal_aggregation_rejected():
    # 对 categorical 字段 SUM
    r = prove_sql_sound("SELECT SUM(status) FROM order", _proj())
    assert isinstance(r, SqlRejection) and r.code == "illegal_aggregation"


def test_illegal_group_by_rejected():
    # 按 measure 分组
    r = prove_sql_sound(
        "SELECT amount, COUNT(*) FROM order GROUP BY amount", _proj()
    )
    assert isinstance(r, SqlRejection) and r.code == "illegal_group_by"


def test_legal_query_certified():
    r = prove_sql_sound(
        "SELECT city, SUM(amount) AS total FROM order GROUP BY city", _proj()
    )
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)
    assert "order" in r.tables
    assert any("sum(order.amount)" == a for a in r.aggregations)


def test_unparseable_rejected():
    r = prove_sql_sound("SELECT FROM WHERE", _proj())
    assert isinstance(r, SqlRejection)


def test_count_star_no_fanout_check():
    # COUNT(*) 不属于扇出敏感聚合，跨 N:M JOIN 也放行（COUNT 语义可接受）
    r = prove_sql_sound(
        "SELECT COUNT(*) FROM order o JOIN tag t ON o.id = t.id", _proj()
    )
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)


# ---------------------------------------------------------------- 输出别名


@pytest.mark.parametrize(
    "sql",
    [
        # sqlglot 的 qualify 会把 ORDER BY 里的聚合归一成「引用输出别名」，
        # 于是别名以裸列形态出现——曾被误判成臆造字段而拒掉。
        "SELECT status, COUNT(*) AS cnt FROM order GROUP BY status ORDER BY COUNT(*) DESC",
        "SELECT status, COUNT(*) AS cnt FROM order GROUP BY status ORDER BY cnt DESC",
        "SELECT city, SUM(amount) AS total FROM order GROUP BY city ORDER BY total DESC",
    ],
)
def test_order_by_output_alias_not_a_fabricated_column(sql):
    """「按金额降序取 TopN」是最常见的查询形态，不得被误拒。"""
    r = prove_sql_sound(sql, _proj())
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)


@pytest.mark.parametrize(
    "sql",
    [
        # 别名只是「引用」被豁免，其**定义式**照样逐列证明
        "SELECT ghost AS x FROM order ORDER BY x",
        "SELECT status AS s FROM order WHERE ghost = 1",
        # 裸列的输出名与列名相同——不能靠「自己给自己当别名」蒙混过关
        "SELECT fake_col FROM order",
        "SELECT fake_col AS fake_col FROM order ORDER BY fake_col",
    ],
)
def test_alias_does_not_launder_fabricated_columns(sql):
    r = prove_sql_sound(sql, _proj())
    assert isinstance(r, SqlRejection) and r.code == "unknown_column"
