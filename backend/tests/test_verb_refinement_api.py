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


def _setup_ontology_with_specific_verbs(db):
    """建一个动词**不空泛**的本体：用来验证点名批次时不套空动词过滤。"""
    import uuid
    unique_suffix = str(uuid.uuid4())[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:test:specific_verbs:{unique_suffix}", name="测试域"
    )
    db.add(domain)
    db.flush()

    ontology = Ontology(
        domain_context_id=domain.id,
        status=OntologyStatus.DRAFT.value,
        generated_by="test",
    )
    db.add(ontology)
    db.flush()

    order = ObjectType(
        ontology_id=ontology.id,
        name="order",
        display_name="订单",
        source_ref=f"urn:li:dataset:order:{unique_suffix}",
        table_role="business_object",
    )
    supplier = ObjectType(
        ontology_id=ontology.id,
        name="supplier",
        display_name="供应商",
        source_ref=f"urn:li:dataset:supplier:{unique_suffix}",
        table_role="business_object",
    )
    customer = ObjectType(
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        source_ref=f"urn:li:dataset:customer:{unique_suffix}",
        table_role="business_object",
    )
    db.add_all([order, supplier, customer])
    db.flush()

    # 规则给「下给」，与现动词「拥有」不同 → 应出建议。
    changeable = RelationType(
        ontology_id=ontology.id,
        name="order_to_supplier",
        display_name="拥有",
        source_object_type_id=order.id,
        target_object_type_id=supplier.id,
        structure_type="many_to_one",
        source_evidence="外键: supplier_id",
    )
    # 规则给「服务」，与现动词一致 → 改不动，不该出现在建议里。
    unchanged = RelationType(
        ontology_id=ontology.id,
        name="order_to_customer",
        display_name="服务",
        source_object_type_id=order.id,
        target_object_type_id=customer.id,
        structure_type="many_to_one",
        source_evidence="外键: customer_id",
    )
    db.add_all([changeable, unchanged])
    db.commit()

    return ontology.id, changeable.id, unchanged.id


def test_suggest_verb_refinements_scoped_to_batch(client, admin_headers):
    """点名 relation_ids 时只对这一批出建议——细化范围要等于审核台屏幕上的那一组。"""
    db = SessionLocal()
    try:
        ontology_id, rel1_id, rel2_id = _setup_ontology_with_empty_verbs(db)

        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/suggest",
            json={"relation_ids": [rel1_id]},
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert data["candidate_count"] == 1
        assert [s["relation_id"] for s in data["suggestions"]] == [rel1_id]
        # 同一本体里的另一条空动词关系没被捎带上。
        assert rel2_id not in {s["relation_id"] for s in data["suggestions"]}

    finally:
        db.rollback()
        db.close()


def test_suggest_verb_refinements_batch_skips_empty_verb_filter(client, admin_headers):
    """批次模式不套空动词过滤（人点了谁就细化谁），但改不动的不返回。"""
    db = SessionLocal()
    try:
        ontology_id, changeable_id, unchanged_id = _setup_ontology_with_specific_verbs(db)

        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/suggest",
            json={"relation_ids": [changeable_id, unchanged_id]},
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert data["candidate_count"] == 2
        # 「拥有」不是空泛动词，全本体扫描不会碰它；点名了就该出建议。
        assert [s["relation_id"] for s in data["suggestions"]] == [changeable_id]
        assert data["suggestions"][0]["suggested_verb"] == "下给"
        # 建议词与现动词相同的那条是噪声，不返回。
        assert data["total"] == 1

        # 不给 relation_ids 时仍是老行为：只捞空泛动词，这两条都不在其中。
        whole = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/suggest",
            headers=admin_headers,
        )
        assert whole.status_code == 200
        assert whole.json()["suggestions"] == []

    finally:
        db.rollback()
        db.close()


