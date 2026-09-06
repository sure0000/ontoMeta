"""草稿生成的派发入口（中性位置）。

「起草一份本体草稿」有两个触发方：Web 的工作区、MCP 上的 agent。派发方式（分离子进程 /
进程内限流队列）是**部署事实**，不是某个入口的私事——两处各写一份，迟早只有一边跟上
`draft_worker_subprocess` 的语义，而差异要等到某个入口的任务莫名其妙不跑了才被发现。

默认走分离子进程（``start_new_session=True``）：脱离 uvicorn 进程组，``--reload`` 重启不会
波及生成任务。关掉时回退到进程内 asyncio 限流队列（测试/inline）。两条路径的进度、状态与
取消全部经由 DB，语义一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config import settings


def launch_draft_task(task_id: str, runner: Callable[[str], Awaitable[None]]) -> None:
    """派发一次草稿生成。``runner`` 是进程内回退路径要跑的协程工厂。"""
    if settings.draft_worker_subprocess:
        from app.jobs.draft_worker import spawn_draft_worker

        spawn_draft_worker(task_id)
        return

    from app.services.draft_generation_queue import run_draft_generation_limited
    from app.services.workspace_service import WorkspaceService

    async def _execute() -> None:
        await runner(task_id)

    task = asyncio.create_task(
        run_draft_generation_limited(
            task_id,
            WorkspaceService._update_task_progress,
            _execute,
            WorkspaceService._is_task_cancelled,
        )
    )
    WorkspaceService._track_draft_task(task_id, task)
