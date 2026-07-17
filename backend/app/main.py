import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.external_routes import router as external_router
from app.api.external_routes import v1_router as external_v1_router
from app.api.routes import router
from app.auth import AdminAuthMiddleware
from app.config import settings
from app.database import init_db

logger = logging.getLogger("ontometa")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if not (settings.ontometa_admin_token or "").strip():
        logger.warning(
            "ONTOMETA_ADMIN_TOKEN 未配置：管理 API（/api/* 除 v1/mcp）将返回 503。"
            "请在 backend/.env 中设置后重启。"
        )
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# 先加 CORS，再加鉴权：鉴权中间件在内层，CORS 能正确处理预检与响应头
app.add_middleware(AdminAuthMiddleware)
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
app.include_router(external_router, prefix="/api")
app.include_router(external_v1_router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}
