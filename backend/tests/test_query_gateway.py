"""Doris-only query routing invariants."""

from __future__ import annotations

from app.models import DataSource
from app.database import SessionLocal
from app.services.data_app import resolve_domain_data_source
import pytest


@pytest.fixture(autouse=True)
def _cleanup_sources():
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()


def test_explicit_default_doris_is_selected(db):
    doris = DataSource(
        name="Doris", kind="doris", purpose="warehouse",
        is_default_warehouse=True, dsn_secret_ref="mysql+pymysql://reader@fe:9030",
        status="ok",
    )
    business = DataSource(
        name="ERP", kind="mysql", purpose="business_source",
        catalog_name="erp", dsn_secret_ref="mysql+pymysql://erp@db:3306",
        status="ok",
    )
    db.add_all([doris, business])
    db.commit()
    assert resolve_domain_data_source(db) is doris
    assert resolve_domain_data_source(db, target_catalog="erp") is None


def test_no_default_doris_fails_closed(db):
    db.add(DataSource(name="ERP", kind="mysql", purpose="business_source", dsn_secret_ref="mysql://x"))
    db.commit()
    assert resolve_domain_data_source(db) is None


def test_disabled_default_doris_fails_closed(db):
    db.add(DataSource(
        name="Doris", kind="doris", purpose="warehouse", is_default_warehouse=True,
        enabled=False, dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    ))
    db.commit()
    assert resolve_domain_data_source(db) is None
