"""Phase 0/1 Doris architecture invariants."""

from __future__ import annotations

from app.agents.validation import validate_spec
from app.models import DataSource, DorisWarehouseConfig
from app.services.data_app import DataAppService, resolve_domain_data_source
from app.warehouse import DEFAULT_ENGINE
from app.warehouse.policy import ALLOWED_EXECUTION_ENGINES, require_doris
import pytest


@pytest.fixture(autouse=True)
def _cleanup_doris_sources():
    from app.database import SessionLocal
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()


def test_agent_prompt_is_short_and_execution_boundaries_are_structural():
    """提示词只说明目标；Doris/ODS 边界由工具 schema 与执行代码承担。"""
    from app.services.chat_bi import ChatBiService
    from app.services.chat_bi_tool_schemas import (
        _AGENT_SYSTEM_PROMPT,
        _PIPELINE_KINDS,
        _TOOL_BY_NAME,
        _tools_for_skill,
    )

    assert "数据查询使用默认 Doris" in _AGENT_SYSTEM_PROMPT
    assert len(_AGENT_SYSTEM_PROMPT) < 300
    run_sql = _TOOL_BY_NAME["run_sql"]["function"]["parameters"]["properties"]
    assert "target" not in run_sql
    assert _PIPELINE_KINDS == ("materialize", "sync", "transform", "metric")
    compact = ChatBiService._compact_tools_for_prompt_retry(_tools_for_skill(None))
    assert all("description" not in tool["function"] for tool in compact)
    assert "description" not in str(compact)


def test_prompt_flag_detection_is_narrow():
    from app.services.chat_bi import ChatBiService

    assert ChatBiService._is_prompt_flag_error(
        ValueError("Invalid prompt: your prompt was flagged as potentially violating our usage policy")
    )
    assert not ChatBiService._is_prompt_flag_error(ValueError("401 unauthorized"))
    assert not ChatBiService._is_prompt_flag_error(ValueError("context length exceeded"))


def test_doris_is_the_new_default_engine():
    assert DEFAULT_ENGINE == "doris"
    assert ALLOWED_EXECUTION_ENGINES["materialize"] == frozenset({"doris"})
    assert ALLOWED_EXECUTION_ENGINES["transform"] == frozenset({"doris"})
    assert ALLOWED_EXECUTION_ENGINES["metric"] == frozenset({"doris"})
    assert require_doris("DORIS") == "doris"


def test_explicit_warehouse_rejects_non_doris_and_duplicate_default(db):
    svc = DataAppService()
    first = svc.create_data_source(
        db,
        name="Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    second = svc.create_data_source(
        db,
        name="Doris 2",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@fe2:9030",
    )
    db.refresh(first)
    assert first.is_default_warehouse is False
    assert second.is_default_warehouse is True

    try:
        svc.create_data_source(
            db,
            name="Hive",
            kind="hive",
            purpose="warehouse",
            dsn_secret_ref="hive://fe:10000",
        )
    except ValueError as exc:
        assert "Doris" in str(exc)
    else:
        raise AssertionError("non-Doris warehouse should be rejected")


def test_query_resolver_is_explicit_and_fail_closed(db):
    svc = DataAppService()
    source = svc.create_data_source(
        db,
        name="Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    business = DataSource(
        name="ERP",
        kind="mysql",
        purpose="business_source",
        catalog_name="erp",
        dsn_secret_ref="mysql+pymysql://erp@db:3306/erp",
    )
    db.add(business)
    db.commit()

    assert resolve_domain_data_source(db) is source
    assert resolve_domain_data_source(db, target_catalog="erp") is None

    source.enabled = False
    db.commit()
    assert resolve_domain_data_source(db) is None


def test_doris_config_api_masks_reader_secret_and_sets_stable_conn_ids(
    client, admin_headers, db
):
    svc = DataAppService()
    ds = svc.create_data_source(
        db,
        name="Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader:secret@fe:9030",
    )
    response = client.put(
        "/api/doris-warehouse",
        headers=admin_headers,
        json={
            "warehouse_datasource_id": ds.id,
            "enabled": True,
            "query_host": "fe",
            "query_port": 9030,
            "fenodes": ["fe:8030"],
            "reader_dsn_secret_ref": "secret://reader-dsn",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "reader_dsn_secret_ref" not in body
    assert body["reader_dsn_set"] is True
    token = "".join(c for c in ds.id if c.isalnum())[:12]
    assert body["airflow_ddl_conn_id"] == f"ontometa_doris_{token}_ddl"
    assert body["airflow_flink_conn_id"] == f"ontometa_doris_{token}_flink"


def test_doris_config_edit_keeps_masked_reader_password(db):
    svc = DataAppService()
    ds = svc.create_data_source(
        db,
        name="Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader:secret@old-fe:9030/dw",
    )
    svc.save_doris_config(
        db,
        {
            "warehouse_datasource_id": ds.id,
            "enabled": True,
            "query_host": "old-fe",
            "query_port": 9030,
            "fenodes": ["old-fe:8030"],
            "reader_dsn_secret_ref": "mysql+pymysql://reader:secret@old-fe:9030/dw",
        },
    )

    svc.save_doris_config(
        db,
        {
            "warehouse_datasource_id": ds.id,
            "enabled": True,
            "query_host": "new-fe",
            "query_port": 9030,
            "fenodes": ["new-fe:8030"],
            # 设置页密码不回显，因此编辑时提交的 DSN 没有密码。
            "reader_dsn_secret_ref": "mysql+pymysql://reader@new-fe:9030/dw",
        },
    )

    saved = db.query(DorisWarehouseConfig).first()
    assert saved is not None
    assert saved.reader_dsn_secret_ref == "mysql+pymysql://reader:secret@new-fe:9030/dw"


def test_validation_gate_rejects_non_doris_when_default_is_configured(db):
    svc = DataAppService()
    svc.create_data_source(
        db,
        name="Doris",
        kind="doris",
        purpose="warehouse",
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    issues = validate_spec(
        db,
        kind="transform",
        spec={"ontology_id": "o", "target_table": "customer", "engine": "hive"},
        ontology_id=None,
    )
    assert any(issue.code == "engine_forbidden" for issue in issues)
