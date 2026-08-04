"""F3 集成：从真实已发布本体（ORM）构建投影 → SQL 语义证明端到端。

单元测试 test_sql_soundness 用手搓投影；这里覆盖 build_projection 的落库路径，
确保「已发布对象/属性/关系 → 投影 → 证明器」整条链在真实数据上成立。
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
from app.services.ontology_projection import build_projection
from app.services.sql_soundness import SqlCertificate, SqlRejection, prove_sql_sound


def _seed_published() -> str:
    """建 order/customer 两个已发布对象 + 属性 + many_to_one 关系，返回 ontology_id。"""
    pub = EntityStatus.PUBLISHED.value
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:sql-int-{uniq}", name=f"SQL集成域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(onto)
        db.flush()
        order = ObjectType(
            ontology_id=onto.id, name="order", display_name="订单",
            table_role="business_object", status=pub,
        )
        customer = ObjectType(
            ontology_id=onto.id, name="customer", display_name="客户",
            table_role="business_object", status=pub,
        )
        db.add_all([order, customer])
        db.flush()
        db.add_all([
            Property(object_type_id=order.id, name="amount", display_name="金额",
                     semantic_type="measure", data_type="decimal", status=pub),
            Property(object_type_id=order.id, name="status", display_name="状态",
                     semantic_type="categorical", data_type="varchar", status=pub),
            Property(object_type_id=order.id, name="customer_id", display_name="客户ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=customer.id, name="id", display_name="ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
        ])
        db.add(RelationType(
            ontology_id=onto.id, name="order_of_customer", display_name="归属",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            cardinality="many_to_one", structure_type="foreign_key", status=pub,
        ))
        db.commit()
        return onto.id


def test_projection_built_from_orm(client):
    onto_id = _seed_published()
    with SessionLocal() as db:
        proj = build_projection(db, onto_id, mapping=None)
    assert "order" in proj.objects and "customer" in proj.objects
    assert proj.objects["order"].resolve_property("amount") is not None
    assert proj.relation_between("order", "customer")


def test_end_to_end_reject_unknown_column(client):
    onto_id = _seed_published()
    with SessionLocal() as db:
        proj = build_projection(db, onto_id, mapping=None)
    r = prove_sql_sound("SELECT ghost FROM order", proj)
    assert isinstance(r, SqlRejection) and r.code == "unknown_column"


def test_end_to_end_certify_legal(client):
    onto_id = _seed_published()
    with SessionLocal() as db:
        proj = build_projection(db, onto_id, mapping=None)
    r = prove_sql_sound("SELECT status, SUM(amount) FROM order GROUP BY status", proj)
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)


def test_end_to_end_reject_fanout(client):
    onto_id = _seed_published()
    with SessionLocal() as db:
        proj = build_projection(db, onto_id, mapping=None)
    # many_to_one：SUM(order.amount) JOIN customer 不放大订单 → 应放行
    r = prove_sql_sound(
        "SELECT SUM(o.amount) FROM order o JOIN customer c ON o.customer_id = c.id",
        proj,
    )
    assert isinstance(r, SqlCertificate), getattr(r, "message", r)
