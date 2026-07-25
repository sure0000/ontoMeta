"""人工生成（manual generation）服务：DDL 生成与手工业务对象写入草稿本体。"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, Property
from app.services.manual_creation import (
    ManualCreationService,
    ManualPropertyInput,
    generate_create_table_ddl,
)


def test_generate_ddl_maps_semantic_types_per_dialect():
    props = [
        ManualPropertyInput(name="cust_id", semantic_type="identifier", primary_key=True),
        ManualPropertyInput(name="cust_name", semantic_type="attribute"),
        ManualPropertyInput(name="balance", semantic_type="amount"),
    ]
    mysql = generate_create_table_ddl("Customer", props, dialect="mysql")
    assert "CREATE TABLE customer" in mysql
    assert "cust_id BIGINT NOT NULL" in mysql
    assert "balance DECIMAL(18,2)" in mysql
    assert "PRIMARY KEY (cust_id)" in mysql

    pg = generate_create_table_ddl("Customer", props, dialect="postgresql")
    assert "balance NUMERIC(18,2)" in pg

    hive = generate_create_table_ddl("Customer", props, dialect="hive")
    assert "cust_name STRING" in hive


def test_explicit_data_type_overrides_semantic_mapping():
    ddl = generate_create_table_ddl(
        "t", [ManualPropertyInput(name="code", semantic_type="attribute", data_type="CHAR(8)")]
    )
    assert "code CHAR(8)" in ddl


def test_create_object_writes_into_draft_ontology(client):
    # client fixture 触发 init_db，确保建表。
    domain_id = None
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"urn:test:{uuid.uuid4()}",
            name="新业务域",
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id
    finally:
        db.close()

    service = ManualCreationService()
    db = SessionLocal()
    try:
        result = service.create_object(
            db,
            domain_id,
            name="Loyalty Member",
            display_name="会员",
            description="新会员业务对象",
            dialect="mysql",
            data_source="mysql-new-biz",
            properties=[
                ManualPropertyInput(name="member_id", semantic_type="identifier", primary_key=True),
                ManualPropertyInput(name="level", semantic_type="category"),
            ],
        )
    finally:
        db.close()

    assert "CREATE TABLE loyalty_member" in result.ddl
    assert result.table_name == "loyalty_member"

    # 校验写入草稿本体：对象 + 属性，且标记为人工创建。
    db = SessionLocal()
    try:
        obj = db.get(ObjectType, result.object_type_id)
        assert obj is not None
        assert obj.name == "loyalty_member"
        assert obj.display_name == "会员"
        assert obj.user_created is True
        assert obj.origin == "user"
        assert obj.table_role == "business_object"
        onto = db.get(Ontology, result.ontology_id)
        assert onto.domain_context_id == domain_id
        assert onto.status == "draft"
        props = db.query(Property).filter(Property.object_type_id == obj.id).all()
        assert {p.name for p in props} == {"member_id", "level"}
        assert all(p.user_created for p in props)
    finally:
        db.close()


def test_create_object_reuses_existing_draft(client):
    db = SessionLocal()
    try:
        domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="域2")
        db.add(domain)
        db.commit()
        domain_id = domain.id
    finally:
        db.close()

    service = ManualCreationService()
    db = SessionLocal()
    try:
        r1 = service.create_object(
            db, domain_id, name="a", display_name="A", description=None,
            properties=[ManualPropertyInput(name="id", semantic_type="identifier")],
        )
        r2 = service.create_object(
            db, domain_id, name="b", display_name="B", description=None,
            properties=[ManualPropertyInput(name="id", semantic_type="identifier")],
        )
    finally:
        db.close()
    # 两次手工创建复用同一份草稿本体。
    assert r1.ontology_id == r2.ontology_id
