"""管理端鉴权：共享 Admin Token（Batch B1）。

请求头（二选一）：
  - X-Admin-Token: <token>
  - Authorization: Bearer <token>

环境变量：ONTOMETA_ADMIN_TOKEN
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import settings

# 管理鉴权豁免：健康检查、对外 v1、MCP（已有 External App API Key）、公开分享看板
_ADMIN_EXEMPT_EXACT = frozenset({"/health"})
_ADMIN_EXEMPT_PREFIXES = ("/api/v1", "/api/mcp", "/api/public")


def is_admin_auth_exempt(path: str) -> bool:
    if path in _ADMIN_EXEMPT_EXACT:
        return True
    if not path.startswith("/api"):
        return True
    for prefix in _ADMIN_EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def extract_admin_token(request: Request) -> str | None:
    header = request.headers.get("x-admin-token")
    if header and header.strip():
        return header.strip()
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


def verify_admin_token(provided: str | None) -> None:
    """校验管理 Token；失败抛出 HTTPException。"""
    expected = (settings.ontometa_admin_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="未配置 ONTOMETA_ADMIN_TOKEN，管理 API 不可用。请在 backend/.env 中设置后重启。",
        )
    if not provided:
        raise HTTPException(
            status_code=401,
            detail="缺少管理鉴权：请在请求头传入 X-Admin-Token 或 Authorization: Bearer <token>",
        )
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="管理 Token 无效")


def hash_api_key(raw_key: str, pepper: str | None = None) -> str:
    material = f"{pepper or ''}{raw_key}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def api_key_prefix(raw_key: str, length: int = 12) -> str:
    return raw_key[:length] if raw_key else ""


def generate_dev_admin_token() -> str:
    """仅用于文档示例，勿在生产使用固定值。"""
    return f"om_admin_{secrets.token_urlsafe(24)}"


class AdminAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if is_admin_auth_exempt(path):
            return await call_next(request)

        # 第一层：Token 有效性。未配置 principals 时行为与 RBAC 上线前一致。
        role, principal_id = resolve_principal(request)
        if role is None:
            try:
                verify_admin_token(extract_admin_token(request))
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code, content={"detail": exc.detail}
                )
            # Token 校验通过但未解析出角色 → 视为 superuser（共享 Admin Token）
            role = "publisher"

        # 第二层：角色是否满足该端点要求。
        from app.models.principal import role_satisfies

        minimum = required_role(request.method, path)
        if not role_satisfies(role, minimum):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"权限不足：该操作需要 {minimum} 角色，当前为 {role}"
                },
            )

        request.state.principal_role = role
        request.state.principal_id = principal_id
        return await call_next(request)


# ---------------------------------------------------------------- RBAC（M0）

# 端点 → 最低角色。**集中式强制**：逐个端点挂依赖漏一个就是静默失守，
# 而这里默认按方法兜底，新增端点自动被覆盖（fail-closed）。
#
# 匹配顺序敏感：先精确路径覆盖，再按 HTTP 方法兜底。
_ROLE_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    # (方法正则, 路径正则, 最低角色)
    # 主体自管理必须最高权限，否则低权角色可自我提权。
    (r".*", r"^/api/principals(/|$)", "publisher"),
    # 写侧智能体：会改集群、建表、执行 SQL；制品本身也含拓扑与 SQL，读亦敏感。
    (r".*", r"^/api/agents(/|$)", "publisher"),
    # 发布类：本体发布、预发布、数据应用发布与公开分享
    (r"^(POST|PATCH|PUT|DELETE)$", r"/(pre-)?publish(/|$)", "publisher"),
    (r"^(POST|DELETE)$", r"/share(/|$)", "publisher"),
    # 回写类：向外部 DataHub 写入，影响全域元数据。
    (r"^POST$", r"/datahub/writeback(/|$)", "publisher"),
    # 执行类：直接打物理数据源
    (r"^POST$", r"/execute(/|$)", "publisher"),
    # 物化类：一键落库，直接对目标数据源建表/写数
    (r"^POST$", r"/warehouse/materialize(/|$)", "publisher"),
    # 设置类：改 LLM/DataHub/Cube 连接与凭据
    (r"^(POST|PATCH|PUT|DELETE)$", r"^/api/settings(/|$)", "publisher"),
    (r"^(POST|PATCH|PUT|DELETE)$", r"^/api/(llm-services|datahub|cube)", "publisher"),
    # 复核类：二次确认与冲突裁决
    (r".*", r"^/api/confirmations(/|$)", "reviewer"),
    (r"^(POST|PATCH|PUT)$", r"/conflicts?(/|$)", "reviewer"),
    (r"^(POST|PATCH)$", r"/(resolve|resolve-all)(/|$)", "reviewer"),
    (r"^PATCH$", r"^/api/fields/pin(/|$)", "reviewer"),
)

# 方法兜底。DELETE 归入 publisher——删除不可逆。
_METHOD_DEFAULTS: dict[str, str] = {
    "GET": "reader",
    "HEAD": "reader",
    "OPTIONS": "reader",
    "POST": "editor",
    "PUT": "editor",
    "PATCH": "editor",
    "DELETE": "publisher",
}


def required_role(method: str, path: str) -> str:
    """该请求所需的最低角色。未知方法按最高权限处理（fail-closed）。"""
    for method_re, path_re, role in _ROLE_OVERRIDES:
        if re.match(method_re, method, re.IGNORECASE) and re.search(path_re, path):
            return role
    return _METHOD_DEFAULTS.get(method.upper(), "publisher")


def resolve_principal(request: Request):
    """按请求头解析主体。

    返回 ``(role, principal_or_None)``；``ONTOMETA_ADMIN_TOKEN`` 为 superuser，
    等价 publisher，且不查库——保证未配置 principals 时行为与改造前完全一致。
    解析失败返回 ``(None, None)``。
    """
    from app.database import SessionLocal
    from app.models.principal import Principal

    token = extract_admin_token(request)
    if not token:
        return None, None

    expected = (settings.ontometa_admin_token or "").strip()
    if expected and hmac.compare_digest(token, expected):
        return "publisher", None

    token_hash = hash_api_key(token, settings.api_key_hash_pepper)
    with SessionLocal() as db:
        principal = (
            db.query(Principal)
            .filter(Principal.token_hash == token_hash, Principal.active.is_(True))
            .first()
        )
        if principal is None:
            return None, None
        principal.last_used_at = datetime.now(timezone.utc)
        role = principal.role
        db.commit()
        return role, principal.id


def require_role(minimum: str):
    """FastAPI 依赖：要求当前主体至少具备 ``minimum`` 角色。

    中间件已对全部 /api 路径做集中强制；本依赖供新端点在需要**更严格于
    路径策略**时额外加固，或用于在处理函数内拿到当前角色。
    """
    from fastapi import Depends  # noqa: F401  （保持与其它依赖一致的导入位置）

    def _dep(request: Request) -> str:
        from app.models.principal import role_satisfies

        role = getattr(request.state, "principal_role", None)
        if not role_satisfies(role, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：该操作需要 {minimum} 角色，当前为 {role or '未知'}",
            )
        return role

    return _dep
