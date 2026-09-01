"""测试动词细化 API 端点（S2）。"""

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, RelationType


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _setup_ontology_with_empty_verbs(db):
    """创建一个包含空泛动词关系的测试本体。"""
    import uuid
    unique_suffix = str(uuid.uuid4())[:8]
    domain = DomainContext(datahub_domain_id=f"urn:test:empty_verbs:{unique_suffix}", name="测试域")
    db.add(domain)
    db.flush()

    ontology = Ontology(
        domain_context_id=domain.id,
        status=OntologyStatus.DRAFT.value,
        generated_by="test",
    )
    db.add(ontology)
    db.flush()

    # 创建对象
    order = ObjectType(
        ontology_id=ontology.id,
        name="order",
        display_name="订单",
        source_ref="urn:li:dataset:order",
        table_role="business_object",
    )
    customer = ObjectType(
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        source_ref="urn:li:dataset:customer",
        table_role="business_object",
    )
    db.add_all([order, customer])
    db.flush()

    # 创建空泛动词关系
    rel1 = RelationType(
        ontology_id=ontology.id,
        name="order_to_customer",
        display_name="属于",  # 空泛动词
        source_object_type_id=order.id,
        target_object_type_id=customer.id,
        structure_type="many_to_one",
        source_evidence="外键: customer_id",
    )
    rel2 = RelationType(
        ontology_id=ontology.id,
        name="customer_to_order",
        display_name="引用",  # 空泛动词
        source_object_type_id=customer.id,
        target_object_type_id=order.id,
        structure_type="one_to_many",
        source_evidence="外键: order.customer_id",
    )
    db.add_all([rel1, rel2])
    db.commit()

    return ontology.id, rel1.id, rel2.id


def test_suggest_verb_refinements(client, admin_headers):
    """测试生成动词细化建议端点。"""
    db = SessionLocal()
    try:
        ontology_id, rel1_id, rel2_id = _setup_ontology_with_empty_verbs(db)

        # 调用建议端点
        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/suggest",
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert "suggestions" in data
        assert "total" in data
        assert data["total"] == 2  # 两个空泛动词关系

        # 验证建议结构
        suggestions = data["suggestions"]
        assert len(suggestions) == 2

        for sug in suggestions:
            assert "relation_id" in sug
            assert "current_verb" in sug
            assert "suggested_verb" in sug
            assert "method" in sug
            assert "confidence" in sug
            assert "source_object_name" in sug
            assert "target_object_name" in sug

            # 当前动词应该是空泛的
            assert sug["current_verb"] in {"属于", "引用"}

    finally:
        db.rollback()
        db.close()


def test_apply_verb_refinements(client, admin_headers):
    """测试应用动词细化建议端点。"""
    db = SessionLocal()
    try:
        ontology_id, rel1_id, rel2_id = _setup_ontology_with_empty_verbs(db)

        # 应用动词细化
        apply_request = {
            "items": [
                {"relation_id": rel1_id, "new_verb": "下单于", "operator": "test_user"},
                {"relation_id": rel2_id, "new_verb": "拥有", "operator": "test_user"},
            ],
            "operator": "test_user",
        }

        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/apply",
            json=apply_request,
            headers=admin_headers,
        )
        assert response.status_code == 200

        result = response.json()
        assert result["updated_count"] == 2
        assert result["total_requested"] == 2
        assert len(result["errors"]) == 0

        # 验证关系已更新
        db.expire_all()
        rel1 = db.get(RelationType, rel1_id)
        rel2 = db.get(RelationType, rel2_id)

        # 采纳即已复核：调用方是人在审核台勾选的，采纳本身就是一次判定。
        # 旧行为置 True，等于每跑一次细化就把这批关系重新打成待复核（净增审核债）。
        assert rel1.display_name == "下单于"
        assert rel1.needs_review is False

        assert rel2.display_name == "拥有"
        assert rel2.needs_review is False

    finally:
        db.rollback()
        db.close()


def test_apply_verb_refinements_with_invalid_relation(client, admin_headers):
    """测试应用动词细化时处理无效关系 ID。"""
    db = SessionLocal()
    try:
        ontology_id, rel1_id, _ = _setup_ontology_with_empty_verbs(db)

        # 包含一个有效和一个无效的关系 ID
        apply_request = {
            "items": [
                {"relation_id": rel1_id, "new_verb": "下单于"},
                {"relation_id": "invalid_id", "new_verb": "测试"},
            ],
        }

        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/apply",
            json=apply_request,
            headers=admin_headers,
        )
        assert response.status_code == 200

        result = response.json()
        assert result["updated_count"] == 1  # 只成功一个
        assert result["total_requested"] == 2
        assert len(result["errors"]) == 1
        assert "invalid_id" in result["errors"][0]

    finally:
        db.rollback()
        db.close()


def test_apply_verb_refinements_cross_ontology_rejection(client, admin_headers):
    """测试跨本体应用动词细化会被拒绝。"""
    db = SessionLocal()
    try:
        # 创建第一个本体
        ontology_id_1, rel1_id, _ = _setup_ontology_with_empty_verbs(db)

        # 创建第二个本体
        domain2 = DomainContext(datahub_domain_id="urn:test:other", name="其他域")
        db.add(domain2)
        db.flush()
        ontology2 = Ontology(
            domain_context_id=domain2.id,
            status=OntologyStatus.DRAFT.value,
            generated_by="test",
        )
        db.add(ontology2)
        db.commit()

        # 尝试用本体 2 的 ID 更新本体 1 的关系
        apply_request = {
            "items": [{"relation_id": rel1_id, "new_verb": "测试"}],
        }

        response = client.post(
            f"/api/ontologies/{ontology2.id}/verb-refinement/apply",
            json=apply_request,
            headers=admin_headers,
        )
        assert response.status_code == 200

        result = response.json()
        assert result["updated_count"] == 0
        assert len(result["errors"]) == 1
        assert "不属于本体" in result["errors"][0]

    finally:
        db.rollback()
        db.close()
