"""sync_domains 错误暴露口径：区分"真不可达"与"配置/服务端错误"。

回归钉子：以前 `sync_domains` 用 `except Exception` 把 404/GraphQL 错误也静默降级为
本地缓存，工作区只显示空数据域、看不出是 gms_url 填错。现在只有传输层错误(真不可达)
才回退缓存；HTTP 4xx/5xx 与 GraphQL 业务错误须上抛，让 /domains 端点回 502 暴露。
"""

from __future__ import annotations

from unittest.mock import patch

import httpx

from app.api.deps import workspace


class _FakeConn:
    """假连接器：list_domains 抛指定异常，aclose 为 async no-op。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def list_domains(self):
        raise self._exc

    async def aclose(self):
        return None


def _get_domains(client, admin_headers, exc: Exception):
    with patch.object(workspace, "_datahub", return_value=_FakeConn(exc)):
        return client.get("/api/domains", headers=admin_headers)


def test_http_404_surfaces_as_502(client, admin_headers):
    """gms_url 填成前端口致 404：不再静默吞掉，端点回 502 并点名 DataHub。"""
    request = httpx.Request("POST", "http://h:9002/gms/api/graphql")
    response = httpx.Response(404, request=request)
    err = httpx.HTTPStatusError("Not Found", request=request, response=response)
    res = _get_domains(client, admin_headers, err)
    assert res.status_code == 502, res.text
    assert "DataHub" in res.json()["detail"]


def test_graphql_runtime_error_surfaces_as_502(client, admin_headers):
    """GraphQL 业务错误(RuntimeError)：上抛为 502，而非伪装成空缓存。"""
    res = _get_domains(client, admin_headers, RuntimeError("[{'message': 'boom'}]"))
    assert res.status_code == 502, res.text


def test_transport_error_falls_back_to_cache_200(client, admin_headers):
    """DataHub 真不可达(ConnectError)：仍降级本地缓存，端点保持 200。"""
    res = _get_domains(client, admin_headers, httpx.ConnectError("connection refused"))
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)
