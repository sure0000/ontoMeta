"""草稿生成并发控制器，限制同时执行的生成任务数量。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config import settings

_generation_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _generation_semaphore
    if _generation_semaphore is None:
        _generation_semaphore = asyncio.Semaphore(settings.max_concurrent_draft_generations)
    return _generation_semaphore


async def run_draft_generation_limited(
    task_id: str,
    update_progress: Callable[[str, int, str], Awaitable[None]],
    coro: Callable[[], Awaitable[None]],
    is_cancelled: Callable[[str], bool] | None = None,
) -> None:
    """在并发上限内执行草稿生成；排队时更新任务进度提示。"""
    semaphore = _get_semaphore()

    while semaphore.locked():
        if is_cancelled and is_cancelled(task_id):
            return
        await update_progress(task_id, 1, "排队等待中，等待其他任务完成...")
        await asyncio.sleep(1)

    if is_cancelled and is_cancelled(task_id):
        return

    async with semaphore:
        await coro()
