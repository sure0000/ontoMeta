"""Airflow REST 客户端（M10）：触发 DagRun、回读状态。

**ontoMeta 不做运行时执行器**（见 `MATERIALIZE_ORCHESTRATION.md` §2）：物化提交后由
Airflow 负责重试、补数、水位与并发，本模块只做两件事——把 DagRun 触发起来、把状态读回来。

凭据不入产物：连接信息来自 ``settings``（比照 DatahubSetting/CubeSetting 的 DB-backed 做法），
生成的 DAG 里只有 conn_id，不含任何账号密码。

⚠ **REST 版本**：Airflow 2.x 为 ``/api/v1``，3.x 为 ``/api/v2``。**不要用户去配**——
先按 v1 打，遇 404/405 自探一次 ``openapi.json``（``detect_api_version``），探到别的版本
就换过来重试。设置页因此没有 api_version 这一项：填了也只是把猜错的机会交给人。
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


def build_run_id(
    artifact_id: str | None, suffix: str | None = None, *, stamp: str | None = None
) -> str:
    """DagRun id ＝ ``ontometa__<制品>[__<批次>]__<本次提交时刻>``。

    **时间戳不是装饰**：run_id 曾只由制品 id 决定，注释写着「重复提交在 Airflow 侧幂等」，
    实际是 Airflow 对重复 run_id 回 409 ``already exists``——于是一个失败的任务**永远重试
    不了**，人在界面上再确认多少次都是同一句冲突。

    防重复提交本就不归 run_id 管：制品状态机只让 confirmed 执行、succeeded 直接回原回执
    （见 ``agent_pipeline.execute``）。走到这里就说明人确实要再跑一次，那就该是**新的一次**
    运行，回执里记的也是这个新 run_id。
    """
    from datetime import datetime, timezone

    parts = ["ontometa", artifact_id or "manual"]
    if suffix:
        parts.append(suffix)
    parts.append(stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return "__".join(parts)


class AirflowClient:
    """最小 Airflow 下发/回读客户端。``client`` 可注入（测试用 httpx.MockTransport）。"""

    def __init__(
        self,
        endpoint: str,
        *,
        username: str | None = None,
        password: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        """``api_version`` 只是**起点**：打不通时客户端会自探真实版本并换过来（见 ``_request``）。"""
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_version = api_version or DEFAULT_API_VERSION
        self._auth = (username, password) if username and password else None
        # 版本自协商每个客户端只做一次：探不到就认了，别把每个请求都拖成三次 openapi 探测。
        self._version_negotiated = False
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
        """``Accept`` 不是装饰：Airflow 的 ``/config`` 会按它做内容协商。

        httpx 默认发 ``Accept: */*``，Airflow 据此选 ``text/plain`` 返回 ini 文本，
        ``_request`` 在 ``response.json()`` 上抛「响应不是 JSON」，``get_config_option``
        再把它吞成 None——于是**任何**实例都被自检说成「关掉了 expose_config」，哪怕它
        开着。测试替身当时无视 Accept 恒返 JSON，这条路因此一直没被覆盖。
        """
        return {"Content-Type": "application/json", "Accept": "application/json"}

    def _send(self, method: str, path: str, operation: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(
                method, self._url(path), headers=self._headers(), auth=self._auth, **kwargs
            )
        except httpx.HTTPError as exc:
            raise AirflowError(operation, exc) from exc

    def _renegotiate_version(self) -> bool:
        """自探实例真实的 REST 版本，与当前不同就换过来（每个客户端只试一次）。"""
        if self._version_negotiated:
            return False
        self._version_negotiated = True
        detected = self.detect_api_version()
        if not detected or detected == self.api_version:
            return False
        logger.info("Airflow REST 版本自协商：%s → %s", self.api_version, detected)
        self.api_version = detected
        return True

    def _request(self, method: str, path: str, operation: str, **kwargs: Any) -> dict:
        response = self._send(method, path, operation, **kwargs)
        # 404/405 最常见的成因是 2.x(/api/v1) 与 3.x(/api/v2) 之争。自探一次真实版本，
        # 探到别的就换过来重试——这比让用户在设置页手配一个版本号可靠得多。
        if response.status_code in (404, 405) and self._renegotiate_version():
            response = self._send(method, path, operation, **kwargs)
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
                f"{self.endpoint}/health", headers=self._headers(), auth=self._auth
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

    def pause_dag(self, dag_id: str) -> dict:
        """Pause a scheduled DAG during an approved cut-over or rollback."""
        return self._request(
            "PATCH", f"/dags/{dag_id}", "pause_dag", json={"is_paused": True}
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

    def dag_exists(self, dag_id: str) -> bool:
        """DAG 是否已被 Airflow 解析并登记。用于 preflight 的 sentinel 探测。

        ``GET /dags/{id}`` 对未解析的 DAG 返回 404——把它翻成布尔，其余错误（鉴权、
        网络）照旧抛出，不吞。
        """
        try:
            self._request("GET", f"/dags/{dag_id}", "get_dag")
            return True
        except AirflowError as exc:
            if "404" in str(exc):
                return False
            raise

    def list_dag_ids(self, *, limit: int = 200, pattern: str | None = None) -> list[str]:
        """Airflow 已登记的 dag_id 列表。

        用途是判定「我们投出去的 DAG，Airflow 到底看没看见」——比 sentinel 的定时轮询
        强得多：sentinel 只等 20s，分不清「路径错」和「扫得慢」；而**历史**投过的 DAG
        若一个都不在册，那就与扫描间隔无关了。
        """
        params: dict[str, Any] = {"limit": limit}
        if pattern:
            params["dag_id_pattern"] = pattern
        body = self._request("GET", "/dags", "list_dags", params=params)
        return [d.get("dag_id") for d in (body.get("dags") or []) if d.get("dag_id")]

    def list_dag_filelocs(self, *, limit: int = 200) -> list[str]:
        """已登记 DAG 的源文件路径（``fileloc``），按实例自己看到的路径给。

        用途是**实证**投递目录与实例扫描目录等价：容器部署下两者本来就是两个字符串
        （宿主机路径 vs ``/opt/airflow/dags``），比字符串永远不一致。但只要有一个已注册
        DAG 的 fileloc 落在实例自报的 dags_folder 下的 ``ontometa/`` 里，就说明我们投出去
        的东西**确实**被它扫到了——这比让人去查 docker 挂载表可靠，也不需要远端有 docker。
        """
        body = self._request("GET", "/dags", "list_dags", params={"limit": limit})
        return [d.get("fileloc") for d in (body.get("dags") or []) if d.get("fileloc")]

    def get_config_option(self, section: str, option: str) -> str | None:
        """读 Airflow 自己的配置项；读不到（expose_config=False → 403/406）返回 None。

        能读到时可以把「两侧目录不一致」从推断变成对账：直接拿 core.dags_folder 和
        ontoMeta 的投递目录比字符串。故这里一律 best-effort，不把失败当错误。
        """
        try:
            body = self._request(
                "GET", "/config", "get_config", params={"section": section}
            )
        except AirflowError:
            return None
        for sec in body.get("sections") or []:
            if sec.get("name") != section:
                continue
            for opt in sec.get("options") or []:
                if opt.get("key") == option:
                    value = opt.get("value")
                    return str(value) if value is not None else None
        return None

    def get_connection(self, conn_id: str) -> dict:
        """读一个 Airflow Connection。物化建表任务按此 conn_id 连目标仓。

        只读账号可能对 ``/connections`` 无权（403）——由调用方（preflight）据错误码
        决定是判失败还是降级为「无法确认」，本方法只如实带出 HTTP 状态。
        """
        return self._request("GET", f"/connections/{conn_id}", "get_connection")

    def detect_api_version(self) -> str | None:
        """自探 Airflow 暴露的 REST 版本（``v1``/``v2``），认不出返回 None。

        不照抄文档：openapi.json 的位置本身随版本变（2.x 常在 ``/api/v1/openapi.json``，
        根路径 ``/openapi.json`` 可能 404），故逐个候选位置试，命中 JSON 后优先看
        ``servers[].url``（最权威），再退到候选路径 / paths 前缀。全程不抛，探不到即 None。
        """
        import re

        for candidate in ("/openapi.json", "/api/v1/openapi.json", "/api/v2/openapi.json"):
            try:
                resp = self._client.get(
                    f"{self.endpoint}{candidate}", headers=self._headers(), auth=self._auth
                )
            except httpx.HTTPError:
                continue
            if resp.status_code >= 400:
                continue
            try:
                doc = resp.json()
            except ValueError:
                continue
            for server in doc.get("servers") or []:
                match = re.search(r"/api/(v\d+)", server.get("url") or "")
                if match:
                    return match.group(1)
            match = re.search(r"/api/(v\d+)/", candidate)
            if match:
                return match.group(1)
            for path in doc.get("paths") or {}:
                match = re.search(r"^/api/(v\d+)/", path)
                if match:
                    return match.group(1)
        return None

    def list_task_instances(self, dag_id: str, dag_run_id: str) -> list[dict]:
        body = self._request(
            "GET",
            f"/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances",
            "list_task_instances",
        )
        return list(body.get("task_instances") or [])

    def get_xcom(
        self, dag_id: str, dag_run_id: str, task_id: str, key: str = "return_value"
    ) -> Any:
        """读一个任务的 XCom 值。任务没跑完/没留值时返回 None（404 不算错）。

        **逐个任务一次请求**，Airflow 没有跨任务批量读 XCom 的端点。故调用方必须按需读
        （单个任务展开时），不能在状态轮询里对整轮几百个任务全读一遍。

        Airflow 的 ``value`` 是**序列化后的字符串**（2.x 默认 JSON，也可能是 repr 过的
        Python 字面量）。这里尽力解析，解析不出就原样带出——回执宁可显示一段原文，
        也不该因为格式不认识而假装没有值。
        """
        import ast
        import json

        try:
            body = self._request(
                "GET",
                f"/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/{task_id}"
                f"/xcomEntries/{key}",
                "get_xcom",
            )
        except AirflowError as exc:
            if "404" in str(exc):
                return None
            raise
        value = body.get("value")
        if not isinstance(value, str):
            return value
        for parse in (json.loads, ast.literal_eval):
            try:
                return parse(value)
            except (ValueError, SyntaxError):
                continue
        return value

    # ---------- 展示 ----------

    def run_url(self, dag_id: str, dag_run_id: str) -> str:
        """Airflow UI 里这次运行的地址，放进回执供人一键跳转查日志。"""
        return f"{self.endpoint}/dags/{dag_id}/grid?dag_run_id={dag_run_id}"


def is_terminal(state: str | None) -> bool:
    return (state or "").lower() in TERMINAL_STATES


def explain_ping_failure(client: "AirflowClient", error: "AirflowError") -> str:
    """把 ``ping_api`` 的失败翻成人能照做的解释。

    ``/health`` 能通但带版本前缀的 REST 打不通时，失败几乎只有两类，分别给出下一步：

    - **401/403（鉴权）**：最常见是没开 basic_auth 后端（2.x 默认只有 session，仅供 Web UI）。
    - **404/405（版本/路径）**：客户端已在请求里自协商过版本（见 ``_request``），走到这里
      说明换版本也没用——要么探不到 ``openapi.json``，要么两个版本都 404。故这里只如实
      说明「已试过哪个版本」，不再要用户去改一个早已不存在的 api_version 配置项。

    其余错误原样带出，不臆测。返回补充说明后的完整 detail 字符串。
    """
    detail = str(error)
    if "401" in detail or "403" in detail:
        return detail + (
            "（/health 可通说明网络没问题，是 REST API 鉴权不通。"
            "Airflow 2.x 默认 api.auth_backends 只有 session，仅供 Web UI；"
            "请在 Airflow 侧设 AIRFLOW__API__AUTH_BACKENDS="
            "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session "
            "后重启 webserver，并确认设置页填的账号密码是该实例的 API 账号）"
        )
    if "404" in detail or "405" in detail:
        detected = client.detect_api_version()
        if detected:
            return detail + (
                f"（已按实测版本 {detected} 请求仍 404/405，"
                "多半是端点路径或反向代理问题，非版本不符）"
            )
        return detail + (
            f"（已按 {client.api_version} 请求且返回 404/405，"
            "又探不到 openapi.json 无法自动确认版本；"
            "请核对 endpoint 是否指向 Airflow webserver 本身而非其前置代理的子路径）"
        )
    return detail
