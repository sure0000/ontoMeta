"""上线阻断项（P0）的回归钉子。

这五条来自一次全量审计，共同点是**都不在业务逻辑里**——它们是「跑起来之后」那一层：
默认开关、连接池、语句超时、限流归属、进程模型。这类问题不会让任何一条既有用例变红，
所以只能各钉一颗钉子，否则下次重构顺手改回去也没人知道。

对应关系：
  P0-1  debug 默认关 + 拒绝用公开令牌启动     app/config.py、app/auth.py
  P0-2  应用库连接池（pre_ping / 容量 / 回收）  app/database.py
  P0-3  Doris/MySQL 也要有语句超时             app/warehouse/registry.py
  P0-4  限流按调用方分桶，不是全局一个闸        app/mcp/rate_limit.py
  P0-5  容器非 root + 多 worker + 启动串行化    backend/Dockerfile、app/database.py
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------- P0-1 引导期凭据


def test_debug_defaults_off():
    """debug 默认必须是关。

    开着时全局异常处理器把 `类名: 异常文本` 原样回给调用方（app/main.py）。默认开意味着
    任何绕过 compose 的启动方式——service.sh、裸 uvicorn、MCP 子进程——都在对外泄露内部
    细节。测试进程自己设了 DEBUG=true（conftest），所以这里断言的是**类的默认值**，
    不是当前实例。
    """
    from app.config import Settings

    assert Settings.model_fields["debug"].default is False


@pytest.mark.parametrize(
    "token, expect_problem",
    [
        ("dev-admin-token-change-me", True),   # 仓库里公开的引导期默认值
        ("DEV-ADMIN-TOKEN-CHANGE-ME", True),   # 大小写不该绕过
        ("changeme", True),
        ("short", True),                       # 够随机但太短
        ("", False),                           # 未配置是另一条既有路径（/api 503）
        ("R7bK2wQz9vT4xN1mLpYc8FhJ", False),   # 部署时注入的随机串
    ],
)
def test_published_and_short_tokens_are_flagged(monkeypatch, token, expect_problem):
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "ontometa_admin_token", token)
    problems = auth.check_bootstrap_secrets()
    assert bool(problems) is expect_problem, problems


def test_weak_token_refuses_to_start_when_debug_off(monkeypatch):
    """生产（debug 关）下弱令牌必须**拒绝启动**，不是告警。

    告警会淹没在启动日志里没人看，而后果是整套管理 API 对外敞开——令牌是 superuser
    凭据（等价 publisher 且不查库）。
    """
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "ontometa_admin_token", "dev-admin-token-change-me")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(RuntimeError) as excinfo:
        auth.enforce_bootstrap_secrets()
    # 报错要给出可执行的下一步，不能只说「不安全」
    assert "ONTOMETA_ADMIN_TOKEN" in str(excinfo.value)


def test_weak_token_only_warns_in_debug(monkeypatch, caplog):
    """开发态放行：本地 compose / service.sh 用的就是那个公开令牌，不该被拦。"""
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "ontometa_admin_token", "dev-admin-token-change-me")
    monkeypatch.setattr(settings, "debug", True)

    with caplog.at_level("WARNING"):
        auth.enforce_bootstrap_secrets()  # 不抛
    assert any("引导期凭据不安全" in r.message for r in caplog.records)


def test_strong_token_passes_in_production(monkeypatch):
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "ontometa_admin_token", "R7bK2wQz9vT4xN1mLpYc8FhJ")
    monkeypatch.setattr(settings, "debug", False)
    auth.enforce_bootstrap_secrets()  # 不抛


def test_startup_enforces_secrets_before_touching_the_database():
    """凭据检查必须排在 init_db 之前：不合格就不该启动，更不该先跑一遍 schema 迁移。"""
    from app import main

    source = inspect.getsource(main.lifespan)
    assert source.index("enforce_bootstrap_secrets") < source.index("init_db()")


# ------------------------------------------------------------------ P0-2 连接池


def test_network_database_gets_a_configured_pool():
    """网络型库必须开 pre_ping 并显式给容量。

    不开 pre_ping 时，数据库重启或连接被中间设备掐断后，池里那批死连接会被照常发出去，
    表现为「重启数据库后前 N 个请求必报 OperationalError」。
    """
    from app.database import engine_options

    opts = engine_options("postgresql+psycopg://u:p@h:5432/db")
    assert opts["pool_pre_ping"] is True
    assert opts["pool_size"] > 0
    assert opts["max_overflow"] >= 0
    assert opts["pool_recycle"] > 0     # 回收长期空闲连接，躲开服务端 idle 超时
    assert opts["pool_timeout"] > 0     # 拿不到连接要超时报错，不能无限期挂住线程


def test_sqlite_keeps_default_pool():
    """SQLite 是本地文件：pre_ping / recycle 是白花开销，容量限制反而人为制造等待。"""
    from app.database import engine_options

    opts = engine_options("sqlite:///./ontometa.db")
    assert opts == {"connect_args": {"check_same_thread": False}}


#: 线程池容量存在 anyio 的 per-event-loop RunVar 里，取它必须在事件循环内——这也正是
#: 生产代码把对齐动作放进 lifespan 的原因（lifespan 与请求跑同一个循环，改了才算数）。
@pytest.mark.anyio
async def test_threadpool_is_aligned_to_pool_capacity(monkeypatch, anyio_backend):
    """线程池容量必须跟连接池容量对齐。

    同步 def 端点跑在线程池里，每个在飞的请求占一条连接。线程多于连接时，多出来的线程
    不是「排队等一会儿」，而是在 checkout 上等满 pool_timeout 然后抛 TimeoutError——
    等于把「稍慢」变成了「报错」。
    """
    import anyio.to_thread

    from app import database, main
    from app.config import settings

    monkeypatch.setattr(database, "_IS_SQLITE", False)
    monkeypatch.setattr(settings, "db_pool_size", 7)
    monkeypatch.setattr(settings, "db_max_overflow", 3)

    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    try:
        main._align_threadpool_to_db_pool()
        assert limiter.total_tokens == 10
    finally:
        limiter.total_tokens = original


@pytest.mark.anyio
async def test_sqlite_leaves_threadpool_alone(monkeypatch, anyio_backend):
    import anyio.to_thread

    from app import database, main

    monkeypatch.setattr(database, "_IS_SQLITE", True)
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    try:
        main._align_threadpool_to_db_pool()
        assert limiter.total_tokens == original
    finally:
        limiter.total_tokens = original


# ---------------------------------------------------------------- P0-3 语句超时


@pytest.mark.parametrize(
    "engine, expected",
    [
        # 各家旋钮名和**单位**都不一样，写错不会报错，只会静默失效或全量误杀。
        ("postgres", ["SET statement_timeout = 15000"]),      # 毫秒
        ("mysql", ["SET max_execution_time = 15000"]),        # 毫秒
        ("doris", ["SET query_timeout = 15"]),                # 秒
        ("starrocks", ["SET query_timeout = 15"]),            # 秒
        ("clickhouse", ["SET max_execution_time = 15"]),      # 秒（与 MySQL 同名不同单位）
    ],
)
def test_each_engine_gets_its_own_timeout_knob(engine, expected):
    from app.warehouse import session_timeout_statements

    assert session_timeout_statements(engine, 15) == expected


def test_doris_is_covered_not_just_postgres():
    """这条是 P0-3 的核心：改之前超时只在 `backend == "postgres"` 分支里。

    生产数仓是 Doris（走 MySQL 线协议），那条分支根本进不去——也就是 Data Agent 与 MCP
    代跑的 SQL 在真实数仓上**没有任何服务端超时**。
    """
    from app.warehouse import session_timeout_statements

    assert session_timeout_statements("doris", 15), "Doris 必须有语句超时"


@pytest.mark.parametrize("engine", ["hive", "kyuubi", "duckdb", "sqlite", "", None])
def test_engines_without_a_knob_return_empty(engine):
    """没有等价旋钮的引擎返回空：照常执行，不因此拒绝查询。"""
    from app.warehouse import session_timeout_statements

    assert session_timeout_statements(engine, 15) == []


def test_non_positive_timeout_sets_nothing():
    """0 秒在多数引擎里意味着「不限时」，比不设更危险——直接不发语句。"""
    from app.warehouse import session_timeout_statements

    assert session_timeout_statements("doris", 0) == []
    assert session_timeout_statements("doris", -1) == []


def test_sub_second_timeout_rounds_up_to_one_second():
    """秒制引擎上 0.4 秒会被截断成 0 = 不限时。向上取整，宁可宽 0.6 秒也不能变成无限。"""
    from app.warehouse import session_timeout_statements

    assert session_timeout_statements("doris", 0.4) == ["SET query_timeout = 1"]


def test_executor_applies_timeout_through_the_registry(monkeypatch):
    """执行器真的把超时语句发给了连接——不是只在注册表里躺着。"""
    from app.services import data_app_executor

    sent: list[str] = []

    class _FakeConn:
        def exec_driver_sql(self, sql):
            sent.append(sql)

    data_app_executor._apply_session_timeout(_FakeConn(), "doris", 15)
    assert sent == ["SET query_timeout = 15"]


def test_executor_survives_an_engine_that_rejects_the_knob(caplog):
    """设不上超时不该让查询失败（版本差异是常态），但必须留痕，不能静默。"""
    from app.services import data_app_executor

    class _RejectingConn:
        def exec_driver_sql(self, sql):
            raise RuntimeError("Unknown system variable")

    with caplog.at_level("WARNING"):
        data_app_executor._apply_session_timeout(_RejectingConn(), "mysql", 15)
    messages = [r.getMessage() for r in caplog.records]
    assert any("语句超时" in m and "不受服务端超时保护" in m for m in messages), messages


# ------------------------------------------------------------------ P0-4 限流归属


@pytest.fixture
def limits(db):
    """临时改限流配置，退出时还原。

    限流值落在**共享的测试库**里，不还原就会漏给后面的用例：把上限压到 1 之后，
    任何一个经服务器走工具的用例都会在第二次调用时被限流拒掉，症状还离题万里
    （test_mcp_auth 会报 StopIteration）。同时重置窗口，免得把计数带进/带出本用例。
    """
    from app.mcp.rate_limit import reset_rate_limit
    from app.services.settings_service import SettingsService

    service = SettingsService()
    before = service.get_mcp_runtime(db)
    original = {
        "mcp_rate_limit_per_minute": before.mcp_rate_limit_per_minute,
        "mcp_execute_sql_rate_limit_per_minute": before.mcp_execute_sql_rate_limit_per_minute,
    }

    def _set(*, per_minute: int):
        service.update_mcp_settings(
            db,
            {
                "mcp_rate_limit_per_minute": per_minute,
                "mcp_execute_sql_rate_limit_per_minute": 0,
            },
        )
        reset_rate_limit()

    try:
        yield _set
    finally:
        service.update_mcp_settings(db, original)
        reset_rate_limit()


def test_one_runaway_caller_does_not_block_everyone_else(limits):
    """P0-4 的核心：配额是每个调用方各一份。

    改之前窗口只按工具名计数，远程 HTTP 传输下多主体共进程——一个失控 agent 打满窗口
    会把其他所有主体一起拒掉，也就是拿别人的可用性替自己兜底。
    """
    from app.mcp import rate_limit as rl

    limits(per_minute=2)

    # A 把自己的额度用光
    assert rl.check_rate_limit("query_objects", principal="principal:A", now=0.0)["allowed"]
    assert rl.check_rate_limit("query_objects", principal="principal:A", now=0.1)["allowed"]
    assert not rl.check_rate_limit("query_objects", principal="principal:A", now=0.2)["allowed"]

    # B 一次都还没调过，必须照常放行
    assert rl.check_rate_limit("query_objects", principal="principal:B", now=0.3)["allowed"]
    assert rl.check_rate_limit("query_objects", principal="principal:B", now=0.4)["allowed"]


def test_quota_is_still_per_tool_within_one_caller(limits):
    """分桶键是（调用方, 工具）：同一个人打爆一个工具，不该连累他自己的其它工具。"""
    from app.mcp import rate_limit as rl

    limits(per_minute=1)

    assert rl.check_rate_limit("query_objects", principal="principal:A", now=0.0)["allowed"]
    assert not rl.check_rate_limit("query_objects", principal="principal:A", now=0.1)["allowed"]
    assert rl.check_rate_limit("list_tasks", principal="principal:A", now=0.2)["allowed"]


def test_anonymous_and_admin_are_separate_buckets():
    """匿名回落与共享 Admin Token 都是 principal_id=None，但必须分得开。

    合在一起就意味着任何匿名请求都能挤占管理员通道的额度。
    """
    from app.mcp.tools import AuthContext

    admin = AuthContext(role="publisher", principal_id=None)
    anon = AuthContext(role="reader", principal_id=None, anonymous=True)
    named = AuthContext(role="editor", principal_id="p-123")
    nobody = AuthContext(role=None, principal_id=None)

    keys = {admin.rate_limit_key, anon.rate_limit_key, named.rate_limit_key, nobody.rate_limit_key}
    assert len(keys) == 4, keys
    assert named.rate_limit_key == "principal:p-123"


def test_idle_buckets_are_evicted(limits):
    """按主体分桶后键空间不再封顶——远程传输下那是个开放集合，不回收就是内存泄漏。"""
    from app.mcp import rate_limit as rl

    limits(per_minute=1)

    for i in range(50):
        key = f"principal:{i}"
        rl.check_rate_limit("query_objects", principal=key, now=0.0)

    # 窗口期之后再来一个被拒的调用，触发回收：早已滑出窗口的 50 个桶应被清掉
    rl.check_rate_limit("query_objects", principal="principal:latest", now=1000.0)
    rl.check_rate_limit("query_objects", principal="principal:latest", now=1000.1)
    assert len(rl._limiter._calls) <= 2


def test_server_passes_caller_identity_to_the_limiter():
    """服务器必须把身份传下去——限流器支持分桶但调用点不传，等于什么都没改。"""
    from app.mcp import server

    source = inspect.getsource(server.handle_call_tool)
    assert "check_rate_limit(name, principal=auth.rate_limit_key)" in source


# ------------------------------------------------- 附带：启动迁移不许把日志掐掉


def test_startup_migrations_do_not_disable_application_loggers():
    """``alembic upgrade`` 跑完之后，应用日志必须还活着。

    ``logging.config.fileConfig`` 默认 ``disable_existing_loggers=True``——它会把调用时
    已存在、又不在 alembic.ini 里的 logger 全部禁用。而迁移不只在命令行里跑：``init_db()``
    在**每个 API 进程启动时**都跑一遍，于是 ontometa.* 全族在启动那一刻集体失声。

    后果不是少几行日志，是生产上整个应用不输出日志，包括 500 现场那句
    ``logger.exception("Unhandled server error")``。这条用例把它钉住。
    """
    import logging

    from app.database import run_migrations

    probe = logging.getLogger("ontometa.test_probe")
    assert not probe.disabled

    run_migrations()  # 测试库已在 head，是空操作——但 fileConfig 照样会跑

    assert not probe.disabled, "启动迁移把应用 logger 禁用了（见 alembic/env.py）"
    for name in ("ontometa", "ontometa.database", "ontometa.auth"):
        assert not logging.getLogger(name).disabled, name


# --------------------------------------------------------------- P0-5 进程模型


def test_container_runs_as_non_root_with_multiple_workers():
    dockerfile = (_BACKEND / "Dockerfile").read_text()
    assert "USER ontometa" in dockerfile, "容器必须以非 root 用户运行"
    assert "--workers" in dockerfile, "单 worker 只用一个 CPU 核"
    # 运行用户要写得了这三个目录，否则血缘上传与证据缓存在容器里直接失败
    for path in ("/app/data", "/app/.cache", "/app/.logs"):
        assert path in dockerfile, f"{path} 需要属于运行用户"


def test_startup_work_is_serialized():
    """多 worker 的前提：N 个进程同时 `alembic upgrade head` 会在 alembic_version 上互相踩。"""
    from app import database

    source = inspect.getsource(database.init_db)
    assert "_startup_lock()" in source
    assert source.index("_startup_lock()") < source.index("run_migrations()")


def test_startup_lock_is_transparent_on_sqlite():
    """SQLite 不加锁（单文件、单 worker），但上下文管理器本身要能正常进出。"""
    from app import database

    assert database._IS_SQLITE, "测试库应为 SQLite"
    with database._startup_lock():
        pass


def test_startup_lock_propagates_body_errors(monkeypatch):
    """拿锁失败要接住，启动逻辑自己的异常必须原样上抛——别把真故障吞成一条警告。"""
    from app import database

    monkeypatch.setattr(database, "_IS_SQLITE", False)

    class _FailingEngine:
        def connect(self):
            raise RuntimeError("advisory lock 不可用")

    monkeypatch.setattr(database, "engine", _FailingEngine())

    with pytest.raises(ValueError, match="启动逻辑自己的错"):
        with database._startup_lock():
            raise ValueError("启动逻辑自己的错")
