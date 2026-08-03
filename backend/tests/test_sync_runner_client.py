"""sync-runner 客户端：请求形状、错误封装、JobSpec→线格式无凭据。

用 httpx MockTransport，不需要真实 runner（比照 test_airflow_connector.py）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.connectors.sync_runner import (
    SyncRunnerClient,
    SyncRunnerError,
    job_spec_to_wire,
)
from app.warehouse.jobs import ColumnMapping, JobEndpoint, JobSpec


def _client(handler) -> SyncRunnerClient:
    transport = httpx.MockTransport(handler)
    return SyncRunnerClient(
        "http://sync-runner:8088", client=httpx.Client(transport=transport)
    )


def _job() -> JobSpec:
    return JobSpec(
        name="sync_dim_customer",
        source=JobEndpoint(alias="erp_readonly", platform="mariadb", table="tabCustomer", database="erp"),
        target=JobEndpoint(alias="warehouse_default", platform="doris", table="dim_customer", database="dw"),
        columns=(ColumnMapping(source="name", target="customer_name"),),
        mode="incremental",
        partition_key="modified",
    )


def test_job_spec_to_wire_has_alias_no_credentials():
    wire = job_spec_to_wire(_job())
    assert wire["source"]["alias"] == "erp_readonly"
    assert wire["target"]["platform"] == "doris"
    assert wire["columns"] == [{"source": "name", "target": "customer_name"}]
    assert wire["partition_key"] == "modified"
    # 凭据不进产物：整个线格式里只有 alias，没有任何 host/user/password 键。
    blob = json.dumps(wire)
    for forbidden in ("password", "host", "login", "://"):
        assert forbidden not in blob


def test_capabilities_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/capabilities"
        return httpx.Response(200, json={"contract_version": "1", "backends": ["native"]})

    out = _client(handler).capabilities()
    assert out["contract_version"] == "1" and out["backends"] == ["native"]


def test_submit_job_sends_spec_and_idempotency_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job_id": "j1", "state": "queued", "idempotency_key": "r1__t1"})

    out = _client(handler).submit_job(_job(), idempotency_key="r1__t1", watermark="2024-01-01")
    assert seen["path"] == "/jobs"
    assert seen["body"]["idempotency_key"] == "r1__t1"
    assert seen["body"]["watermark"] == "2024-01-01"
    assert seen["body"]["spec"]["name"] == "sync_dim_customer"
    assert out["state"] == "queued"


def test_probe_body_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"alias": "erp_readonly", "reachable": True})

    _client(handler).probe("erp_readonly", platform="mariadb", table="tabCustomer", database="erp")
    assert seen["body"] == {
        "alias": "erp_readonly",
        "platform": "mariadb",
        "table": "tabCustomer",
        "database": "erp",
    }


def test_http_error_wrapped_with_operation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(SyncRunnerError) as exc:
        _client(handler).get_job("j1")
    assert "get_job" in str(exc.value) and "500" in str(exc.value)


def test_default_client_disables_proxy_env():
    """内网服务不该走开发机代理——与 airflow/cube 连接器同处置（trust_env=False）。"""
    c = SyncRunnerClient("http://sync-runner:8088")
    try:
        assert c._client.trust_env is False
    finally:
        c.close()
