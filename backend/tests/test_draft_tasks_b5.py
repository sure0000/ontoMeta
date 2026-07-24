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
    # client fixture 触发 init_db；在此基础上写入僵尸任务再修复
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:b5-recover",
            name="B5 Recover",
        )
        db.add(domain)
        db.flush()

        queued = DraftGenerationTask(
            domain_context_id=domain.id, status="queued", progress=0, message="排队"
        )
        running = DraftGenerationTask(
            domain_context_id=domain.id, status="running", progress=50, message="执行中"
        )
        done = DraftGenerationTask(
            domain_context_id=domain.id, status="succeeded", progress=100, message="完成"
        )
        db.add_all([queued, running, done])
        db.commit()
        qid, rid, did = queued.id, running.id, done.id
    finally:
        db.close()

    n = recover_stale_draft_tasks()
    assert n >= 2

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, qid).status == "failed"
        assert db.get(DraftGenerationTask, rid).status == "failed"
        assert "重启" in (db.get(DraftGenerationTask, qid).message or "")
        assert db.get(DraftGenerationTask, did).status == "succeeded"
    finally:
        db.close()


def test_start_draft_generation_queued(client):
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
def test_object_and_relation_generation_can_run_in_parallel(client):
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


def test_same_scope_generation_conflicts(client):
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


def test_full_generation_conflicts_with_scoped_generation(client):
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


def test_scoped_generation_conflicts_with_running_full(client):
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
