"""ontometa-sync-runner 的 HTTP 接口（§3.1 接口契约）。

| 端点 | 用途 | 谁调 |
|---|---|---|
| ``GET /healthz`` | 存活 | preflight、Airflow |
| ``GET /capabilities`` | contract_version + 支持的源/目标 + 已装驱动 + backend 档 | preflight |
| ``POST /probe`` | ``{alias}`` → 该连接能否连通、能否读到指定表 | preflight |
| ``POST /jobs`` | 提交 JobSpec（幂等键 = dag_run_id+task_id） | Airflow task |
| ``GET /jobs/{id}`` | 状态、行数、水位、错误 | Airflow task 轮询 |
| ``GET /jobs/{id}/log`` | 该作业日志 | 回执跳转 |

启动：``uvicorn sync_runner.app:app``。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from sqlalchemy import MetaData, Table, create_engine, text

from sync_runner import secrets
from sync_runner.backends import capabilities as build_capabilities
from sync_runner.contract import (
    CONTRACT_VERSION,
    Capabilities,
    JobStatus,
    JobSubmit,
    ProbeRequest,
    ProbeResult,
)
from sync_runner.jobs import JobStore

app = FastAPI(title="ontometa-sync-runner", version=CONTRACT_VERSION)
_store = JobStore()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "contract_version": CONTRACT_VERSION}


@app.get("/capabilities", response_model=Capabilities)
def capabilities() -> Capabilities:
    return build_capabilities()


@app.post("/probe", response_model=ProbeResult)
def probe(req: ProbeRequest) -> ProbeResult:
    """按 alias 自解析连接串并试连；给了表名则顺带验能否读到它。preflight 提交前调。"""
    try:
        url = secrets.resolve(req.alias)
    except secrets.SecretNotFound as exc:
        return ProbeResult(alias=req.alias, reachable=False, detail=str(exc))

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 连不上的原因原样带出，供 preflight 显示
        engine.dispose()
        return ProbeResult(alias=req.alias, reachable=False, detail=str(exc))

    can_read: bool | None = None
    detail = "连接正常"
    if req.table:
        try:
            Table(req.table, MetaData(), autoload_with=engine, schema=req.database)
            can_read = True
            detail = f"连接正常，可读到表 {req.table}"
        except Exception as exc:  # noqa: BLE001
            can_read = False
            detail = f"连上了但读不到表 {req.table}：{exc}"
    engine.dispose()
    return ProbeResult(
        alias=req.alias, reachable=True, can_read_table=can_read, detail=detail
    )


@app.post("/jobs", response_model=JobStatus)
def submit_job(submit: JobSubmit) -> JobStatus:
    return _store.submit(submit)


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    status = _store.get(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} 不存在")
    return status


@app.get("/jobs/{job_id}/log")
def get_job_log(job_id: str) -> dict:
    lines = _store.log(job_id)
    if lines is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} 不存在")
    return {"job_id": job_id, "lines": lines}
