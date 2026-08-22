"""B5：草稿任务状态机、重启修复、队列位次。"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DomainContext, DraftGenerationTask, ObjectType, Ontology, OntologyStatus
from app.services.draft_generation_queue import get_queue_position
from app.services.draft_task_service import (
    DraftGenerationAlreadyRunning,
    DraftTaskService,
    recover_stale_draft_tasks,
)


def test_recover_stale_draft_tasks(client):
    # client fixture 触发 init_db；在此基础上写入僵尸任务再修复。
    # 陈旧（updated_at 早于宽限窗口）→ 回收；最近仍在推进 → 保护不回收。
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.config import settings

    db = SessionLocal()
    try:
        db_now = db.execute(select(func.now())).scalar()
        grace = settings.draft_task_stale_grace_seconds
        old = db_now - timedelta(seconds=grace + 60)
        fresh = db_now  # 窗口内

        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-recover",
            name="B5 Recover",
        )
        db.add(domain)
        db.flush()

        queued = DraftGenerationTask(
            domain_context_id=domain.id, status="queued", progress=0, message="排队",
            updated_at=old,
        )
        running = DraftGenerationTask(
            domain_context_id=domain.id, status="running", progress=50, message="执行中",
            updated_at=old,
        )
        recent = DraftGenerationTask(
            domain_context_id=domain.id, status="running", progress=70, message="推进中",
            updated_at=fresh,
        )
        done = DraftGenerationTask(
            domain_context_id=domain.id, status="succeeded", progress=100, message="完成",
            updated_at=old,
        )
        db.add_all([queued, running, recent, done])
        db.commit()
        qid, rid, freshid, did = queued.id, running.id, recent.id, done.id
    finally:
        db.close()

    n = recover_stale_draft_tasks()
    assert n == 2  # 仅两条陈旧的 queued/running 被回收

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, qid).status == "failed"
        assert db.get(DraftGenerationTask, rid).status == "failed"
        assert "重启" in (db.get(DraftGenerationTask, qid).message or "")
        # 窗口内最近推进的任务受保护，不被误杀（可能属于另一存活进程）
        assert db.get(DraftGenerationTask, freshid).status == "running"
        assert db.get(DraftGenerationTask, did).status == "succeeded"
    finally:
        db.close()


def test_start_draft_generation_queued(client, llm_ready):
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-start",
            name="B5 Start",
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        progress = svc.start_draft_generation(db, domain_id)
        assert progress.status == "queued"
        assert progress.task_id

        pos, total = get_queue_position(progress.task_id)
        assert total >= 1
        assert pos >= 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 「仅生成业务对象」/「仅生成业务关系」独立按钮：范围化并发控制
# ---------------------------------------------------------------------------
def test_object_and_relation_generation_can_run_in_parallel(client, llm_ready):
    """对象/关系两个范围互不阻塞：可同时排队，支持并行执行。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-parallel", name="B5 Parallel"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value
        )
        db.add(ontology)
        db.flush()
        db.add(
            ObjectType(
                ontology_id=ontology.id,
                name="payment",
                display_name="支付",
                source_ref="urn:li:dataset:payment",
            )
        )
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        object_progress = svc.start_object_generation(db, domain_id)
        relation_progress = svc.start_relation_generation(db, domain_id)

        assert object_progress.scope == "objects"
        assert relation_progress.scope == "relations"
        assert object_progress.status == "queued"
        assert relation_progress.status == "queued"
    finally:
        db.close()


def test_same_scope_generation_conflicts(client, llm_ready):
    """同一范围的两个生成任务互斥：第二次触发应报「已有任务进行中」。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-same-scope", name="B5 Same Scope"
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        svc.start_object_generation(db, domain_id)
        with pytest.raises(DraftGenerationAlreadyRunning):
            svc.start_object_generation(db, domain_id)
    finally:
        db.close()


def test_full_generation_conflicts_with_scoped_generation(client, llm_ready):
    """``full`` 会整体重建草稿本体，与任何范围的进行中任务都冲突。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-full-conflict", name="B5 Full Conflict"
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        svc.start_object_generation(db, domain_id)
        with pytest.raises(DraftGenerationAlreadyRunning):
            svc.start_draft_generation(db, domain_id)
    finally:
        db.close()


def test_scoped_generation_conflicts_with_running_full(client, llm_ready):
    """反向同样成立：``full`` 进行中时，对象/关系范围的生成也应被阻塞。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-full-first", name="B5 Full First"
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        svc.start_draft_generation(db, domain_id)
        with pytest.raises(DraftGenerationAlreadyRunning):
            svc.start_object_generation(db, domain_id)
    finally:
        db.close()


def test_relation_generation_requires_existing_objects(client):
    """尚无草稿本体/业务对象时，「仅生成业务关系」应拒绝并提示先生成对象。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-no-objects", name="B5 No Objects"
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id

        svc = DraftTaskService()
        with pytest.raises(ValueError, match="业务对象"):
            svc.start_relation_generation(db, domain_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 无 LLM 时的「提示而非降级」：起草入口当场 400，不建任务、不出技术名草稿
# ---------------------------------------------------------------------------
def test_generate_without_llm_returns_400(client, admin_headers):
    """未配置 LLM 时三个生成入口都返回 400 + 中文指引，并且不留下任务记录。"""
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-no-llm", name="B5 No LLM"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value
        )
        db.add(ontology)
        db.flush()
        db.add(
            ObjectType(
                ontology_id=ontology.id,
                name="payment",
                display_name="支付",
                source_ref="urn:li:dataset:payment-no-llm",
            )
        )
        db.commit()
        domain_id = domain.id
    finally:
        db.close()

    for scope in ("generate-draft", "generate-objects", "generate-relations"):
        resp = client.post(f"/api/domains/{domain_id}/{scope}", headers=admin_headers)
        assert resp.status_code == 400, (scope, resp.text)
        assert "未配置可用的 LLM 服务" in resp.json()["detail"]

    db = SessionLocal()
    try:
        assert (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.domain_context_id == domain_id)
            .count()
            == 0
        )
    finally:
        db.close()
