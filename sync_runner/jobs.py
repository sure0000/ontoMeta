"""作业存储与执行：内存作业库 + 后台线程 + 幂等键。

**幂等**（§3.3）：``idempotency_key = dag_run_id + task_id``。同一个键重复提交返回同一个
job，重跑不会搬第二遍——Airflow 任务重试、DAG 重复触发都因此天然安全。

**异步执行**：``POST /jobs`` 立即返回 ``queued``，搬运在后台线程跑，Airflow task 轮询
``GET /jobs/{id}`` 拿状态。M14 用进程内内存库（单进程、无状态外部依赖，挂了 preflight 立刻红，
不会静默）；作业历史不跨重启保留——重启后 Airflow 重试会用同一幂等键重新提交，语义仍正确。
"""

from __future__ import annotations

import threading
import uuid

from sync_runner import secrets
from sync_runner.backends import pick_backend, seatunnel
from sync_runner.backends.native import run_native
from sync_runner.contract import FAILED, QUEUED, RUNNING, SUCCESS, JobStatus, JobSubmit


class JobStore:
    def __init__(self) -> None:
        self._by_id: dict[str, JobStatus] = {}
        self._by_key: dict[str, str] = {}
        self._logs: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def submit(self, submit: JobSubmit) -> JobStatus:
        """提交一个作业。幂等键已存在则直接返回既有 job，不重复搬。"""
        with self._lock:
            existing = self._by_key.get(submit.idempotency_key)
            if existing is not None:
                return self._by_id[existing].model_copy(deep=True)
            job_id = uuid.uuid4().hex
            status = JobStatus(
                job_id=job_id,
                idempotency_key=submit.idempotency_key,
                state=QUEUED,
                target=submit.spec.target.qualified,
            )
            self._by_id[job_id] = status
            self._by_key[submit.idempotency_key] = job_id
            self._logs[job_id] = []
        threading.Thread(
            target=self._run, args=(job_id, submit), daemon=True
        ).start()
        return status.model_copy(deep=True)

    def get(self, job_id: str) -> JobStatus | None:
        with self._lock:
            status = self._by_id.get(job_id)
            return status.model_copy(deep=True) if status is not None else None

    def log(self, job_id: str) -> list[str] | None:
        with self._lock:
            lines = self._logs.get(job_id)
            return list(lines) if lines is not None else None

    # ---------- 内部 ----------

    def _set(self, job_id: str, **fields) -> None:
        with self._lock:
            status = self._by_id[job_id]
            for key, value in fields.items():
                setattr(status, key, value)

    def _append(self, job_id: str, line: str) -> None:
        with self._lock:
            self._logs.setdefault(job_id, []).append(line)

    def _run(self, job_id: str, submit: JobSubmit) -> None:
        spec = submit.spec
        self._set(job_id, state=RUNNING)
        self._append(job_id, f"run {spec.name}: {spec.source.qualified} -> {spec.target.qualified} mode={spec.mode}")
        try:
            backend = pick_backend(spec)
            if backend is None:
                # 不静默降级：两档都搬不了就明说，planner 本应已按 capabilities 把它
                # 列进 unsupported，走到这里说明两侧对能力的认知不一致。
                raise RuntimeError(
                    f"没有能搬这张表的档位（源 {spec.source.platform} / 目标 "
                    f"{spec.target.platform} / 装载 {spec.mode}）：native 不支持该组合或"
                    "驱动未装，seatunnel 档不可用或不支持该组合。"
                )
            # 进 JobStatus 而不只是日志：Airflow 任务会把它带回 XCom，ontoMeta 的回执
            # 据此说清「这张表实际用了哪一档」。
            self._set(job_id, backend=backend)
            self._append(job_id, f"backend={backend}")

            if backend == "native":
                result = run_native(
                    spec,
                    source_url=secrets.resolve(spec.source.alias),
                    target_url=secrets.resolve(spec.target.alias),
                    watermark=submit.watermark,
                    batch_size=submit.batch_size,
                )
                fields = {
                    "rows_read": result.rows_read,
                    "rows_written": result.rows_written,
                    "watermark_before": result.watermark_before,
                    "watermark_after": result.watermark_after,
                }
            else:
                st = seatunnel.run(spec, watermark=submit.watermark)
                # 水位不回报：seatunnel 档目前只做全量（见 SINK_MODES 的说明），
                # 没有水位可言，填个假的比留空更误导。
                fields = {"rows_read": st.rows_read, "rows_written": st.rows_written}
                self._append(job_id, f"zeta job {st.job_id}")

            self._set(job_id, state=SUCCESS, **fields)
            self._append(
                job_id,
                f"done read={fields['rows_read']} written={fields['rows_written']}",
            )
        except Exception as exc:  # noqa: BLE001 — 作业失败要如实进回执，不吞
            self._set(job_id, state=FAILED, error=str(exc))
            self._append(job_id, f"FAILED: {exc}")
