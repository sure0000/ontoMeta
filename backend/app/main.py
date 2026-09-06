import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import router
from app.auth import AdminAuthMiddleware, enforce_bootstrap_secrets
from app.config import settings
from app.database import init_db, max_pooled_connections
from app.observability import (
    RequestContextMiddleware,
    check_readiness,
    configure_logging,
)

configure_logging()
logger = logging.getLogger("ontometa")


def _align_threadpool_to_db_pool() -> None:
    """把 FastAPI 的线程池容量对齐到应用库连接池容量。

    同步 ``def`` 端点跑在 anyio 的线程池里（默认 40 条），每个在飞的请求占一条数据库
    连接。线程数大于池容量时，多出来的线程不会排队等一会儿就好——它们在 checkout 上
    等满 ``pool_timeout`` 然后抛 TimeoutError，也就是把「稍慢」变成了「报错」。
    对齐之后，超出容量的请求排在线程队列里等，延迟上升但不失败。

    SQLite 不限容量（返回 0），保持 anyio 默认值不动。
    """
    capacity = max_pooled_connections()
    if capacity <= 0:
        return
    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
    except Exception as exc:  # noqa: BLE001 — 对齐失败不该拦住启动，退回默认值即可
        logger.warning("无法对齐线程池容量，沿用 anyio 默认值：%s", exc)
        return
    if limiter.total_tokens != capacity:
        logger.info(
            "线程池容量 %s → %s（对齐数据库连接池 %s + %s 溢出）",
            limiter.total_tokens,
            capacity,
            settings.db_pool_size,
            settings.db_max_overflow,
        )
        limiter.total_tokens = capacity


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 凭据检查在建库之前：不合格就不该启动，更不该先把 schema 迁移跑一遍。
    enforce_bootstrap_secrets()
    _align_threadpool_to_db_pool()
    init_db()
    if not (settings.ontometa_admin_token or "").strip():
        logger.warning(
            "ONTOMETA_ADMIN_TOKEN 未配置：管理 API（/api/* 除 public）将返回 503。"
            "请在 backend/.env 中设置后重启。"
        )
    # MCP 远程 HTTP 传输（默认关闭）：session manager 的 run() 是长驻上下文，必须在
    # 主 lifespan 里驱动——mount 的子应用 lifespan 不会被 FastAPI 触发。
    # Session manager 常驻，是否对外开放由数据库运行期配置在 guard 中决定。
    # 这样设置页修改 HTTP 开关后无需重启进程即可生效。
    from app.mcp.http_app import get_session_manager
    logger.info("MCP 远程 HTTP 传输管理器已启动（数据库配置决定是否开放）")
    async with get_session_manager().run():
        yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# MCP 远程 HTTP 传输：挂到 /mcp（非 /api，AdminAuthMiddleware 自动豁免，由 MCP 自管
# 逐请求 Bearer 鉴权）。仅在显式开启时挂载——不开则这条网络面根本不存在。
from app.mcp.http_app import MCP_HTTP_PATH, build_mcp_asgi

app.mount(MCP_HTTP_PATH, build_mcp_asgi())

# ⚠ ``add_middleware`` 是**后加的在外层**，所以下面的书写顺序与执行顺序相反。
# 实际执行：CORS → 请求上下文 → 鉴权 → 路由。
#
# 请求上下文必须在**鉴权外层**：鉴权失败时中间件直接返回 401/403，根本不会往内走。
# 放内层的话，最需要关联 id 的那类响应（谁被挡了、为什么）恰好一个 id 都没有。
# CORS 仍在最外层，预检与错误响应的跨域头才加得上。
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 允许 str / dict / list（如一致性校验 issues），其它类型回退通用文案
    if isinstance(exc.detail, (str, dict, list)):
        detail = exc.detail
    else:
        detail = "请求错误"
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数校验失败", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_exc_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error: %s", exc)
    if settings.debug:
        detail = f"服务端内部错误：{exc.__class__.__name__}: {exc}"
    else:
        detail = "服务端内部错误，请稍后重试或联系管理员"
    return JSONResponse(status_code=500, content={"detail": detail})


app.include_router(router, prefix="/api")


@app.get("/health")
def health():
    """**存活**探针：进程还在就算数。

    刻意不探数据库——数据库挂了重启容器救不回来，反而会把好好的实例拖进重启循环。
    「依赖是否可用」是就绪的事，见 /ready。
    """
    return {"status": "ok", "app": settings.app_name}


@app.get("/ready")
def ready():
    """**就绪**探针：真连一次应用库。未就绪返回 503，让负载均衡摘掉这个实例。"""
    ok, detail = check_readiness()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", **detail},
    )
