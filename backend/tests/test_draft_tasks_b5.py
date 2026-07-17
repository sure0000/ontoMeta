"""B5：草稿任务状态机、重启修复、队列位次。"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, DraftGenerationTask
from app.services.draft_generation_queue import get_queue_position
from app.services.draft_task_service import (
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
