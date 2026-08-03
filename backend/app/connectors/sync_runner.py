"""ontometa-sync-runner 的 ontoMeta 侧客户端（M14）。

比照 ``connectors/airflow.py``：``trust_env=False``（runner 是内网服务，不该走开发机代理）、
错误统一封装成可读的 :class:`SyncRunnerError`、``client`` 可注入（测试用 httpx.MockTransport）。

**契约版本**：``EXPECTED_CONTRACT_VERSION`` 是 ontoMeta 认识的 runner 线格式版本。preflight
用 :meth:`capabilities` 比对，不匹配即拒绝提交（不是发过去再看会不会炸，§3.5）。

**凭据不进产物不变量**：本客户端发给 runner 的 JobSpec 里只有 alias，runner 自解析连接串
（见 :func:`job_spec_to_wire`）。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.warehouse.jobs import JobSpec

# ontoMeta 认识的 runner 契约版本。runner 的 GET /capabilities.contract_version 与此比对。
EXPECTED_CONTRACT_VERSION = "1"


class SyncRunnerError(RuntimeError):
    """runner 侧的可读错误，带操作名，便于回执说清是哪一步失败。"""

    def __init__(self, operation: str, cause: Exception | str):
        self.operation = operation
        super().__init__(f"sync-runner {operation} 失败：{cause}")


def job_spec_to_wire(job: JobSpec) -> dict:
    """backend 的 JobSpec → runner 线格式。只带搬运需要的字段，凭据不在内（只有 alias）。"""
    return {
        "name": job.name,
        "source": {
            "alias": job.source.alias,
            "platform": job.source.platform,
            "table": job.source.table,
            "database": job.source.database,
        },
        "target": {
            "alias": job.target.alias,
            "platform": job.target.platform,
            "table": job.target.table,
            "database": job.target.database,
        },
        "columns": [{"source": c.source, "target": c.target} for c in job.columns],
        "mode": job.mode,
        "partition_key": job.partition_key,
    }


class SyncRunnerClient:
    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.endpoint = (endpoint or "").rstrip("/")
        # trust_env=False：内网服务不走开发机 HTTP(S)_PROXY / ALL_PROXY，与 airflow/cube 连接器同处置。
        self._client = client or httpx.Client(trust_env=False, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ---------- 内部 ----------

    def _request(self, method: str, path: str, operation: str, **kwargs: Any) -> dict:
        try:
            response = self._client.request(method, f"{self.endpoint}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise SyncRunnerError(operation, exc) from exc
        if response.status_code >= 400:
            raise SyncRunnerError(
                operation, f"HTTP {response.status_code} {response.text[:300]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise SyncRunnerError(operation, f"响应不是 JSON：{response.text[:200]}") from exc

    # ---------- 操作 ----------

    def healthz(self) -> dict:
        return self._request("GET", "/healthz", "healthz")

    def capabilities(self) -> dict:
        return self._request("GET", "/capabilities", "capabilities")

    def probe(
        self,
        alias: str,
        *,
        platform: str | None = None,
        table: str | None = None,
        database: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            "/probe",
            "probe",
            json={
                "alias": alias,
                "platform": platform,
                "table": table,
                "database": database,
            },
        )

    def submit_job(
        self,
        job: JobSpec | dict,
        *,
        idempotency_key: str,
        watermark: str | None = None,
        batch_size: int = 1000,
    ) -> dict:
        """提交一个搬运作业。``idempotency_key`` 由调用方给确定性值（dag_run_id+task_id），
        runner 对重复键返回同一个 job，重复提交天然幂等。"""
        spec = job_spec_to_wire(job) if isinstance(job, JobSpec) else job
        return self._request(
            "POST",
            "/jobs",
            "submit_job",
            json={
                "spec": spec,
                "idempotency_key": idempotency_key,
                "watermark": watermark,
                "batch_size": batch_size,
            },
        )

    def get_job(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}", "get_job")

    def get_job_log(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}/log", "get_job_log")
