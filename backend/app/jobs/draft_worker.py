"""分离子进程草稿生成入口（C）。

用法::

    python -m app.jobs.draft_worker <task_id>

由 API worker 通过 ``subprocess.Popen(..., start_new_session=True)`` 拉起：子进程处于独立
会话/进程组，uvicorn ``--reload`` 重启或异常退出杀掉 API worker 时不会波及本进程，任务得以
跑到底。进度/状态/检查点全部落 DB（与进程内路径共用），取消走 DB 标志轮询。

注意：本入口**不**调用 ``init_db()``——迁移与陈旧任务回收是 API 启动的职责；子进程只复用已
建好的表与引擎（连接时会应用 WAL + busy_timeout PRAGMA）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

# backend/ 根目录与日志目录（与 app/api/workspace.py 的 _spawn_draft_worker 对齐）。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_LOG_DIR = _BACKEND_DIR / ".logs"


def spawn_draft_worker(task_id: str) -> None:
    """在分离子进程（``start_new_session=True``）拉起草稿生成 worker。

    供**失败自动续跑**复用同一执行路径：续跑同样 reload 免疫、脱离 uvicorn 进程组，
    日志落 ``.logs/draft-worker-<task_id>.log``。与 API 首发用的
    ``app.api.workspace._spawn_draft_worker`` 是同一行为的服务层孪生（后者带 FastAPI
    依赖，不宜从 worker/服务层导入，故此处独立留一份）。
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = open(  # noqa: SIM115 —— 交由子进程持有
            _LOG_DIR / f"draft-worker-{task_id}.log", "ab", buffering=0
        )
    except OSError:
        logfile = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-m", "app.jobs.draft_worker", task_id],
            cwd=str(_BACKEND_DIR),
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        if logfile not in (subprocess.DEVNULL, None):
            logfile.close()


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _execute(task_id: str) -> None:
    from app.services.draft_generation_queue import await_running_slot
    from app.services.query import WorkspaceService

    from app.database import SessionLocal
    from app.models import DraftGenerationTask
    from app.services.draft_generation_queue import TERMINAL_STATUSES

    # 读任务：拿 scope 与 domain，任务不存在/已终态（含被取消）直接退出。
    with SessionLocal() as db:
        task = db.get(DraftGenerationTask, task_id)
        if task is None:
            logging.warning("draft_worker: task %s not found, exit", task_id)
            return
        if task.status in TERMINAL_STATUSES:
            logging.info(
                "draft_worker: task %s already terminal (%s), exit", task_id, task.status
            )
            return
        scope = task.scope or "full"
        domain_id = task.domain_context_id

    svc = WorkspaceService()
    runners = {
        "full": svc._run_draft_generation,
        "objects": svc._run_object_generation,
        "relations": svc._run_relation_generation,
    }
    runner = runners.get(scope, svc._run_draft_generation)

    # 跨进程准入：占到 running 名额后执行；被取消/任务消失则直接返回。
    admitted = await await_running_slot(
        task_id,
        WorkspaceService._update_task_progress,
        WorkspaceService._is_task_cancelled,
    )
    if not admitted:
        logging.info("draft_worker: task %s cancelled/gone before start, exit", task_id)
        return
    if WorkspaceService._is_task_cancelled(task_id):
        return

    # _run_* 自身已捕获异常并落 failed / 处理取消；此处仅作调用。
    await runner(domain_id, task_id)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.jobs.draft_worker <task_id>", file=sys.stderr)
        return 2
    task_id = args[0]

    logging.info("draft_worker: start task_id=%s", task_id)
    try:
        asyncio.run(_execute(task_id))
        logging.info("draft_worker: done task_id=%s", task_id)
        return 0
    except Exception as exc:  # 顶层兜底：准入/导入/未预期崩溃时也把任务落 failed
        logging.exception("draft_worker: crashed task_id=%s: %s", task_id, exc)
        try:
            from app.services.query import WorkspaceService

            WorkspaceService._mark_task_failed(
                task_id, f"生成子进程异常退出：{WorkspaceService._describe_exc(exc)}"
            )
        except Exception:
            logging.exception("draft_worker: failed to mark task %s failed", task_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
