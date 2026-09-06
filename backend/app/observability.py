"""平台级可观测性：请求关联 ID、结构化日志、就绪探针。

审计时这一层是空的：没有 metrics、没有 trace、没有结构化日志、没有请求关联 ID，
``/health`` 只回一个静态字典。业务层的观测其实做得不错（mcp_audit_logs、agent_telemetry、
agent_trace），缺的正是「出故障时能回答哪个请求慢、慢在哪一段」的那一层。

本模块只做**基础三件事**，不引入 Prometheus / OpenTelemetry 依赖：

1. **请求关联 ID**：每个请求一个 id，进日志、进响应头。没有它，多 worker 并发下的日志
   就是一锅粥——同一个 500 的上下游几行分散在几百行里，拼不回来。
   上游（nginx / 网关）传了 ``X-Request-ID`` 就沿用，便于跨服务串联。
2. **结构化日志**：``LOG_FORMAT=json`` 时输出 JSON 行，便于采集；缺省仍是人读的文本，
   本地开发不受影响。
3. **就绪与存活分开**：``/health`` 是**存活**探针，只证明进程还在——它不该探数据库，
   因为数据库挂了重启容器也救不回来，反而会把好好的实例杀掉重启循环。
   ``/ready`` 是**就绪**探针，真去连一次库；不就绪时返回 503，让负载均衡摘掉它。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

#: 当前请求的关联 id。contextvar 而不是线程本地：同步端点跑在线程池里，
#: 异步端点跑在事件循环上，只有 contextvar 两种都覆盖得到。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("ontometa.access")


class RequestIdFilter(logging.Filter):
    """把当前请求 id 挂到每条日志记录上。

    用 filter 而不是让每处调用点自己传：调用点有上千个，漏一个就断一条线索。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象。字段名对齐常见采集端的约定。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # 结构化字段：调用方用 logger.info("...", extra={"fields": {...}}) 带上。
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """按 ``LOG_FORMAT`` 装配根 logger。

    ⚠ 必须用 ``force=True``：``logging.basicConfig`` 在已有 handler 时**默默什么都不做**，
    而 uvicorn 会先装自己的 handler。不加这个参数，JSON 格式与 request_id 都不会生效，
    并且没有任何报错——又是一种静默失效。
    """
    fmt = (os.environ.get("LOG_FORMAT") or "text").strip().lower()
    level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
        )
    logging.basicConfig(level=level, handlers=[handler], force=True)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """给每个请求分配关联 id，记一行访问日志，并把 id 回写到响应头。

    访问日志只在**慢请求或出错时**记 WARNING，其余记 DEBUG——正常流量每条都记 INFO
    会把日志淹掉，真正要看的那几条反而找不到。
    """

    #: 超过这个毫秒数记 WARNING。挑 1000ms 是因为本仓的读路径普遍在百毫秒级，
    #: 超过一秒基本都对应「对账打了远端 Airflow」「SQL 打了数仓」这类值得看一眼的事。
    SLOW_MS = 1000

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (incoming or "").strip() or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.monotonic() - started) * 1000
            logger.exception(
                "%s %s 抛出未处理异常（%.0fms）", request.method, request.url.path, elapsed
            )
            raise
        finally:
            request_id_var.reset(token)

        elapsed = (time.monotonic() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        level = (
            logging.WARNING
            if response.status_code >= 500 or elapsed >= self.SLOW_MS
            else logging.DEBUG
        )
        logger.log(
            level,
            "%s %s → %s（%.0fms）",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            extra={
                "fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed, 1),
                }
            },
        )
        return response


def check_readiness() -> tuple[bool, dict]:
    """就绪判定：能不能真的连上应用库。

    只探应用库，不探 DataHub / Airflow / 数仓——那些不通时系统仍能提供大部分功能
    （浏览本体、看历史记录），把它们算进就绪会让一次外部抖动摘掉整个服务。
    """
    from sqlalchemy import text

    from app.database import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 —— 探针要把任何失败都翻译成「未就绪」
        return False, {"database": "unreachable", "error": str(exc)[:200]}
    return True, {"database": "ok"}


__all__ = [
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "RequestContextMiddleware",
    "RequestIdFilter",
    "check_readiness",
    "configure_logging",
    "request_id_var",
]
