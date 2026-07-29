"""遗留4：Bigtop Manager 下发客户端与集群 Executor 的下发路径。

与 M7 DataHub 回写同样的纪律：接口按 BM ``release-1.1.0`` 源码构造，测试只断言**请求体形状
与握手序列**（不连真实 BM）；默认不下发，下发须显式 opt-in；凭据只经 context、不进 Spec。

无 pytest-asyncio，异步用 ``asyncio.run(...)``。这些是纯客户端/执行器测试，不碰数据库，
故不需要 client fixture。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agents.drafters.cluster import ClusterDrafter
from app.agents.executors.cluster import ClusterExecutor
from app.connectors import bigtop_manager as bm


# ---------- PBKDF2 口令派生 ----------


def test_derive_login_password_matches_bm_params():
    """PBKDF2-HMAC-SHA256, 600000 轮, 32B → 64 位小写 hex；确定性。"""
    derived = bm.derive_login_password("admin123", "somesalt")
    assert len(derived) == 64
    assert derived == derived.lower()
    int(derived, 16)  # 合法 hex
    assert derived == bm.derive_login_password("admin123", "somesalt")
    assert derived != bm.derive_login_password("admin123", "othersalt")


def test_unwrap_tolerates_enveloped_and_bare():
    assert bm._unwrap({"code": 0, "message": "ok", "data": {"token": "t"}}) == {"token": "t"}
    assert bm._unwrap("SALT") == "SALT"
    # 非信封 dict（业务对象自带 data 字段但还有别的键）不误拆
    assert bm._unwrap({"id": 1, "data": 2, "extra": 3}) == {"id": 1, "data": 2, "extra": 3}


# ---------- HostReq / CommandReq 构造 ----------


def test_build_host_req_inlines_ssh_from_context_not_spec():
    """BM 要内联 SSH 明文——这些只从运行时 ssh 字典来，Spec 里从没有。"""
    ssh = {"sshUser": "root", "sshPort": 2222, "authType": 1, "sshPassword": "hunter2"}
    req = bm.build_host_req(["n1", "n2"], ssh)
    assert req["hostnames"] == ["n1", "n2"]
    assert req["sshUser"] == "root"
    assert req["sshPort"] == 2222
    assert req["sshPassword"] == "hunter2"
    assert req["grpcPort"] == 8835  # 默认


def test_build_cluster_command_shape():
    cmd = bm.build_cluster_command(
        name="c1", display_name="集群1", hostnames=["n1"],
        ssh={"sshPassword": "x"},
    )
    assert cmd["command"] == "add"
    assert cmd["commandLevel"] == "cluster"
    assert cmd["clusterCommand"]["name"] == "c1"
    assert cmd["clusterCommand"]["hosts"][0]["hostnames"] == ["n1"]


# ---------- 三步握手 + 提交 command（MockTransport） ----------


def _mock_bm(records: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        records.append(request)
        path = request.url.path
        if path == "/api/salt":
            return httpx.Response(200, json={"code": 0, "data": "SALT123"})  # 信封
        if path == "/api/nonce":
            return httpx.Response(200, json="NONCE123")  # 裸值
        if path == "/api/login":
            body = json.loads(request.content)
            assert body["username"] == "admin"
            assert len(body["password"]) == 64  # 已派生，非明文
            assert body["nonce"] == "NONCE123"
            return httpx.Response(200, json={"data": {"token": "JWT-XYZ"}})
        if path == "/api/command":
            return httpx.Response(200, json={"data": {"id": 42, "state": "Pending"}})
        if path.endswith("/jobs/42"):
            return httpx.Response(200, json={"data": {"id": 42, "state": "Successful", "progress": 100}})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_login_handshake_sequence_and_token_header():
    records: list[httpx.Request] = []
    client = bm.BigtopManagerClient("http://bm:8080", client=_mock_bm(records))

    async def run():
        token = await client.login("admin", "rawpass")
        job = await client.submit_command({"command": "add", "commandLevel": "cluster"})
        await client.aclose()
        return token, job

    token, job = asyncio.run(run())
    assert token == "JWT-XYZ"
    assert job == {"id": 42, "state": "Pending"}
    # 握手序列：salt → nonce → login → command
    assert [r.url.path for r in records] == [
        "/api/salt", "/api/nonce", "/api/login", "/api/command",
    ]
    # 登录后请求带自定义 Token 头（非 Authorization: Bearer）
    command_req = records[-1]
    assert command_req.headers.get("Token") == "JWT-XYZ"
    assert "Authorization" not in command_req.headers


def test_submit_command_wraps_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = bm.BigtopManagerClient(
        "http://bm:8080", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    async def run():
        client._token = "t"
        try:
            await client.submit_command({"x": 1})
        finally:
            await client.aclose()

    with pytest.raises(bm.BigtopManagerError) as exc:
        asyncio.run(run())
    assert exc.value.operation == "submit_command"


# ---------- Executor 下发路径 ----------


def test_executor_dispatches_when_opted_in():
    """allow_dispatch=true + context 提供凭据 → 真实提交建集群 command。"""
    spec = ClusterDrafter().draft("部署 hdfs", {"hosts": ["n1", "n2"]})
    records: list[httpx.Request] = []
    context = {
        "allow_dispatch": True,
        "bm_endpoint": "http://bm:8080",
        "bm_username": "admin",
        "bm_password": "rawpass",
        "ssh_credentials": {"sshUser": "root", "sshPassword": "hunter2"},
        "bm_http_client": _mock_bm(records),
    }
    out = ClusterExecutor().execute(spec, context)
    assert out["dispatched"] is True
    assert out["cluster_job"] == {"id": 42, "state": "Pending"}

    # SSH 明文进了发给 BM 的 command，但**从未**出现在 Spec 里。
    command_body = json.loads(records[-1].content)
    host = command_body["clusterCommand"]["hosts"][0]
    assert host["sshPassword"] == "hunter2"
    assert host["hostnames"] == ["n1", "n2"]
    assert "sshPassword" not in json.dumps(spec, ensure_ascii=False)
    assert "hunter2" not in json.dumps(spec, ensure_ascii=False)


def test_executor_refuses_dispatch_without_runtime_credentials():
    """opt-in 了但缺运行时凭据 → 显式报错，不下发连不上的作业。"""
    spec = ClusterDrafter().draft("部署 hdfs", {"hosts": ["n1"]})
    context = {"allow_dispatch": True, "bm_endpoint": "http://bm:8080"}
    with pytest.raises(ValueError, match="缺少运行时凭据"):
        ClusterExecutor().execute(spec, context)
