"""Airflow REST 客户端：请求形状、幂等触发、错误封装。

用 httpx MockTransport，不需要真实 Airflow。
真实实例上的 REST 版本与路径以起栈后 ``GET /openapi.json`` 为准，见
docker/orchestration/README.md「起栈后待核实项」。
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors.airflow import AirflowClient, AirflowError, is_terminal


def _client(handler, **kwargs) -> AirflowClient:
    transport = httpx.MockTransport(handler)
    return AirflowClient(
        "http://airflow:8080",
        username="admin",
        password="admin",
        client=httpx.Client(transport=transport),
        **kwargs,
    )


def test_trigger_dag_uses_given_run_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"dag_run_id": "r1", "state": "queued"})

    client = _client(handler)
    out = client.trigger_dag("dag1", dag_run_id="r1", conf={"a": 1})

    assert seen["url"] == "http://airflow:8080/api/v1/dags/dag1/dagRuns"
    # run_id 由调用方给定 —— 重复提交时 Airflow 靠它返回 409，从而天然幂等
    assert seen["body"]["dag_run_id"] == "r1"
    assert seen["body"]["conf"] == {"a": 1}
    assert out["state"] == "queued"


def test_duplicate_run_id_surfaces_as_error():
    """重复提交：Airflow 返回 409，客户端如实报错而不是假装成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="DAGRun with run_id r1 already exists")

    with pytest.raises(AirflowError) as exc:
        _client(handler).trigger_dag("dag1", dag_run_id="r1")
    assert "409" in str(exc.value)
    assert "trigger_dag" in str(exc.value)


def test_unpause_before_trigger():
    """新 DAG 可能是暂停态，不取消暂停会一直排队不跑。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"is_paused": False})

    _client(handler).unpause_dag("dag1")
    assert seen["method"] == "PATCH"
    assert seen["body"] == {"is_paused": False}


def test_api_version_is_configurable():
    """Airflow 2.x=/api/v1，3.x=/api/v2——版本做成参数，不写死。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    _client(handler, api_version="v2").get_dag_run("dag1", "r1")
    assert "/api/v2/dags/dag1/dagRuns/r1" in seen["url"]


def test_network_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AirflowError, match="get_dag_run"):
        _client(handler).get_dag_run("dag1", "r1")


def test_task_instances_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "task_instances": [
                    {"task_id": "create_tables", "state": "success"},
                    {"task_id": "sync_dim_customer", "state": "running"},
                ]
            },
        )

    tasks = _client(handler).list_task_instances("dag1", "r1")
    assert [t["task_id"] for t in tasks] == ["create_tables", "sync_dim_customer"]


def test_default_client_ignores_proxy_env(monkeypatch):
    """内网服务不走开发机代理。

    ALL_PROXY=socks5://… 时 httpx 若 trust_env，会直接抛 ImportError（缺 socksio），
    连通性测试因此永远失败——现实里踩过一次，钉住。
    """
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:17891")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:17891")

    client = AirflowClient("http://airflow:8080")
    try:
        assert client._client.trust_env is False
    finally:
        client.close()


def test_ping_api_hits_versioned_api():
    """探鉴权要打带版本前缀的 API —— /health 匿名可读，只测它是假绿灯。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"dags": [], "total_entries": 0})

    _client(handler).ping_api()
    assert "/api/v1/dags" in seen["url"]


def test_ping_api_surfaces_401():
    """没开 basic_auth 后端时 API 回 401，必须报错而不是当成连通。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"title": "Unauthorized"}')

    with pytest.raises(AirflowError) as exc:
        _client(handler).ping_api()
    assert "401" in str(exc.value)


def test_explain_ping_failure_auth_hint_on_401():
    """401 → 补上「开 basic_auth 后端」的下一步，不去探版本。"""
    from app.connectors.airflow import explain_ping_failure

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"title": "Unauthorized"}')

    client = _client(handler)
    try:
        client.ping_api()
    except AirflowError as exc:
        msg = explain_ping_failure(client, "v1", exc)
    assert "401" in msg
    assert "AUTH_BACKENDS" in msg


def test_explain_ping_failure_detects_version_mismatch_on_404():
    """404 且配的版本与实测不符 → 自动探测并明确告知应改成哪个版本。"""
    from app.connectors.airflow import explain_ping_failure

    def handler(request: httpx.Request) -> httpx.Response:
        # /api/v1/dags 打不通（404）；openapi 探测暴露的是 v2。
        if "openapi" in request.url.path:
            return httpx.Response(200, json={"servers": [{"url": "/api/v2"}]})
        return httpx.Response(404, text='{"title": "Not Found"}')

    client = _client(handler, api_version="v1")
    try:
        client.ping_api()
    except AirflowError as exc:
        msg = explain_ping_failure(client, "v1", exc)
    assert "v2" in msg
    assert "api_version" in msg


def test_explain_ping_failure_404_when_version_undetectable():
    """404 且探不到 openapi → 不臆测版本，只提示手动核对 v1/v2。"""
    from app.connectors.airflow import explain_ping_failure

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"title": "Not Found"}')

    client = _client(handler, api_version="v1")
    try:
        client.ping_api()
    except AirflowError as exc:
        msg = explain_ping_failure(client, "v1", exc)
    assert "404" in msg
    assert "openapi.json" in msg


def test_xcom_parses_json_value():
    """Airflow 的 XCom value 是序列化后的字符串，要解回结构才能取 backend/行数。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"key": "return_value", "value": '{"backend": "native", "rows_written": 7}'}
        )

    out = _client(handler).get_xcom("dag1", "r1", "sync_dim_a")
    assert "/dags/dag1/dagRuns/r1/taskInstances/sync_dim_a/xcomEntries/return_value" in seen["url"]
    assert out == {"backend": "native", "rows_written": 7}


def test_xcom_parses_python_repr_value():
    """2.x 有的版本存的是 repr 过的 Python 字面量（单引号），JSON 解不动，要退到字面量解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": "{'backend': 'seatunnel'}"})

    assert _client(handler).get_xcom("dag1", "r1", "t") == {"backend": "seatunnel"}


def test_xcom_unparseable_value_is_returned_raw():
    """认不出的格式原样带出——回执宁可显示一段原文，也别因为格式不认识就丢掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": "not-json-at-all"})

    assert _client(handler).get_xcom("dag1", "r1", "t") == "not-json-at-all"


def test_xcom_missing_is_none_not_error():
    """任务没跑完/不产 XCom 时 404。这不是错误，别把它变成一个红色的回执。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"title": "XCom entry not found"}')

    assert _client(handler).get_xcom("dag1", "r1", "create_tables") is None


def test_terminal_states():
    assert is_terminal("success") and is_terminal("failed")
    assert not is_terminal("running")
    assert not is_terminal(None)  # 状态未知不能当成跑完了
