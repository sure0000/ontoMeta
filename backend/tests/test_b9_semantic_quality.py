"""B9：版本 diff、一致性校验、Chat BI grounding。"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    DomainContext,
    DraftGenerationTask,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)
from app.services.draft_consistency import validate_ontology
from app.services.publish import PublishService
from tests.conftest import ADMIN_HEADERS


def _seed_domain_with_ontology(*, name: str = "B9域") -> tuple[str, str]:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}",
            name=name,
            description="b9 test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.DRAFT.value,
            version=0,
        )
        db.add(ontology)
        db.flush()
        obj_a = ObjectType(
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            status="suggested",
        )
        obj_b = ObjectType(
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            status="suggested",
        )
        db.add_all([obj_a, obj_b])
        db.flush()
        db.add(
            RelationType(
                ontology_id=ontology.id,
                name="order_belongs_to_customer",
                display_name="订单归属客户",
                source_object_type_id=obj_a.id,
                target_object_type_id=obj_b.id,
                status="suggested",
            )
        )
        db.commit()
        return domain.id, ontology.id


def test_publish_version_diff_readable(client, admin_headers):
    domain_id, ontology_id = _seed_domain_with_ontology(name="diff-domain")

    # 首次发布：全部为新增
    conf = client.post(
        "/api/confirmations",
        headers=admin_headers,
        json={
            "ontology_id": ontology_id,
            "target_type": "ontology",
            "action_type": "publish",
            "reason": "b9",
        },
    )
    assert conf.status_code == 200, conf.text
    ok = client.post(
        f"/api/confirmations/{conf.json()['id']}/confirm",
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text

    versions = client.get(
        f"/api/ontologies/{ontology_id}/versions", headers=admin_headers
    )
    assert versions.status_code == 200
    items = versions.json()
    assert len(items) >= 1
    assert items[0]["version"] == 1
    assert items[0]["has_diff"] is True
    assert "新增" in (items[0]["diff_summary"] or "")

    diff = client.get(
        f"/api/ontologies/{ontology_id}/versions/1/diff",
        headers=admin_headers,
    )
    assert diff.status_code == 200
    body = diff.json()
    assert body["version"] == 1
    assert len(body["object_types"]["added"]) == 2
    assert len(body["relation_types"]["added"]) == 1

    snap = client.get(
        f"/api/ontologies/{ontology_id}/versions/1/snapshot",
        headers=admin_headers,
    )
    assert snap.status_code == 200
    assert len(snap.json()["object_types"]) == 2

    # 第二次发布前改一个对象，应出现 modified
    with SessionLocal() as db:
        ontology = db.get(Ontology, ontology_id)
        ontology.status = OntologyStatus.DRAFT.value
        obj = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id, ObjectType.name == "order")
            .one()
        )
        obj.display_name = "销售订单"
        db.commit()

    conf2 = client.post(
        "/api/confirmations",
        headers=admin_headers,
        json={
            "ontology_id": ontology_id,
            "target_type": "ontology",
            "action_type": "publish",
            "reason": "b9-v2",
        },
    )
    assert conf2.status_code == 200
    ok2 = client.post(
        f"/api/confirmations/{conf2.json()['id']}/confirm",
        headers=admin_headers,
    )
    assert ok2.status_code == 200, ok2.text

    diff2 = client.get(
        f"/api/ontologies/{ontology_id}/versions/2/diff",
        headers=admin_headers,
    )
    assert diff2.status_code == 200
    body2 = diff2.json()
    assert body2["previous_version"] == 1
    assert any(m["name"] == "order" for m in body2["object_types"]["modified"])


def test_inconsistent_draft_validation_fails(client, admin_headers):
    domain_id, ontology_id = _seed_domain_with_ontology(name="bad-draft")

    # 再造一个域外对象，把关系源端点改成跨本体（SQLite 默认不强制 FK）
    with SessionLocal() as db:
        other = Ontology(
            domain_context_id=domain_id,
            status=OntologyStatus.DRAFT.value,
        )
        db.add(other)
        db.flush()
        foreign = ObjectType(
            ontology_id=other.id,
            name="foreign_obj",
            display_name="域外对象",
            status="suggested",
        )
        db.add(foreign)
        db.flush()
        rel = (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .first()
        )
        rel.source_object_type_id = foreign.id
        db.commit()

    with SessionLocal() as db:
        issues = validate_ontology(db, ontology_id)
    assert any(i.code == "relation_source_missing" for i in issues)

    resp = client.post(
        f"/api/ontologies/{ontology_id}/validate",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert any(i["code"] == "relation_source_missing" for i in body["issues"])

    conf = client.post(
        "/api/confirmations",
        headers=admin_headers,
        json={
            "ontology_id": ontology_id,
            "target_type": "ontology",
            "action_type": "publish",
            "reason": "should-fail",
        },
    )
    assert conf.status_code == 200
    bad = client.post(
        f"/api/confirmations/{conf.json()['id']}/confirm",
        headers=admin_headers,
    )
    assert bad.status_code == 400
    detail = bad.json()["detail"]
    assert isinstance(detail, dict)
    assert "一致性校验失败" in detail["message"]
    assert detail["issues"]


def test_chat_bi_no_hit_refuses_fiction(client, admin_headers):
    domain_id, ontology_id = _seed_domain_with_ontology(name="chatbi-ground")
    with SessionLocal() as db:
        PublishService().publish(db, ontology_id)

    # 无命中问题：不应返回订单/客户等对象名作为“命中解读”
    ask = client.post(
        "/api/chat-bi/ask",
        headers=admin_headers,
        json={
            "domain_id": domain_id,
            "question": "火星上的独角兽库存怎么算",
        },
    )
    assert ask.status_code == 200, ask.text
    payload = ask.json()
    assert payload.get("grounding_refused") is True
    assert payload["referenced_objects"] == []
    assert payload["referenced_logics"] == []
    assert payload["suggested_sql"] is None
    answer = payload["answer"]
    assert "未检索到" in answer or "无法基于" in answer
    # 不应把本体对象名当作已匹配主对象来编造口径
    assert "基于「订单」本体解读" not in answer
    assert "基于「客户」本体解读" not in answer


def test_chat_bi_session_domain_binding(client, admin_headers):
    domain_a, ontology_a = _seed_domain_with_ontology(name="bind-a")
    domain_b, ontology_b = _seed_domain_with_ontology(name="bind-b")
    with SessionLocal() as db:
        PublishService().publish(db, ontology_a)
        PublishService().publish(db, ontology_b)

    created = client.post(
        "/api/chat-bi/conversations",
        headers=admin_headers,
        json={"domain_id": domain_a, "title": "会话A"},
    )
    assert created.status_code == 200
    conv_id = created.json()["id"]

    mismatch = client.post(
        "/api/chat-bi/ask",
        headers=admin_headers,
        json={
            "domain_id": domain_b,
            "conversation_id": conv_id,
            "question": "订单数量",
        },
    )
    assert mismatch.status_code == 400
    assert "数据域" in mismatch.json()["detail"]


def test_failed_task_retry_and_duplicate_report(client, admin_headers):
    from app.services.draft_task_service import DraftTaskService

    domain_id, _ontology_id = _seed_domain_with_ontology(name="retry-domain")

    with SessionLocal() as db:
        task = DraftGenerationTask(
            domain_context_id=domain_id,
            status="failed",
            progress=40,
            message="模拟失败：LLM timeout",
            error_summary="模拟失败：LLM timeout",
        )
        db.add(task)
        # 制造两个 draft 以便去重报告
        for _ in range(2):
            db.add(
                Ontology(
                    domain_context_id=domain_id,
                    status=OntologyStatus.DRAFT.value,
                )
            )
        db.commit()
        task_id = task.id

    dups = client.get(
        f"/api/domains/{domain_id}/draft-duplicates",
        headers=admin_headers,
    )
    assert dups.status_code == 200
    assert dups.json()["draft_count"] >= 2

    # 服务层重试（不经 HTTP，避免后台 asyncio 污染共享测试库）
    with SessionLocal() as db:
        progress = DraftTaskService().retry_draft_generation(db, domain_id, task_id)
        assert progress.status == "queued"
        assert progress.task_id != task_id
        # 立即取消，避免 ACTIVE 遗留
        new_task = db.get(DraftGenerationTask, progress.task_id)
        assert new_task is not None
        new_task.status = "cancelled"
        new_task.message = "test cleanup"
        db.commit()

    # 路由可达性冒烟
    with SessionLocal() as db:
        another = DraftGenerationTask(
            domain_context_id=domain_id,
            status="failed",
            progress=10,
            message="again",
            error_summary="again",
        )
        db.add(another)
        db.commit()
        another_id = another.id

    retry = client.post(
        f"/api/domains/{domain_id}/tasks/{another_id}/retry",
        headers=admin_headers,
    )
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["status"] == "queued"
    with SessionLocal() as db:
        t = db.get(DraftGenerationTask, body["task_id"])
        if t and t.status in {"queued", "running"}:
            t.status = "cancelled"
            t.message = "test cleanup"
            db.commit()
