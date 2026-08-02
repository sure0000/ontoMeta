"""Airflow REST 客户端：请求形状、幂等触发、错误封装。

用 httpx MockTransport，不需要真实 Airflow（比照 test_bigtop_manager.py 的做法）。
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


def test_terminal_states():
    assert is_terminal("success") and is_terminal("failed")
    assert not is_terminal("running")
    assert not is_terminal(None)  # 状态未知不能当成跑完了
