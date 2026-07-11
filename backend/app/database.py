from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

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


def init_db() -> None:
    from sqlalchemy import inspect

    from app import models  # noqa: F401
    from app.services.relation_structure import infer_relation_structure_type
    from app.services.settings_service import SettingsService

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        SettingsService().ensure_defaults(db)

    inspector = inspect(engine)
    if "relation_types" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("relation_types")}
    bl_columns: set[str] = set()
    if "business_logics" in inspector.get_table_names():
        bl_columns = {column["name"] for column in inspector.get_columns("business_logics")}

    # 将所有需要的 ALTER 合并到同一个事务中执行，减少连接/提交开销。
    alter_statements: list[str] = []
    if "structure_type" not in columns:
        alter_statements.append(
            "ALTER TABLE relation_types ADD COLUMN structure_type VARCHAR(50)"
        )
    if "mapping_object_type_id" not in columns:
        alter_statements.append(
            "ALTER TABLE relation_types ADD COLUMN mapping_object_type_id "
            "VARCHAR(36) REFERENCES object_types(id)"
        )
    if "expression_draft" not in bl_columns:
        alter_statements.append(
            "ALTER TABLE business_logics ADD COLUMN expression_draft TEXT"
        )
    if "expression_json" not in bl_columns:
        alter_statements.append(
            "ALTER TABLE business_logics ADD COLUMN expression_json TEXT"
        )
    chatbi_conv_columns: set[str] = set()
    if "chat_bi_conversations" in inspector.get_table_names():
        chatbi_conv_columns = {c["name"] for c in inspector.get_columns("chat_bi_conversations")}
    if "category" not in chatbi_conv_columns:
        alter_statements.append(
            "ALTER TABLE chat_bi_conversations ADD COLUMN category VARCHAR(100)"
        )
    if "is_pinned" not in chatbi_conv_columns:
        alter_statements.append(
            "ALTER TABLE chat_bi_conversations ADD COLUMN is_pinned BOOLEAN DEFAULT 0"
        )
    if "is_archived" not in chatbi_conv_columns:
        alter_statements.append(
            "ALTER TABLE chat_bi_conversations ADD COLUMN is_archived BOOLEAN DEFAULT 0"
        )
    if "category_id" not in bl_columns:
        alter_statements.append(
            "ALTER TABLE business_logics ADD COLUMN category_id VARCHAR(36) "
            "REFERENCES business_logic_categories(id) ON DELETE SET NULL"
        )
    if alter_statements:
        with engine.begin() as conn:
            for stmt in alter_statements:
                conn.execute(text(stmt))

    _ensure_secondary_indexes(inspector)

    with SessionLocal() as db:
        from app.models import RelationType

        for rel in db.query(RelationType).filter(RelationType.structure_type.is_(None)).all():
            rel.structure_type = infer_relation_structure_type(rel.description, rel.source_evidence)
        db.commit()


# 表名 -> 列名，对应 models 中新增的 index=True 列。
# SQLite/SQLAlchemy 在 create_all 时会为新表自动建索引，
# 但对历史已存在的表不会补建，这里显式 CREATE INDEX IF NOT EXISTS。
_SECONDARY_INDEXES: dict[str, list[str]] = {
    "ontologies": ["domain_context_id", "status"],
    "object_types": ["ontology_id", "source_ref", "status"],
    "properties": ["object_type_id", "status"],
    "relation_types": [
        "ontology_id",
        "source_object_type_id",
        "target_object_type_id",
        "mapping_object_type_id",
        "status",
    ],
    "business_logics": ["ontology_id", "status", "category_id"],
    "business_logic_categories": ["name"],
    "draft_evidences": ["ontology_id", "source_ref"],
    "change_confirmations": ["ontology_id", "target_id", "confirmation_status"],
    "version_records": ["entity_id", "created_at"],
    "entity_change_logs": ["entity_id", "created_at"],
    "draft_generation_tasks": ["domain_context_id", "ontology_id", "status"],
    "chat_bi_conversations": ["domain_id", "category"],
    "chat_bi_messages": ["conversation_id"],
}


def _ensure_secondary_indexes(inspector) -> None:
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _SECONDARY_INDEXES.items():
            if table not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            for col in columns:
                if col not in existing_columns:
                    continue
                idx_name = f"ix_{table}_{col}"
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} "
                        f"ON {table} ({col})"
                    )
                )
