"""Doris-only query tools: source catalogs are not exposed to the Agent."""

from __future__ import annotations

from app.models import DataSource
from app.services.chat_bi import ChatBiService
from app.services.chat_bi_tool_schemas import _TOOL_BY_NAME
from app.services.data_app import resolve_domain_data_source
from app.database import SessionLocal
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


def test_list_catalogs_is_not_agent_tool():
    assert "list_catalogs" not in _TOOL_BY_NAME


def test_run_sql_target_is_not_in_schema():
    run_sql = _TOOL_BY_NAME["run_sql"]
    assert "target" not in run_sql["function"]["parameters"].get("properties", {})


def test_default_doris_is_the_only_query_target(db):
    doris = DataSource(
        name="生产 Doris", kind="doris", purpose="warehouse",
        is_default_warehouse=True, enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030", status="ok",
    )
    source = DataSource(
        name="ERP", kind="mysql", purpose="business_source",
        catalog_name="erp", dsn_secret_ref="mysql+pymysql://erp@db:3306", status="ok",
    )
    db.add_all([doris, source])
    db.commit()
    assert resolve_domain_data_source(db) is doris


def test_multiple_or_missing_default_doris_fails_closed(db):
    source = DataSource(
        name="ERP", kind="mysql", purpose="business_source",
        dsn_secret_ref="mysql+pymysql://erp@db:3306", status="ok",
    )
    db.add(source)
    db.commit()
    assert resolve_domain_data_source(db) is None
