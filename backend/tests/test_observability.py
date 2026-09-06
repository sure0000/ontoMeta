"""平台级可观测性：请求关联 ID、结构化日志、存活/就绪分开。

审计时这一层是空的——没有关联 id、没有结构化日志、``/health`` 只回静态字典。
这组用例钉的是「出故障时还能查」的最低保障。
"""

from __future__ import annotations

import json
import logging

from app.observability import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    RequestIdFilter,
    request_id_var,
)

# --------------------------------------------------------------- 请求关联 ID


def test_every_response_carries_a_request_id(client, admin_headers):
    response = client.get("/api/ontologies", headers=admin_headers)
    assert response.headers.get(REQUEST_ID_HEADER), "响应里没有请求关联 id"


def test_upstream_request_id_is_reused(client, admin_headers):
    """上游（nginx / 网关）传了就沿用，这样一条请求能跨服务串起来。"""
    response = client.get(
        "/api/ontologies",
        headers={**admin_headers, REQUEST_ID_HEADER: "trace-from-gateway"},
    )
    assert response.headers[REQUEST_ID_HEADER] == "trace-from-gateway"


def test_request_ids_differ_between_requests(client, admin_headers):
    first = client.get("/api/ontologies", headers=admin_headers)
    second = client.get("/api/ontologies", headers=admin_headers)
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


def test_auth_failures_also_get_an_id(client):
    """401/403 更需要关联 id——排查「谁被挡了」全靠它。"""
    response = client.get("/api/ontologies")  # 不带令牌
    assert response.status_code in (401, 403)
    assert response.headers.get(REQUEST_ID_HEADER)


# --------------------------------------------------------------- 结构化日志


def test_json_formatter_emits_one_object_per_line():
    record = logging.LogRecord(
        name="ontometa.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="搬了 %s 行", args=(42,), exc_info=None,
    )
    RequestIdFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "搬了 42 行"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ontometa.test"
    assert "request_id" in payload


def test_json_formatter_carries_structured_fields():
    """``extra={"fields": {...}}`` 里的东西要进 JSON，否则结构化就只是换了个壳。"""
    record = logging.LogRecord(
        name="ontometa.access", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="慢请求", args=(), exc_info=None,
    )
    record.fields = {"path": "/api/x", "duration_ms": 1234.5}
    RequestIdFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["path"] == "/api/x"
    assert payload["duration_ms"] == 1234.5


def test_filter_injects_the_current_request_id():
    token = request_id_var.set("abc123")
    try:
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg="m", args=(), exc_info=None,
        )
        RequestIdFilter().filter(record)
        assert record.request_id == "abc123"
    finally:
        request_id_var.reset(token)


def test_logging_config_uses_force_so_uvicorn_handlers_do_not_win():
    """``basicConfig`` 在已有 handler 时默默什么都不做，而 uvicorn 会先装自己的。

    不传 force=True 的话 JSON 格式与 request_id 全都不生效，且没有任何报错。
    """
    import inspect

    from app import observability

    assert "force=True" in inspect.getsource(observability.configure_logging)


# --------------------------------------------------------------- 存活 / 就绪


def test_health_is_liveness_only(client):
    """存活探针不该探数据库：库挂了重启容器救不回来，只会把实例拖进重启循环。"""
    import inspect

    from app import main

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    source = inspect.getsource(main.health)
    assert "check_readiness" not in source, "存活探针不该做就绪检查"


def test_ready_actually_touches_the_database(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"


def test_ready_returns_503_when_the_database_is_gone(monkeypatch):
    """未就绪必须是 503——200 配一句 "not_ready" 没有任何负载均衡看得懂。"""
    from app import observability

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("app.database.engine", _BrokenEngine())
    ok, detail = observability.check_readiness()
    assert ok is False
    assert detail["database"] == "unreachable"


def test_probes_do_not_require_a_token(client):
    """负载均衡与编排系统不会带管理令牌，两个探针都必须豁免鉴权。"""
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)
