"""分离子进程治理制品执行入口。

MCP 的 ``execute_task`` 先原子抢占制品，再由本模块使用独立数据库 Session 完成
Airflow 提交与回执落库。分离进程避免 MCP/HTTP 请求超时或服务 reload 中断提交。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_LOG_DIR = _BACKEND_DIR / ".logs"


def spawn_artifact_execution_worker(artifact_id: str) -> None:
    """拉起一个脱离当前请求进程组的制品执行 worker。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = open(  # noqa: SIM115 -- 子进程继承后由操作系统关闭
            _LOG_DIR / f"artifact-execution-worker-{artifact_id}.log",
            "ab",
            buffering=0,
        )
    except OSError:
        logfile = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-m", "app.jobs.artifact_execution_worker", artifact_id],
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


def _mark_crashed(artifact_id: str, exc: Exception) -> None:
    """顶层异常兜底；正常执行错误由 AgentPipelineService 自己落回执。"""
    try:
        from app.database import SessionLocal
        from app.models.agent import ArtifactStatus, GovernanceArtifact

        with SessionLocal() as db:
            artifact = db.get(GovernanceArtifact, artifact_id)
            if artifact is None or artifact.status != ArtifactStatus.EXECUTING.value:
                return
            artifact.status = ArtifactStatus.FAILED.value
            artifact.execution_receipt_json = json.dumps(
                {"error": f"执行 worker 异常退出：{exc}"},
                ensure_ascii=False,
                sort_keys=True,
            )
            artifact.executed_at = datetime.now(timezone.utc)
            db.commit()
    except Exception:
        logging.exception("failed to mark artifact %s failed", artifact_id)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: python -m app.jobs.artifact_execution_worker <artifact_id>",
            file=sys.stderr,
        )
        return 2
    artifact_id = args[0]

    logging.info("artifact_execution_worker: start artifact_id=%s", artifact_id)
    try:
        from app.database import SessionLocal
        from app.services.agent_pipeline import AgentPipelineService

        with SessionLocal() as db:
            artifact = AgentPipelineService().execute_claimed(db, artifact_id)
            logging.info(
                "artifact_execution_worker: done artifact_id=%s status=%s",
                artifact_id,
                artifact.status,
            )
        return 0
    except Exception as exc:
        logging.exception(
            "artifact_execution_worker: crashed artifact_id=%s: %s", artifact_id, exc
        )
        _mark_crashed(artifact_id, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
