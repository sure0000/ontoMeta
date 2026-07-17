"""数据库引擎、Session 与启动迁移。

Schema 变更一律走 Alembic（见 backend/alembic/）。
本模块仅负责：连接、跑 upgrade/stamp、以及幂等数据回填。
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger("ontometa.database")

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_wal_mode(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
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

    run_migrations()

    with SessionLocal() as db:
        SettingsService().ensure_defaults(db)

    _backfill_relation_structure_types()
    _migrate_external_api_key_hashes()
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


def _migrate_external_api_key_hashes() -> None:
    """幂等：将遗留明文 api_key 哈希回填（B1 数据迁移，可重复执行）。"""
    from app.auth import api_key_prefix, hash_api_key
    from app.models import ExternalApp

    inspector = inspect(engine)
    if "external_apps" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("external_apps")}
    if "api_key_hash" not in cols:
        return

    pepper = settings.api_key_hash_pepper
    migrated = 0
    with SessionLocal() as db:
        rows = db.query(ExternalApp).all()
        for row in rows:
            if row.api_key_hash:
                raw = (row.api_key or "").strip()
                if raw.startswith("om_sk_"):
                    row.api_key = f"hashed:{row.api_key_hash}"
                    migrated += 1
                continue
            raw = (row.api_key or "").strip()
            if not raw or raw.startswith("hashed:"):
                logger.warning(
                    "external_app %s (%s) 无可用明文 Key 且无 hash，需重新生成密钥",
                    row.id,
                    row.name,
                )
                continue
            key_hash = hash_api_key(raw, pepper)
            row.api_key_hash = key_hash
            row.api_key_prefix = api_key_prefix(raw)
            row.api_key = f"hashed:{key_hash}"
            migrated += 1
        if migrated:
            db.commit()
            logger.info("Migrated %s external app API key(s) to hash storage", migrated)
