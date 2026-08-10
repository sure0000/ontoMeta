"""Catalog 同步服务:外部 JDBC catalog 的 DDL 生成(不依赖真实 StarRocks)。

验证:mysql/postgres 连接串 → 正确的 jdbc_uri 与 CREATE EXTERNAL CATALOG DDL;
仓库源(catalog_name 空/"internal")不生成;不支持的类型/脏 DSN 优雅跳过。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DataSource
from app.services.catalog_sync import (
    generate_catalog_ddl,
    is_warehouse_source,
    jdbc_uri_for,
)


def _ds(catalog_name: str | None, *, kind: str = "mysql",
        dsn: str | None = "mysql+pymysql://readonly:pw@erp-db:3306") -> DataSource:
    return DataSource(name="t", kind=kind, dsn_secret_ref=dsn, catalog_name=catalog_name)


def test_jdbc_uri_mysql():
    ds = _ds("erp")
    assert jdbc_uri_for(ds) == "jdbc:mysql://erp-db:3306"


def test_jdbc_uri_mysql_with_db():
    ds = _ds("erp", dsn="mysql+pymysql://u:p@h:3306/erp_db")
    assert jdbc_uri_for(ds) == "jdbc:mysql://h:3306/erp_db"


def test_jdbc_uri_postgres():
    ds = _ds("crm", kind="postgres", dsn="postgresql+psycopg://u:p@pg:5432/crm")
    assert jdbc_uri_for(ds) == "jdbc:postgresql://pg:5432/crm"


def test_jdbc_uri_unsupported_kind():
    ds = _ds("x", kind="sqlite", dsn="sqlite:///f.db")
    assert jdbc_uri_for(ds) is None


def test_jdbc_uri_dirty_dsn_returns_none():
    ds = _ds("erp", dsn="not-a-url:::")
    assert jdbc_uri_for(ds) is None


def test_ddl_contains_catalog_name_and_driver():
    ddl = generate_catalog_ddl(_ds("erp"))
    assert ddl is not None
    assert "CREATE EXTERNAL CATALOG erp" in ddl
    assert '"type" = "jdbc"' in ddl
    assert "com.mysql.cj.jdbc.Driver" in ddl
    assert "jdbc:mysql://erp-db:3306" in ddl


def test_ddl_postgres_driver():
    ddl = generate_catalog_ddl(_ds("crm", kind="postgres",
                                   dsn="postgresql+psycopg://u:p@pg:5432/crm"))
    assert ddl is not None
    assert "org.postgresql.Driver" in ddl


def test_warehouse_source_no_ddl():
    for marker in (None, "", "internal"):
        assert generate_catalog_ddl(_ds(marker)) is None
        assert is_warehouse_source(_ds(marker))


def test_catalog_named_internal_not_external():
    assert is_warehouse_source(_ds("internal")) is True
    assert is_warehouse_source(_ds("erp")) is False


def test_missing_password_still_generates():
    ds = _ds("erp", dsn="mysql+pymysql://readonly@erp-db:3306")
    ddl = generate_catalog_ddl(ds)
    assert ddl is not None
    assert '"password" = ""' in ddl


def test_unsupported_kind_no_ddl():
    assert generate_catalog_ddl(_ds("erp", kind="clickhouse")) is None


# ------------------------------------------------------ 编排 sync_all_catalogs


@pytest.fixture(autouse=True)
def _cleanup_sources():
    yield
    with SessionLocal() as s:
        s.query(DataSource).delete()
        s.commit()


def test_sync_all_requires_warehouse_source():
    from app.services.catalog_sync import sync_all_catalogs

    with SessionLocal() as s:
        s.add(_ds("erp"))
        s.commit()
    with SessionLocal() as s:
        out = sync_all_catalogs(s)
    assert out["ok"] is False
    assert "未配置仓库源" in out["error"]


def test_sync_all_fe_unreachable_reports_error(monkeypatch):
    from app.services import catalog_sync as mod
    from app.services.catalog_sync import sync_all_catalogs

    with SessionLocal() as s:
        s.add(DataSource(name="wh", kind="starrocks",
                         dsn_secret_ref="starrocks+pymysql://root:@fe:9030"))
        s.add(_ds("erp"))
        s.commit()

    def _boom(fe_dsn, sources):
        raise RuntimeError("StarRocks FE 不可达")

    monkeypatch.setattr(mod, "sync_catalogs", _boom)
    with SessionLocal() as s:
        out = sync_all_catalogs(s)
    assert out["ok"] is False
    assert "不可达" in out["error"]


def test_sync_all_returns_receipts(monkeypatch):
    from app.services import catalog_sync as mod
    from app.services.catalog_sync import sync_all_catalogs

    with SessionLocal() as s:
        s.add(DataSource(name="wh", kind="starrocks",
                         dsn_secret_ref="starrocks+pymysql://root:@fe:9030"))
        s.add(_ds("erp"))
        s.commit()

    monkeypatch.setattr(mod, "sync_catalogs", lambda fe, sources: [
        {"name": "erp", "kind": "mysql", "created": True}])
    with SessionLocal() as s:
        out = sync_all_catalogs(s)
    assert out["ok"] is True
    assert out["receipts"] == [{"name": "erp", "kind": "mysql", "created": True}]


def test_resolve_warehouse_source_skips_non_multicatalog():
    from app.services.catalog_sync import resolve_warehouse_source

    assert resolve_warehouse_source([_ds(None, kind="postgres")]) is None
    wh = _ds(None, kind="starrocks")
    assert resolve_warehouse_source([wh]).name == "t"
