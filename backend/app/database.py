"""数据库引擎、Session 与启动迁移。

Schema 变更一律走 Alembic（见 backend/alembic/）。
本模块仅负责：连接、跑 upgrade/stamp、以及幂等数据回填。
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger("ontometa.database")

_IS_SQLITE = settings.database_url.startswith("sqlite")


def engine_options(database_url: str) -> dict:
    """建应用库引擎要传的 ``create_engine`` 参数。

    池参数只对网络型数据库有意义：SQLite 是本地文件，连接近乎免费，pre_ping 与 recycle
    只是白花开销，容量限制反而会人为制造等待。故 SQLite 只给 check_same_thread，不设池。

    **pool_pre_ping 是这里最要紧的一项**：不开时，数据库重启、连接被中间设备掐断之后，
    池里那批已死的连接会被照常发出去，直到每一条都用一次失败一次才被剔除——表现是
    「重启数据库后前 N 个请求必报 OperationalError」。开了则借出前先探活，死连接静默重建。
    同仓库里 catalog_sync / data_app_executor 建外部引擎时都写了 pool_pre_ping=True，
    唯独承载全部业务流量的主引擎漏了。
    """
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {
        "connect_args": {},
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
    }


engine = create_engine(settings.database_url, **engine_options(settings.database_url))


def max_pooled_connections() -> int:
    """本进程最多能同时占用的应用库连接数。

    ``app.main`` 用它把 FastAPI 的线程池对齐到池容量——两者必须同数量级，理由见
    ``Settings.db_pool_size`` 的注释。SQLite 不限容量，返回 0 表示「不必对齐」。
    """
    if _IS_SQLITE:
        return 0
    return settings.db_pool_size + settings.db_max_overflow


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_wal_mode(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # 分离子进程生成（C）：API worker 与多个 draft-worker 子进程会并发写同一
        # SQLite 文件。WAL 允许并发读 + 单写，但写锁竞争仍会抛 "database is locked"；
        # busy_timeout 让写方最多等待 15s 而非立即失败。
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _alembic_config():
    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


# 启动串行化用的 Postgres advisory lock 键。任意固定 64 位整数即可，只要全应用唯一。
_STARTUP_LOCK_KEY = 7_213_090_001


@contextmanager
def _startup_lock():
    """把启动期的迁移与回填串行化。

    **为什么需要**：多 worker 部署下每个 worker 都会跑一遍 lifespan，也就是 N 个进程
    同时 ``alembic upgrade head``。它们会在 alembic_version 上互相踩：轻则重复执行 DDL
    报「表已存在」，重则两个事务各自建到一半死锁。第一个拿到锁的把库升上去，其余的等它
    做完再进来——那时 upgrade 已是空操作。

    Postgres 用会话级 advisory lock（连接断开自动释放，进程崩了不会留下死锁）。
    SQLite 是单文件、且这类部署本就是单 worker，靠既有的 busy_timeout 兜住即可，
    不额外加锁。
    """
    if _IS_SQLITE:
        yield
        return

    conn = None
    locked = False
    try:
        conn = engine.connect()
        conn.exec_driver_sql(f"SELECT pg_advisory_lock({_STARTUP_LOCK_KEY})")
        locked = True
    except Exception as exc:  # noqa: BLE001 —— 只接住「拿锁失败」，不接住启动逻辑本身
        # 后端不支持 advisory lock（非 Postgres 的 DSN）或此刻连不上。拿不到锁不该
        # 拦住启动——单 worker 部署压根不需要它——但必须留痕，否则多 worker 下的
        # 迁移竞争会变成一个查无实据的偶发故障。
        logger.warning("启动锁不可用（%s），退回不加锁启动；多 worker 请确保迁移已先行", exc)
        if conn is not None:
            conn.close()
            conn = None

    try:
        yield
    finally:
        if conn is not None:
            try:
                if locked:
                    conn.exec_driver_sql(f"SELECT pg_advisory_unlock({_STARTUP_LOCK_KEY})")
            finally:
                conn.close()


def run_migrations() -> None:
    """执行 Alembic upgrade；遗留库（有业务表但无 alembic_version）则 stamp head。

    遗留库前提：schema 已与当前模型一致（B1 之后的库满足）。
    若极旧库缺列，请先备份，再按 README「旧库升级」处理，勿直接 stamp。
    """
    from alembic import command

    cfg = _alembic_config()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "alembic_version" not in tables and "domain_contexts" in tables:
        logger.info(
            "Detected legacy DB without alembic_version; stamping to head "
            "(assumes schema already matches models)"
        )
        command.stamp(cfg, "head")
        return

    command.upgrade(cfg, "head")


def init_db() -> None:
    from app import models  # noqa: F401
    from app.services.draft_task_service import recover_stale_draft_tasks
    from app.services.settings_service import SettingsService

    # 整段放在启动锁里：迁移会互相踩（见 _startup_lock），而下面几个回填虽然各自幂等，
    # N 个 worker 同时跑也只是把同一份活做 N 遍、外加并发写同几张表。串行做一次。
    with _startup_lock():
        run_migrations()

        with SessionLocal() as db:
            SettingsService().ensure_defaults(db)

        _backfill_relation_structure_types()
        _backfill_ready_sync_projections()
        recover_stale_draft_tasks()


def _backfill_relation_structure_types() -> None:
    """幂等：补全 relation_types.structure_type 空值。"""
    inspector = inspect(engine)
    if "relation_types" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("relation_types")}
    if "structure_type" not in cols:
        return

    from app.models import RelationType
    from app.services.relation_structure import infer_relation_structure_type

    with SessionLocal() as db:
        updated = 0
        for rel in db.query(RelationType).filter(RelationType.structure_type.is_(None)).all():
            rel.structure_type = infer_relation_structure_type(
                rel.description, rel.source_evidence
            )
            updated += 1
        if updated:
            db.commit()
            logger.info("Backfilled structure_type on %s relation_types", updated)


def _backfill_ready_sync_projections() -> None:
    """Register serving mappings for syncs completed before direct-sync support.

    A verified source sync is sufficient for a source-backed object that has no
    independent transform.  Older versions only updated ``IngestionContract``
    and left the query Projection absent when materialization had not run first.
    The operation is idempotent and only changes control-plane metadata; it does
    not issue SQL to Doris or alter any physical table.
    """
    from app.models import IngestionContract
    from app.services.ingestion_contract import mirror_contract_to_projection

    with SessionLocal() as db:
        contracts = (
            db.query(IngestionContract)
            .filter(IngestionContract.status == "ready")
            .all()
        )
        updated = 0
        for contract in contracts:
            try:
                if mirror_contract_to_projection(db, contract) is not None:
                    db.commit()
                    updated += 1
            except Exception as exc:  # noqa: BLE001 - one stale contract must not block startup
                db.rollback()
                logger.warning(
                    "Could not backfill direct sync projection for contract %s: %s",
                    contract.id,
                    exc,
                )
        if updated:
            logger.info("Backfilled direct sync projections for %s ready contracts", updated)

