"""Airflow REST 客户端（M10）：触发 DagRun、回读状态。

**ontoMeta 不做运行时执行器**（见 `MATERIALIZE_ORCHESTRATION.md` §2）：物化提交后由
Airflow 负责重试、补数、水位与并发，本模块只做两件事——把 DagRun 触发起来、把状态读回来。

凭据不入产物：连接信息来自 ``settings``（比照 DatahubSetting/CubeSetting 的 DB-backed 做法），
生成的 DAG 里只有 conn_id，不含任何账号密码。

⚠ **REST 版本**：Airflow 2.x 为 ``/api/v1``，3.x 为 ``/api/v2``。本客户端把版本做成参数，
起栈后用 ``GET /openapi.json`` 实测确认，不照抄文档（见
`docker/orchestration/README.md`「起栈后待核实项」）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ontometa.connectors.airflow")

DEFAULT_API_VERSION = "v1"
# DagRun 的终态。非终态即仍在跑，前端据此决定是否继续轮询。
TERMINAL_STATES = frozenset({"success", "failed"})


class AirflowError(RuntimeError):
    """Airflow 侧的可读错误。带上操作名，便于回执里说清是哪一步失败。"""

    def __init__(self, operation: str, cause: Exception | str):
        self.operation = operation
        super().__init__(f"Airflow {operation} 失败：{cause}")


class AirflowClient:
    """最小 Airflow 下发/回读客户端。``client`` 可注入（测试用 httpx.MockTransport）。"""

    def __init__(
        self,
        endpoint: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_version = api_version or DEFAULT_API_VERSION
        self._auth = (username, password) if username and password else None
        self._token = token
        # trust_env=False：Airflow 是内网服务，绝不该走开发机的 HTTP(S)_PROXY / ALL_PROXY。
        # 尤其 ALL_PROXY=socks5://… 时 httpx 会直接抛 ImportError（缺 socksio），
        # 连通性测试因此永远失败。与 cube/datahub 连接器、services.common 同一处置。
        self._client = client or httpx.Client(trust_env=False, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ---------- 内部 ----------

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/api/{self.api_version}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, path: str, operation: str, **kwargs: Any) -> dict:
        try:
            response = self._client.request(
                method,
                self._url(path),
                headers=self._headers(),
                auth=self._auth if not self._token else None,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise AirflowError(operation, exc) from exc
        if response.status_code >= 400:
            # 原样带出 Airflow 的错误体：它通常已经说清了原因（DAG 不存在、run 重复等）。
            raise AirflowError(operation, f"HTTP {response.status_code} {response.text[:300]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AirflowError(operation, f"响应不是 JSON：{response.text[:200]}") from exc

    # ---------- 操作 ----------

    def health(self) -> dict:
        """健康检查走的是 ``/health``（Airflow 2.x 无需鉴权、不带 API 版本前缀）。

        与 ``_request`` 对齐：带上鉴权、原样带出错误体、非 JSON 也给可读错误。
        反向代理/受保护部署下 ``/health`` 可能要鉴权或回登录页，否则测试按钮只会
        吐一个没信息的 ``HTTP 4xx``（甚至抛未捕获的 ``ValueError``）。
        """
        try:
            response = self._client.get(
                f"{self.endpoint}/health",
                headers=self._headers(),
                auth=self._auth if not self._token else None,
            )
        except httpx.HTTPError as exc:
            raise AirflowError("health", exc) from exc
        if response.status_code >= 400:
            raise AirflowError("health", f"HTTP {response.status_code} {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise AirflowError(
                "health", f"响应不是 JSON（可能是登录页/反向代理）：{response.text[:200]}"
            ) from exc

    def ping_api(self) -> dict:
        """探一次**带版本前缀的 REST API**，确认鉴权真的能用。

        ``/health`` 在 Airflow 2.x 默认匿名可读，只测它会给出「连通正常」的假绿灯：
        真正下发 DagRun 时才发现 ``/api/v1/*`` 回 401。最常见的原因是部署没开
        basic_auth 后端（2.x 默认 ``api.auth_backends`` 只有 ``session``，仅供 Web UI 用），
        此时应在 Airflow 侧加上
        ``AIRFLOW__API__AUTH_BACKENDS=airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session``。
        """
        return self._request("GET", "/dags", "ping_api", params={"limit": 1})

    def unpause_dag(self, dag_id: str) -> dict:
        """新 DAG 默认可能是暂停态，触发前先取消暂停，否则 run 会一直排队不跑。"""
        return self._request(
            "PATCH", f"/dags/{dag_id}", "unpause_dag", json={"is_paused": False}
        )

    def trigger_dag(
        self, dag_id: str, *, dag_run_id: str, conf: dict | None = None
    ) -> dict:
        """触发一次运行。

        ``dag_run_id`` 由调用方给**确定性**值（制品 id）：Airflow 对重复 run_id 返回 409，
        重复提交因而天然幂等，不会跑第二遍。
        """
        return self._request(
            "POST",
            f"/dags/{dag_id}/dagRuns",
            "trigger_dag",
            json={"dag_run_id": dag_run_id, "conf": conf or {}},
        )

    def get_dag_run(self, dag_id: str, dag_run_id: str) -> dict:
        return self._request(
            "GET", f"/dags/{dag_id}/dagRuns/{dag_run_id}", "get_dag_run"
        )

    def list_task_instances(self, dag_id: str, dag_run_id: str) -> list[dict]:
        body = self._request(
            "GET",
            f"/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances",
            "list_task_instances",
        )
        return list(body.get("task_instances") or [])

    # ---------- 展示 ----------

    def run_url(self, dag_id: str, dag_run_id: str) -> str:
        """Airflow UI 里这次运行的地址，放进回执供人一键跳转查日志。"""
        return f"{self.endpoint}/dags/{dag_id}/grid?dag_run_id={dag_run_id}"


def is_terminal(state: str | None) -> bool:
    return (state or "").lower() in TERMINAL_STATES