def test_suggest_verb_refinements_ignores_foreign_relation_ids(client, admin_headers):
    """点名了别的本体的关系 ID 也不会被改：范围先按本体收窄。"""
    db = SessionLocal()
    try:
        ontology_id, rel1_id, _ = _setup_ontology_with_empty_verbs(db)
        other_ontology_id, other_rel_id, _ = _setup_ontology_with_empty_verbs(db)

        response = client.post(
            f"/api/ontologies/{ontology_id}/verb-refinement/suggest",
            json={"relation_ids": [rel1_id, other_rel_id, "not-a-real-id"]},
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert data["candidate_count"] == 1
        assert [s["relation_id"] for s in data["suggestions"]] == [rel1_id]

    finally:
        db.rollback()
        db.close()


def test_suggest_reads_generated_evidence_format(client, admin_headers):
    """规则要认本项目生成器实际写的证据句式：「A 通过引用字段 company 关联 B」。

    只认「外键: x」时，规则这一路在真实数据上等于没开——真实 erpnext 本体 1279 条关系
    里只有 1 条能命中，其余全压给 LLM，模型一挂就一条建议都出不来。
    """
    db = SessionLocal()
    try:
        import uuid
        suffix = str(uuid.uuid4())[:8]
        domain = DomainContext(datahub_domain_id=f"urn:test:evidence:{suffix}", name="测试域")
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.DRAFT.value,
            generated_by="test",
        )
        db.add(ontology)
        db.flush()

        voucher = ObjectType(
            ontology_id=ontology.id,
            name="tabjournal_entry",
            display_name="会计凭证",
            source_ref=f"urn:li:dataset:je:{suffix}",
            table_role="business_object",
        )
        company = ObjectType(
            ontology_id=ontology.id,
            name="tabcompany",
            display_name="公司",
            source_ref=f"urn:li:dataset:company:{suffix}",
            table_role="business_object",
        )
        db.add_all([voucher, company])
        db.flush()

        rel = RelationType(
            ontology_id=ontology.id,
            # 名字末段是 entity，靠名字猜列名只会得到 "entity"——必须从证据里读。
            name="tabjournal_entry_entity_to_tabcompany_entity",
            display_name="属于",
            source_object_type_id=voucher.id,
            target_object_type_id=company.id,
            structure_type="many_to_one",
            source_evidence="tabjournal_entry 通过引用字段 company 关联 tabCompany（推断，来源 frappe 源画像）",
        )
        db.add(rel)
        db.commit()

        response = client.post(
            f"/api/ontologies/{ontology.id}/verb-refinement/suggest",
            json={"relation_ids": [rel.id]},
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert [s["suggested_verb"] for s in data["suggestions"]] == ["隶属于"]
        assert data["suggestions"][0]["method"] == "rule"

    finally:
        db.rollback()
        db.close()


def test_suggest_drops_still_empty_verbs(client, admin_headers):
    """建议词本身还是空泛词就不算细化：「引用」→「属于」不该占一行。"""
    db = SessionLocal()
    try:
        import uuid
        suffix = str(uuid.uuid4())[:8]
        domain = DomainContext(datahub_domain_id=f"urn:test:still_empty:{suffix}", name="测试域")
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.DRAFT.value,
            generated_by="test",
        )
        db.add(ontology)
        db.flush()

        log = ObjectType(
            ontology_id=ontology.id,
            name="tabactivity_log",
            display_name="活动日志",
            source_ref=f"urn:li:dataset:log:{suffix}",
            table_role="business_object",
        )
        doctype = ObjectType(
            ontology_id=ontology.id,
            name="tabdoctype",
            display_name="文档类型",
            source_ref=f"urn:li:dataset:doctype:{suffix}",
            table_role="business_object",
        )
        db.add_all([log, doctype])
        db.flush()

        # doctype 命中 type 规则 → 「属于」，与现动词「引用」同属空泛词，换了等于没换。
        rel = RelationType(
            ontology_id=ontology.id,
            name="tabactivity_log_entity_to_tabdoctype_entity",
            display_name="引用",
            source_object_type_id=log.id,
            target_object_type_id=doctype.id,
            structure_type="many_to_one",
            source_evidence="tabactivity_log 通过引用字段 doctype 关联 tabDocType（推断）",
        )
        db.add(rel)
        db.commit()

        response = client.post(
            f"/api/ontologies/{ontology.id}/verb-refinement/suggest",
            json={"relation_ids": [rel.id]},
            headers=admin_headers,
        )
        assert response.status_code == 200

        data = response.json()
        assert data["candidate_count"] == 1
        assert data["suggestions"] == []

    finally:
        db.rollback()
        db.close()
