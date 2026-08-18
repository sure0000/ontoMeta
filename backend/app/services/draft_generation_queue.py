"""草稿生成并发控制器：Semaphore 限流 + DB 队列位次进度。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy import func, text

from app.config import settings
from app.database import SessionLocal
from app.models import DraftGenerationTask

logger = logging.getLogger("ontometa.draft_queue")

_generation_semaphore: asyncio.Semaphore | None = None

# 状态机：queued → running → succeeded | failed | cancelled
# 遗留 completed 视为 succeeded 终态
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"succeeded", "completed", "failed", "cancelled"})


def _get_semaphore() -> asyncio.Semaphore:
    global _generation_semaphore
    if _generation_semaphore is None:
        _generation_semaphore = asyncio.Semaphore(settings.max_concurrent_draft_generations)
    return _generation_semaphore


def get_queue_position(task_id: str) -> tuple[int, int]:
    """返回 (队列位次 1-based, 当前排队总数)。

    位次 = 创建时间不晚于本任务、且仍为 queued 的任务数（含自身）。
    若任务已不在排队，返回 (0, queued_total)。
    """
    db = SessionLocal()
    try:
        task = db.get(DraftGenerationTask, task_id)
        if task is None:
            return 0, 0
        queued_total = (
            db.query(func.count(DraftGenerationTask.id))
            .filter(DraftGenerationTask.status == "queued")
            .scalar()
            or 0
        )
        if task.status != "queued":
            return 0, int(queued_total)
        ahead = (
            db.query(func.count(DraftGenerationTask.id))
            .filter(
                DraftGenerationTask.status == "queued",
                DraftGenerationTask.created_at <= task.created_at,
            )
            .scalar()
            or 0
        )
        return int(ahead), int(queued_total)
    finally:
        db.close()


def mark_task_running(task_id: str) -> None:
    """获得执行名额后标记为 running。"""
    db = SessionLocal()
    try:
        task = db.get(DraftGenerationTask, task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        task.status = "running"
        if not task.message or "排队" in (task.message or ""):
            task.message = "开始生成本体草稿..."
        if task.progress < 2:
            task.progress = 2
        db.commit()
    finally:
        db.close()


def _try_claim_running_slot(task_id: str, max_running: int) -> str:
    """跨进程原子占用一个 running 名额（分离子进程模式，C）。

    以单条带子查询的 UPDATE 完成"检查名额 + 置 running"：SQLite 在语句执行期间持有
    写锁并在同一语句内求值 COUNT 子查询，故并发的多个 worker 会被串行化（配合
    busy_timeout 等待），不会两个进程同时抢到最后一个名额。

    返回 "claimed"（已占用可执行）/ "waiting"（暂无名额）/ "gone"（任务不存在或已终态，
    如被取消，调用方应直接退出）。
    """
    db = SessionLocal()
    try:
        task = db.get(DraftGenerationTask, task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return "gone"
        # 原子占名额：仅当仍排队且当前 running 数未达上限时才置 running。
        # 显式写 updated_at（原生 SQL 绕过 ORM 的 onupdate），喂饱 B 的陈旧宽限窗口。
        result = db.execute(
            text(
                "UPDATE draft_generation_tasks "
                "SET status='running', "
                "    progress=CASE WHEN progress < 2 THEN 2 ELSE progress END, "
                "    message=CASE WHEN message IS NULL OR message LIKE '%排队%' "
                "                 THEN '开始生成本体草稿...' ELSE message END, "
                "    updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:tid AND status='queued' "
                "  AND (SELECT COUNT(*) FROM draft_generation_tasks "
                "       WHERE status='running') < :maxr"
            ),
            {"tid": task_id, "maxr": int(max_running)},
        )
        db.commit()
        return "claimed" if result.rowcount == 1 else "waiting"
    finally:
        db.close()


async def await_running_slot(
    task_id: str,
    update_progress: Callable[[str, int, str], Awaitable[None]],
    is_cancelled: Callable[[str], bool] | None = None,
) -> bool:
    """跨进程并发准入（替代进程内 asyncio.Semaphore）：在 DB 内原子占用一个 running
    名额；暂无名额时按队列位次更新排队进度并轮询等待。

    返回 True 表示已置 running、可开始执行；False 表示任务已取消/不存在，调用方应直接退出。
    """
    max_running = settings.max_concurrent_draft_generations
    while True:
        if is_cancelled and is_cancelled(task_id):
            return False

        outcome = await asyncio.to_thread(_try_claim_running_slot, task_id, max_running)
        if outcome == "claimed":
            return True
        if outcome == "gone":
            return False

        pos, total = await asyncio.to_thread(get_queue_position, task_id)
        if pos > 0:
            msg = f"排队中（第 {pos} 位，共 {total} 个等待）…"
        else:
            msg = "排队等待中，等待执行名额…"
        await update_progress(task_id, 1, msg)
        await asyncio.sleep(1.0)


def _acquire_succeeded(acquire_task: asyncio.Task) -> bool:
    if not acquire_task.done() or acquire_task.cancelled():
        return False
    try:
        acquire_task.result()
        return True
    except Exception:
        return False


async def run_draft_generation_limited(
    task_id: str,
    update_progress: Callable[[str, int, str], Awaitable[None]],
    coro: Callable[[], Awaitable[None]],
    is_cancelled: Callable[[str], bool] | None = None,
) -> None:
    """在并发上限内执行草稿生成；等待期间按 DB 队列位次更新进度。"""
    semaphore = _get_semaphore()
    acquire_task = asyncio.create_task(semaphore.acquire())
    acquired = False

    try:
        while not acquire_task.done():
            if is_cancelled and is_cancelled(task_id):
                break

            pos, total = await asyncio.to_thread(get_queue_position, task_id)
            if pos > 0:
                msg = f"排队中（第 {pos} 位，共 {total} 个等待）…"
            else:
                msg = "排队等待中，等待执行名额…"
            await update_progress(task_id, 1, msg)
            await asyncio.wait({acquire_task}, timeout=1.0)

        if is_cancelled and is_cancelled(task_id):
            if _acquire_succeeded(acquire_task):
                acquired = True
                semaphore.release()
                acquired = False
            elif not acquire_task.done():
                acquire_task.cancel()
                with suppress(asyncio.CancelledError):
                    await acquire_task
            return

        await acquire_task
        acquired = True
    except asyncio.CancelledError:
        if _acquire_succeeded(acquire_task):
            semaphore.release()
        elif not acquire_task.done():
            acquire_task.cancel()
            with suppress(asyncio.CancelledError):
                await acquire_task
        raise

    if is_cancelled and is_cancelled(task_id):
        semaphore.release()
        return

    try:
        await asyncio.to_thread(mark_task_running, task_id)
        if is_cancelled and is_cancelled(task_id):
            return
        await coro()
    finally:
        if acquired:
            semaphore.release()
