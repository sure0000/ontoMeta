"""DataHub 拨测：必须打真实 GraphQL 端点并校验响应是 GMS，而非只看状态码。

回归钉子：gms_url 误填成前端 SPA 端口时，任意 GET 路径都被前端路由兜底成
200 + HTML，旧拨测只看状态码会给"假绿灯"。新拨测打 POST /api/graphql 并要求
GraphQL JSON，故 SPA HTML / 404 / 鉴权失败都应判为失败。
"""

from __future__ import annotations

import httpx
import pytest

from app.services import dependency_service as ds

# 前端 SPA 对任意路径兜底返回的首页 HTML（正是假绿灯的来源）。
_SPA_HTML = '<!DOCTYPE html><html lang="en"><head><base href="/" /></head></html>'


def _patch_transport(monkeypatch, handler):
    """把 make_http_client 换成走 MockTransport 的 httpx.Client。"""
    monkeypatch.setattr(
        ds,
        "make_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def test_probe_rejects_frontend_spa_html_200(monkeypatch):
    """误填前端端口：200 + HTML 首页，必须判失败（不再是假绿灯）。"""

    def handler(_req):
        return httpx.Response(200, text=_SPA_HTML, headers={"content-type": "text/html"})

    _patch_transport(monkeypatch, handler)
    result = ds._probe_datahub({"gms_url": "http://h:9002/gms", "token": "t"}, {})
    assert result.ok is False
    assert "SPA" in result.message or "JSON" in result.message


def test_probe_rejects_404(monkeypatch):
    """错路径 404：判失败并提示端口。"""

    def handler(_req):
        return httpx.Response(404, json={"error": "Not Found", "status": 404})

    _patch_transport(monkeypatch, handler)
    result = ds._probe_datahub({"gms_url": "http://h:9002/gms", "token": "t"}, {})
    assert result.ok is False
    assert "404" in result.message


def test_probe_rejects_unauthorized(monkeypatch):
    """token 无权限：401 判失败并点名鉴权。"""

    def handler(_req):
        return httpx.Response(401, text="Unauthorized")

    _patch_transport(monkeypatch, handler)
    result = ds._probe_datahub({"gms_url": "http://h:8080", "token": "bad"}, {})
    assert result.ok is False
    assert "鉴权" in result.message


def test_probe_rejects_graphql_errors(monkeypatch):
    """200 但含 GraphQL errors：判失败。"""

    def handler(_req):
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    _patch_transport(monkeypatch, handler)
    result = ds._probe_datahub({"gms_url": "http://h:8080", "token": "t"}, {})
    assert result.ok is False
    assert "GraphQL" in result.message


def test_probe_accepts_real_gms(monkeypatch):
    """真 GMS：POST /api/graphql 返回 data，判成功；且确实打到 graphql 端点。"""
    seen: dict[str, str] = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["method"] = req.method
        return httpx.Response(200, json={"data": {"listDomains": {"total": 2}}})

    _patch_transport(monkeypatch, handler)
    result = ds._probe_datahub({"gms_url": "http://h:8080", "token": "t"}, {})
    assert result.ok is True
    assert result.message == "连接成功"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/graphql")


def test_probe_missing_gms_url():
    result = ds._probe_datahub({"gms_url": "", "token": "t"}, {})
    assert result.ok is False
    assert "gms_url" in result.message


def test_datahub_registered_probe_is_the_hardened_one():
    """_PROBES['datahub'] 就是这个加固后的函数，别再退回只 GET /config 的 lambda。"""
    assert ds._PROBES[("datahub", "default")] is ds._probe_datahub
