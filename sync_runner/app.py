"""ontometa-sync-runner 的 HTTP 接口（§3.1 接口契约）。

| 端点 | 用途 | 谁调 |
|---|---|---|
| ``GET /healthz`` | 存活 | preflight、Airflow |
| ``GET /capabilities`` | contract_version + 支持的源/目标 + 已装驱动 + backend 档 | preflight |
| ``POST /probe`` | ``{alias}`` → 该连接能否连通、能否读到指定表 | preflight |
| ``POST /jobs`` | 提交 JobSpec（幂等键 = dag_run_id+task_id） | Airflow task |
| ``GET /jobs/{id}`` | 状态、行数、水位、错误 | Airflow task 轮询 |
| ``GET /jobs/{id}/log`` | 该作业日志 | 回执跳转 |
| ``GET/PUT/DELETE /secrets[/{alias}]`` | 连接配置的读写（值只进不出） | ontoMeta 设置页代填 |

**鉴权**：设了 ``SYNC_RUNNER_TOKEN`` 就要求 ``Authorization: Bearer <token>``。
写 secrets 的接口**必须**有它——没设 token 时这些接口直接 403，而不是敞着。
读接口在未设 token 时放行（内网只读探活的既有用法不被打断）。

启动：``uvicorn sync_runner.app:app``。
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException
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


def _expected_token() -> str:
    return os.environ.get("SYNC_RUNNER_TOKEN") or ""


def require_token(authorization: str | None = Header(default=None)) -> None:
    """未设 SYNC_RUNNER_TOKEN 时放行（保持内网只读探活的既有用法）。"""
    expected = _expected_token()
    if not expected:
        return
    got = (authorization or "").removeprefix("Bearer ").strip()
    # 定长比较，避免按字符逐位比较泄露 token 长度/前缀信息。
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="鉴权失败：需要 Bearer <SYNC_RUNNER_TOKEN>")


def require_write_token(authorization: str | None = Header(default=None)) -> None:
    """写 secrets 必须有 token：没配 token 就等于任何人都能改凭据，宁可直接不可用。"""
    if not _expected_token():
        raise HTTPException(
            status_code=403,
            detail=(
                "runner 未设置 SYNC_RUNNER_TOKEN，凭据写入接口已禁用："
                "给 runner 设一个 token 后再从设置页管理连接。"
            ),
        )
    require_token(authorization)


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
        options = secrets.resolve_options(req.alias)
        if options:
            # 配了这个别名，只是没有可直连的 URL——由 Zeta 集群按 metastore 之类写入。
            # 报「连不通」会让人去修一个不存在的连接串（见 ProbeResult.checkable）。
            return ProbeResult(
                alias=req.alias,
                reachable=False,
                checkable=False,
                detail=(
                    f"别名「{req.alias}」不由 runner 直连（已配：{', '.join(sorted(options))}），"
                    "连通性由执行档自己保证，这里探不了。"
                ),
            )
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


# ---------- 连接配置（凭据只进不出） ----------


@app.get("/secrets", dependencies=[Depends(require_token)])
def list_secrets() -> dict:
    """列出已配的别名与**哪些键已设**。机密键只回「已设置」，绝不回明文。"""
    return {"items": secrets.describe()}


@app.put("/secrets/{alias}", dependencies=[Depends(require_write_token)])
def put_secret(alias: str, values: dict[str, str]) -> dict:
    """写入一个别名的连接配置。值落在 runner 自己的存储里，调用方不留副本。"""
    try:
        secrets.save(alias, values)
    except secrets.SecretIsEnvManaged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except secrets.SecretStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入失败：{exc}") from exc
    return {"alias": alias, "ok": True}


@app.delete("/secrets/{alias}", dependencies=[Depends(require_write_token)])
def delete_secret(alias: str) -> dict:
    try:
        removed = secrets.delete(alias)
    except secrets.SecretIsEnvManaged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except secrets.SecretStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"alias": alias, "removed": removed}
