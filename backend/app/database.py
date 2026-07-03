from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
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
    from sqlalchemy import inspect, text

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
    if "structure_type" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE relation_types ADD COLUMN structure_type VARCHAR(50)")
            )
    if "mapping_object_type_id" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE relation_types ADD COLUMN mapping_object_type_id "
                    "VARCHAR(36) REFERENCES object_types(id)"
                )
            )

    with SessionLocal() as db:
        from app.models import RelationType

        for rel in db.query(RelationType).filter(RelationType.structure_type.is_(None)).all():
            rel.structure_type = infer_relation_structure_type(rel.description, rel.source_evidence)
        db.commit()
