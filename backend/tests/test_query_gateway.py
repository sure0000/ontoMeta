"""统一查询网关 P0:选源逻辑(warehouse-first)与 run_sql target 路由。

覆盖 ``services.data_app.resolve_domain_data_source`` 的三种策略:
- 默认取 warehouse 源(catalog_name 空/"internal"),绝不碰源库 catalog
- 显式 target_catalog 才精确匹配源库 catalog;匹配不到返回 None(降级「仅建议 SQL」)
- 无可用源 / 全是 mock 返回 None
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DataSource
from app.services.data_app import resolve_domain_data_source


def _seed(name: str, *, kind: str = "postgres", dsn: str | None = "postgresql://x/y",
          catalog_name: str | None = None) -> str:
    with SessionLocal() as db:
        ds = DataSource(name=name, kind=kind, dsn_secret_ref=dsn, catalog_name=catalog_name)
        db.add(ds)
        db.commit()
        return ds.id


@pytest.fixture(autouse=True)
def _cleanup_sources():
    yield
    with SessionLocal() as db:
        db.query(DataSource).delete()
        db.commit()


def test_warehouse_first_prefers_null_catalog_source():
    """默认选源:仓库源(catalog_name 空)优先于源库 catalog。"""
    erp_id = _seed("erp", kind="postgres", catalog_name="erp")
    wh_id = _seed("warehouse", kind="starrocks", catalog_name=None)
    with SessionLocal() as db:
        s = resolve_domain_data_source(db)
        assert s.id == wh_id, "应选 warehouse 源,而不是源库 catalog"


def test_warehouse_first_accepts_internal_marker():
    """catalog_name="internal" 与空值等价,都是 warehouse 源。"""
    internal_id = _seed("warehouse", kind="starrocks", catalog_name="internal")
    _seed("erp", kind="postgres", catalog_name="erp")
    with SessionLocal() as db:
        s = resolve_domain_data_source(db)
        assert s.id == internal_id


def test_target_catalog_exact_match():
    """显式 target 才查源库 catalog;同名 catalog 多个时取最新更新。"""
    _seed("wh", kind="starrocks", catalog_name=None)
    erp_id = _seed("erp", kind="postgres", catalog_name="erp")
    with SessionLocal() as db:
        s = resolve_domain_data_source(db, target_catalog="erp")
        assert s.id == erp_id


def test_target_catalog_miss_returns_none():
    """target 匹配不到时返回 None(run_sql 降级「仅建议 SQL」),不让 agent 悄悄换源。"""
    _seed("wh", kind="starrocks", catalog_name=None)
    with SessionLocal() as db:
        assert resolve_domain_data_source(db, target_catalog="crm") is None


def test_target_warehouse_only_matches_marked_sources():
    """显式 target="warehouse" 只认 warehouse 源;只有源库 catalog 时返回 None。"""
    _seed("erp", kind="postgres", catalog_name="erp")
    with SessionLocal() as db:
        assert resolve_domain_data_source(db, target_catalog="warehouse") is None


def test_no_usable_source_returns_none():
    with SessionLocal() as db:
        assert resolve_domain_data_source(db) is None
    _seed("mock", kind="mock", dsn=None)
    with SessionLocal() as db:
        assert resolve_domain_data_source(db) is None, "mock 源不算可用"


def test_mock_source_excluded_from_target():
    _seed("mock", kind="mock", dsn=None, catalog_name="erp")
    with SessionLocal() as db:
        assert resolve_domain_data_source(db, target_catalog="erp") is None


def test_legacy_all_null_degrades_to_latest():
    """存量库全无 catalog_name:退化取最新更新的可用源(与旧行为一致)。

    时间戳显式错开——SQLite 的 func.now() 是秒级精度,同一秒内注册的两个源
    排序会打成平手(此时退化为稳定排序的 uuid 序,仍然确定,只是断言不过)。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(DataSource(name="first", kind="postgres", dsn_secret_ref="postgresql://x/y",
                          created_at=now, updated_at=now))
        db.add(DataSource(name="second", kind="postgres", dsn_secret_ref="postgresql://x/y",
                          created_at=now + timedelta(seconds=2),
                          updated_at=now + timedelta(seconds=2)))
        db.commit()
    with SessionLocal() as db:
        s = resolve_domain_data_source(db)
        assert s.name == "second"
