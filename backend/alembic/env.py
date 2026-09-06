"""Alembic environment：使用应用 Settings 与 SQLAlchemy metadata。"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401  — 注册全部表到 metadata
from app.config import settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    # ⚠ disable_existing_loggers 必须显式关掉（默认是 True）。
    #
    # 迁移不只在 `alembic` 命令行里跑——``init_db()`` 在**每个 API 进程启动时**都会跑一遍。
    # 而 fileConfig 的默认行为是把「调用时已存在、且不在这份 ini 里」的 logger 全部
    # ``disabled = True``：也就是 ontometa / ontometa.database / ontometa.auth /
    # ontometa.data_app.executor …… 在启动迁移跑完的那一刻集体失声。
    #
    # 后果不是少几行日志，而是**整个应用的日志在生产里根本不输出**，包括
    # main.py 里那句 `logger.exception("Unhandled server error")`——500 现场无迹可寻。
    # 由 tests/test_p0_production_hardening.py 的 caplog 断言在全量跑时暴露。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# 覆盖 ini 中的 sqlalchemy.url，与运行时 DATABASE_URL 一致
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite") if url else False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = settings.database_url
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=settings.database_url.startswith("sqlite"),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
