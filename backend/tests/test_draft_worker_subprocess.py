"""C：分离子进程草稿生成——跨进程准入 + 派发切换。"""

from __future__ import annotations

import asyncio
import subprocess

from app.database import SessionLocal
from app.models import DomainContext, DraftGenerationTask
from app.schemas import DraftProgressOut
from app.services.draft_generation_queue import (
    _try_claim_running_slot,
    await_running_slot,
)


def _mk_domain(db, key: str) -> str:
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:{key}", name=key)
    db.add(domain)
    db.flush()
    return domain.id


def _mk_task(db, domain_id: str, status: str) -> str:
    task = DraftGenerationTask(
        domain_context_id=domain_id, status=status, progress=0, message="排队"
    )
    db.add(task)
    db.flush()
    return task.id


def _reset_active_tasks(db) -> None:
    """把库里遗留的 queued/running 任务清成终态，保证准入测试有干净的名额基线
    （会话级共享 DB，其他用例可能留下活跃任务）。"""
    for t in (
        db.query(DraftGenerationTask)
        .filter(DraftGenerationTask.status.in_(["queued", "running"]))
        .all()
    ):
        t.status = "failed"


# --------------------------------------------------------------------------
# 跨进程准入：_try_claim_running_slot
# --------------------------------------------------------------------------
def test_claim_slot_when_free(client):
    db = SessionLocal()
    try:
        _reset_active_tasks(db)
        did = _mk_domain(db, "c-claim-free")
        tid = _mk_task(db, did, "queued")
        db.commit()
    finally:
        db.close()

    assert _try_claim_running_slot(tid, max_running=2) == "claimed"

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, tid).status == "running"
        # 清理：避免占用名额影响后续用例
        db.get(DraftGenerationTask, tid).status = "failed"
        db.commit()
    finally:
        db.close()


def test_claim_slot_waits_when_at_max(client):
    db = SessionLocal()
    try:
        _reset_active_tasks(db)
        did = _mk_domain(db, "c-claim-max")
        r1 = _mk_task(db, did, "running")
        r2 = _mk_task(db, did, "running")  # 占满 max=2
        qid = _mk_task(db, did, "queued")
        db.commit()
    finally:
        db.close()

    assert _try_claim_running_slot(qid, max_running=2) == "waiting"

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, qid).status == "queued"
        # 清理占位 running，避免泄漏名额
        for tid in (r1, r2, qid):
            db.get(DraftGenerationTask, tid).status = "failed"
        db.commit()
    finally:
        db.close()


def test_claim_slot_gone_when_cancelled(client):
    db = SessionLocal()
    try:
        did = _mk_domain(db, "c-claim-gone")
        cid = _mk_task(db, did, "cancelled")
        db.commit()
    finally:
        db.close()

    assert _try_claim_running_slot(cid, max_running=2) == "gone"


def test_await_running_slot_admits_and_marks_running(client):
    db = SessionLocal()
    try:
        _reset_active_tasks(db)
        did = _mk_domain(db, "c-await-admit")
        tid = _mk_task(db, did, "queued")
        db.commit()
    finally:
        db.close()

    async def _noop(_tid, _p, _m):  # update_progress 存根
        return None

    admitted = asyncio.run(
        asyncio.wait_for(
            await_running_slot(tid, _noop, is_cancelled=lambda _t: False), timeout=5
        )
    )
    assert admitted is True

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, tid).status == "running"
        db.get(DraftGenerationTask, tid).status = "failed"
        db.commit()
    finally:
        db.close()


def test_await_running_slot_returns_false_when_cancelled(client):
    db = SessionLocal()
    try:
        _reset_active_tasks(db)
        did = _mk_domain(db, "c-await-cancel")
        tid = _mk_task(db, did, "queued")
        db.commit()
    finally:
        db.close()

    async def _noop(_tid, _p, _m):
        return None

    # 一进入即取消 → 不占名额、返回 False
    admitted = asyncio.run(
        asyncio.wait_for(
            await_running_slot(tid, _noop, is_cancelled=lambda _t: True), timeout=5
        )
    )
    assert admitted is False

    db = SessionLocal()
    try:
        assert db.get(DraftGenerationTask, tid).status == "queued"
    finally:
        db.close()


# --------------------------------------------------------------------------
# 派发切换：subprocess 模式下 _launch_draft_task 应 Popen 分离子进程
# --------------------------------------------------------------------------
def test_launch_dispatches_subprocess(monkeypatch, client):
    import app.api.workspace as ws

    calls = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs

    monkeypatch.setattr(ws.settings, "draft_worker_subprocess", True)
    monkeypatch.setattr(ws.subprocess, "Popen", _FakePopen)

    progress = DraftProgressOut(
        task_id="task-xyz", status="queued", progress=0, message="", scope="full"
    )

    async def _runner(_task_id):  # subprocess 模式下不会被调用
        raise AssertionError("runner should not run in subprocess mode")

    ws._launch_draft_task(progress, _runner)

    argv = calls["argv"]
    assert argv[0] == ws.sys.executable
    assert argv[1:] == ["-m", "app.jobs.draft_worker", "task-xyz"]
    assert calls["kwargs"].get("start_new_session") is True
    assert calls["kwargs"].get("cwd") == str(ws._BACKEND_DIR)
    assert calls["kwargs"].get("stderr") == subprocess.STDOUT
