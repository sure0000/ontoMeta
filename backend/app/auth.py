"""管理端鉴权：共享 Admin Token（Batch B1）。

请求头（二选一）：
  - X-Admin-Token: <token>
  - Authorization: Bearer <token>

环境变量：ONTOMETA_ADMIN_TOKEN
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import settings

logger = logging.getLogger("ontometa.auth")

# 管理鉴权豁免：健康检查、公开分享看板
# /ready 与 /health 同为探针：负载均衡与编排系统不会带管理令牌，鉴权必须豁免。
_ADMIN_EXEMPT_EXACT = frozenset({"/health", "/ready"})
_ADMIN_EXEMPT_PREFIXES = ("/api/public",)


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


# 仓库里公开出现过的引导期令牌。它们在 docker-compose / service.sh / 冒烟脚本里都是
# 默认值，等于「写在 README 上的密码」——谁 clone 了仓库谁就知道。
_PUBLISHED_ADMIN_TOKENS = frozenset({"dev-admin-token-change-me", "changeme", "admin", "token"})
# 生产令牌的长度下限。ONTOMETA_ADMIN_TOKEN 是 superuser 凭据（等价 publisher，且不查库），
# 短到能被猜/爆破就没有意义。部署时注入的随机串一定过线，只有手敲的开发值过不了。
_MIN_ADMIN_TOKEN_LENGTH = 16


def check_bootstrap_secrets() -> list[str]:
    """检查引导期凭据的强度，返回问题清单（空 = 无问题）。

    只做判定、不决定后果——由调用方按 debug 决定是拒绝启动还是告警（见
    ``app.main`` 的 lifespan）。分开是为了让测试能直接断言判据，不必去起进程。
    """
    problems: list[str] = []
    token = (settings.ontometa_admin_token or "").strip()
    if not token:
        return problems  # 未配置是另一条既有路径：/api 直接 503，不在这里重复报
    if token.lower() in _PUBLISHED_ADMIN_TOKENS:
        problems.append(
            "ONTOMETA_ADMIN_TOKEN 仍是仓库里公开的引导期默认值"
            f"（{token[:4]}…），任何拿到本仓库的人都知道它"
        )
    elif len(token) < _MIN_ADMIN_TOKEN_LENGTH:
        problems.append(
            f"ONTOMETA_ADMIN_TOKEN 只有 {len(token)} 个字符，"
            f"低于 {_MIN_ADMIN_TOKEN_LENGTH} 的下限；它是 superuser 凭据，须足够随机"
        )
    return problems


def enforce_bootstrap_secrets() -> None:
    """生产（debug 关）下凭据不合格即拒绝启动；开发下只告警。

    **为什么是拒绝而不是告警**：弱令牌的后果是整套管理 API 对外敞开，而告警会淹没在
    启动日志里没人看。开发不受影响——本地要么设 DEBUG=true，要么换个够长的令牌。
    """
    problems = check_bootstrap_secrets()
    if not problems:
        return
    detail = "；".join(problems)
    if settings.debug:
        logger.warning("引导期凭据不安全（debug 模式下仅告警）：%s", detail)
        return
    raise RuntimeError(
        f"拒绝以不安全的引导期凭据启动：{detail}。"
        "请在部署时注入随机令牌（例如 `openssl rand -base64 32`）后重启；"
        "本地开发可设 DEBUG=true 跳过本检查。"
    )


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
    # Phase 6 evidence contains production topology, approvals and cut-over controls.
    (r".*", r"^/api/warehouse/migrations(/|$)", "publisher"),
    # 发布类：本体发布、预发布、数据应用发布与公开分享
    (r"^(POST|PATCH|PUT|DELETE)$", r"/(pre-)?publish(/|$)", "publisher"),
    (r"^(POST|DELETE)$", r"/share(/|$)", "publisher"),
    # 回写类：向外部 DataHub 写入，影响全域元数据。
    (r"^POST$", r"/datahub/writeback(/|$)", "publisher"),
    # 执行类：直接打物理数据源
    (r"^POST$", r"/execute(/|$)", "publisher"),
    # 物化类：一键落库，直接对目标数据源建表/写数
    (r"^POST$", r"/warehouse/materialize(/|$)", "publisher"),
    # 设置类：改 LLM/DataHub/Airflow 连接与凭据
    (r"^(POST|PATCH|PUT|DELETE)$", r"^/api/settings(/|$)", "publisher"),
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


# ``last_used_at`` 的记录精度。低于这个间隔的重复使用不再写库。
#
# 改前是**每个带主体令牌的请求都 UPDATE + COMMIT 一次**——鉴权在中间件里，也就是说
# 每一次 API 调用（含所有 GET）都要在热路径上多一次写事务。这个字段的用途只是设置页
# 里那句「最近使用：X 分钟前」，秒级精度没有任何人会看，却让每个读请求都带上写代价，
# 在 Postgres 上还会给同一行反复制造死元组。60 秒的记录精度对这个用途绰绰有余。
_LAST_USED_RESOLUTION = timedelta(seconds=60)


def _touch_last_used(db, principal) -> None:
    """按 ``_LAST_USED_RESOLUTION`` 精度更新主体的最近使用时间。"""
    now = datetime.now(UTC)
    last = principal.last_used_at
    if last is not None:
        # 列是不带时区的 DateTime：读回来是 naive，直接与 aware 的 now 相减会 TypeError。
        # 写入的一直是 UTC，所以按 UTC 补上时区再比。
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last < _LAST_USED_RESOLUTION:
            return
    principal.last_used_at = now
    db.commit()


def resolve_principal_token(token: str | None) -> tuple[str | None, str | None]:
    """裸 Token → ``(role, principal_id | None)``。传输无关，供 HTTP 与 MCP stdio 共用。

    ``ONTOMETA_ADMIN_TOKEN`` 为 superuser，等价 publisher，且不查库——保证未配置
    principals 时行为与改造前完全一致。Token 缺失/不匹配任何主体返回 ``(None, None)``。

    这里是**唯一**的 Token→角色判定处：MCP 侧不能另抄一份哈希/比对逻辑，否则两条
    入口的鉴权语义迟早分叉（宽的那条就是实际边界）。
    """
    from app.database import SessionLocal
    from app.models.principal import Principal

    token = (token or "").strip()
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
        role = principal.role
        principal_id = principal.id
        _touch_last_used(db, principal)
        return role, principal_id


def resolve_principal(request: Request):
    """按请求头解析主体。

    返回 ``(role, principal_or_None)``；``ONTOMETA_ADMIN_TOKEN`` 为 superuser，
    等价 publisher，且不查库——保证未配置 principals 时行为与改造前完全一致。
    解析失败返回 ``(None, None)``。
    """
    return resolve_principal_token(extract_admin_token(request))


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
        # role_satisfies 对 None 一定返回 False，走到这里必然有值；显式收窄让类型可核。
        return str(role)

    return _dep


def principal_label(db, request: Request) -> str | None:
    """请求主体的人话标识：优先 ``Principal.name``，退回 principal_id。

    存在的理由是**口径统一**：制品的 ``created_by`` / ``confirmed_by`` 由 REST 与 MCP
    两个入口分别写，MCP 侧写的是 ``auth.principal_name or auth.principal_id``。这里不
    对齐，同一个字段在库里就有两种形状，"这条任务是谁建的"要分情况解释——制品既然是
    唯一的记录，就不能留这种分叉。

    共享 Admin Token（未配置 principals）解析不出主体，返回 None。
    """
    principal_id = getattr(request.state, "principal_id", None)
    if not principal_id:
        return None
    try:
        from app.models.principal import Principal

        row = db.query(Principal).filter(Principal.id == principal_id).first()
        return (row.name if row and row.name else None) or principal_id
    except Exception:  # noqa: BLE001 — 取不到名字退回 id，绝不因留痕炸掉正常请求
        return principal_id
