"""list_catalogs 工具:可查询目录清单,供 run_sql 的 target 取值。

验证:warehouse 恒在;源库 catalog 按 catalog_name 列出;mock/无连接串源不列;
无可用源时返回可读降级(不给 agent 编造 target 的空间)。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DataSource
from app.services.chat_bi import ChatBiService


@pytest.fixture(autouse=True)
def _cleanup_sources():
    yield
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()


def _seed(name: str, *, kind: str, dsn: str | None, catalog_name: str | None = None) -> None:
    with SessionLocal() as db:
        db.add(DataSource(name=name, kind=kind, dsn_secret_ref=dsn,
                          catalog_name=catalog_name))
        db.commit()


def _list_catalogs():
    with SessionLocal() as db:
        result, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_id="d", ontology_id="o", name="list_catalogs", args={},
            principal_role="publisher",
        )
    return result, summary, is_error


def test_warehouse_always_listed_when_usable():
    _seed("wh", kind="starrocks", dsn="starrocks+pymysql://u:p@fe:9030")
    result, _, is_error = _list_catalogs()
    assert is_error is False
    names = [c["name"] for c in result["catalogs"]]
    assert names == ["warehouse"]


def test_source_catalogs_listed_alongside_warehouse():
    _seed("wh", kind="starrocks", dsn="starrocks+pymysql://u:p@fe:9030")
    _seed("erp", kind="mysql", dsn="mysql+pymysql://u:p@db:3306", catalog_name="erp")
    _seed("crm", kind="postgres", dsn="postgresql://u:p@pg:5432", catalog_name="crm")
    result, summary, _ = _list_catalogs()
    names = [c["name"] for c in result["catalogs"]]
    assert names == ["warehouse", "erp", "crm"]
    assert "erp" in summary


def test_internal_marker_not_duplicated_as_catalog():
    _seed("wh", kind="starrocks", dsn="starrocks+pymysql://u:p@fe:9030")
    _seed("wh2", kind="starrocks", dsn="starrocks+pymysql://u:p@fe:9030",
          catalog_name="internal")
    result, _, _ = _list_catalogs()
    names = [c["name"] for c in result["catalogs"]]
    assert names == ["warehouse"]  # internal 与 warehouse 同义,不重复列


def test_mock_and_dsn_less_excluded():
    _seed("mock", kind="mock", dsn=None)
    _seed("nodsn", kind="postgres", dsn=None)
    result, summary, is_error = _list_catalogs()
    assert is_error is False
    assert result["catalogs"] == []
    assert "未配置可执行数据源" in result["note"]
    assert "无可执行目录" in summary


def test_empty_target_values_match_resolver():
    """list_catalogs 的名字是 run_sql target 的合法值:warehouse/erp/crm。"""
    _seed("wh", kind="starrocks", dsn="starrocks+pymysql://u:p@fe:9030")
    _seed("erp", kind="mysql", dsn="mysql+pymysql://u:p@db:3306", catalog_name="erp")
    with SessionLocal() as db:
        from app.services.data_app import resolve_domain_data_source

        svc = ChatBiService()
        assert svc._resolve_domain_data_source(db).name == "wh"
        assert svc._resolve_domain_data_source(db, target_catalog="warehouse").name == "wh"
        assert svc._resolve_domain_data_source(db, target_catalog="erp").name == "erp"
        assert svc._resolve_domain_data_source(db, target_catalog="crm") is None
