"""P1.2 语义导航器：JOIN 路径、ON 条件、基数链、扇出判定。

核心契约：
1. 语义层能**事前给出**关联方式（而非只在事后否决臆造的 JOIN）；
2. 导航器给的 ON 必须能过 SQL 语义证明器——两者共用一份投影与多重性规则，
   出现「导航器说能连、证明器说不能连」即为架构自相矛盾，本文件专门锁这一点。
"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.ontology_projection import build_projection, other_is_many
from app.services.semantic_navigator import find_join_path
from app.services.sql_soundness import SqlCertificate, SqlRejection, prove_sql_sound


def _seed() -> str:
    """order —N:1→ customer —N:1→ region，另有 order —N:N→ tag（桥表）。

    order 与 region 之间**无直接关系**，必须经 customer 两跳——这正是导航器的用武之地。
    """
    pub = EntityStatus.PUBLISHED.value
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:nav-{uniq}", name=f"导航域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()

        order = ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                           table_role="business_object", status=pub)
        customer = ObjectType(ontology_id=onto.id, name="customer", display_name="客户",
                              table_role="business_object", status=pub)
        region = ObjectType(ontology_id=onto.id, name="region", display_name="区域",
                            table_role="business_object", status=pub)
        tag = ObjectType(ontology_id=onto.id, name="tag", display_name="标签",
                         table_role="business_object", status=pub)
        db.add_all([order, customer, region, tag])
        db.flush()
        db.add_all([
            Property(object_type_id=order.id, name="amount", display_name="金额",
                     semantic_type="measure", data_type="decimal", status=pub),
            Property(object_type_id=order.id, name="customer_id", display_name="客户ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=customer.id, name="id", display_name="ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=customer.id, name="region_id", display_name="区域ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=region.id, name="id", display_name="ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=region.id, name="region_name", display_name="区域名",
                     semantic_type="categorical", data_type="varchar", status=pub),
            Property(object_type_id=tag.id, name="id", display_name="ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
        ])
        db.add_all([
            RelationType(
                ontology_id=onto.id, name="order_of_customer", display_name="订单归属客户",
                source_object_type_id=order.id, target_object_type_id=customer.id,
                cardinality="many_to_one", structure_type="foreign_key", status=pub,
            ),
            RelationType(
                ontology_id=onto.id, name="customer_in_region", display_name="客户所属区域",
                source_object_type_id=customer.id, target_object_type_id=region.id,
                cardinality="many_to_one", structure_type="foreign_key", status=pub,
            ),
            RelationType(
                ontology_id=onto.id, name="order_has_tag", display_name="订单打标",
                source_object_type_id=order.id, target_object_type_id=tag.id,
                cardinality="many_to_many", structure_type="bridge_table", status=pub,
            ),
        ])
        db.commit()
        return onto.id


def _proj(onto_id: str):
    with SessionLocal() as db:
        return build_projection(db, onto_id, None)


# ---------------------------------------------------------------- 路径与 ON


def test_direct_path_carries_on_condition(client):
    """一跳：ON 必须由本体外键推出，而不是留给模型猜。"""
    proj = _proj(_seed())
    paths = find_join_path(proj, "order", "customer")

    assert paths, "订单与客户有已声明关系，必须给出路径"
    p = paths[0]
    assert p.hop_count == 1
    assert p.joinable is True
    assert p.hops[0].on == "order.customer_id = customer.id"
    assert p.hops[0].cardinality == "many_to_one"
    assert p.sql_hint() == "order JOIN customer ON order.customer_id = customer.id"


def test_multi_hop_path_found(client):
    """两跳：订单与区域无直接关系，导航器要能给出经客户的通路。"""
    proj = _proj(_seed())
    assert not proj.relation_between("order", "region"), "前提：两者无直接关系"

    paths = find_join_path(proj, "order", "region")
    assert paths, "应能经「客户」两跳关联"
    p = paths[0]
    assert p.objects == ["order", "customer", "region"]
    assert p.hop_count == 2
    assert [h.on for h in p.hops] == [
        "order.customer_id = customer.id",
        "customer.region_id = region.id",
    ]


def test_on_direction_flips_when_traversed_backwards(client):
    """反向遍历时 ON 的两侧要摆正，不能照搬 src/tgt 存储方向。"""
    proj = _proj(_seed())
    paths = find_join_path(proj, "customer", "order")
    assert paths
    assert paths[0].hops[0].on == "customer.id = order.customer_id"


def test_unrelated_objects_yield_empty(client):
    """无通路时返回空列表——这不是错误，是「本体中确实无从关联」这一事实。"""
    proj = _proj(_seed())
    # tag 只连 order；region 只连 customer；tag↔region 需经 order-customer 三跳
    assert find_join_path(proj, "tag", "region", max_hops=2) == []
    assert find_join_path(proj, "tag", "region", max_hops=3), "放宽跳数后应能找到"


# ---------------------------------------------------------------- 扇出


def test_fanout_free_path_for_measure_at_start(client):
    """订单金额 JOIN 客户：N:1 不放大订单行 → 安全。"""
    proj = _proj(_seed())
    p = find_join_path(proj, "order", "customer", measure_object="order")[0]
    assert p.fanout_risk is None
    assert p.safe_aggs == []


def test_fanout_flagged_when_measure_on_the_one_side(client):
    """反过来以客户为度量端：1:N 展开会重复计数 → 必须报扇出并给安全聚合。"""
    proj = _proj(_seed())
    p = find_join_path(proj, "customer", "order", measure_object="customer")[0]
    assert p.fanout_risk is not None
    assert "重复计数" in p.fanout_risk
    assert any("DISTINCT" in a for a in p.safe_aggs)


def test_many_to_many_is_flagged_and_not_joinable(client):
    """N:N 经桥表：不给 ON、明确标注扇出——直连两端是错的。"""
    proj = _proj(_seed())
    p = find_join_path(proj, "order", "tag", measure_object="order")[0]
    assert p.fanout_risk is not None and "多对多" in p.fanout_risk
    assert p.joinable is False
    assert p.sql_hint() is None


# ---------------------------------------------------------------- 与证明器一致


def test_navigator_sql_hint_passes_the_prover(client):
    """**架构不变式**：导航器给的 JOIN 必须被证明器接受。

    两者若各自推 JOIN 键，迟早出现「导航器说能连、证明器拒了」的自相矛盾，
    Agent 会在两个守卫之间空转。这条测试就是那道防线。
    """
    proj = _proj(_seed())
    p = find_join_path(proj, "order", "customer", measure_object="order")[0]
    sql = f"SELECT SUM(order.amount) FROM {p.sql_hint()}"

    verdict = prove_sql_sound(sql, proj)
    assert isinstance(verdict, SqlCertificate), getattr(verdict, "message", verdict)


def test_navigator_fanout_agrees_with_prover(client):
    """扇出判定也必须一致：导航器说会扇出的，证明器同样要拒。"""
    proj = _proj(_seed())
    p = find_join_path(proj, "customer", "order", measure_object="customer")[0]
    assert p.fanout_risk is not None

    # 以客户为度量端做 SUM——证明器应以 fanout_risk 拒绝
    sql = (
        "SELECT SUM(customer.id) FROM customer "
        "JOIN order ON customer.id = order.customer_id"
    )
    verdict = prove_sql_sound(sql, proj)
    assert isinstance(verdict, SqlRejection)
    assert verdict.code in ("fanout_risk", "illegal_aggregation")


def test_other_is_many_shared_by_both_sides(client):
    """多重性换算只有一份实现——证明器与导航器都从投影层拿。"""
    proj = _proj(_seed())
    order, customer = proj.object_of("order"), proj.object_of("customer")
    rel = proj.relation_between("order", "customer")[0]

    assert other_is_many(rel, order) is False   # 订单看客户：N:1，客户是「一」端
    assert other_is_many(rel, customer) is True  # 客户看订单：一对多，订单是「多」端


# ---------------------------------------------------------------- 拒绝提示接线


def test_undeclared_join_hint_offers_multi_hop_route(client):
    """P1.4 × P1.2：臆造 order↔region 的 JOIN 被拒时，提示里要带真正的两跳路径。"""
    proj = _proj(_seed())
    sql = (
        "SELECT r.region_name FROM order o "
        "JOIN region r ON o.customer_id = r.id"
    )
    verdict = prove_sql_sound(sql, proj)

    assert isinstance(verdict, SqlRejection) and verdict.code == "undeclared_join"
    paths = verdict.hint.get("join_paths")
    assert paths, f"应给出可执行的多跳路径，实际 hint={verdict.hint}"
    assert paths[0]["objects"] == ["order", "customer", "region"]
    assert paths[0]["sql_hint"]
