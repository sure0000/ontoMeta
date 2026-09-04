"""血缘家底读取的缓存与并发合并测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.services import lineage_inventory
from app.services.lineage_inventory import DomainInventory

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_concurrent_misses_share_one_inventory_load(monkeypatch):
    lineage_inventory._cache.clear()
    lineage_inventory._inflight.clear()
    calls = 0
    inventory = DomainInventory(
        domain_id="domain-1",
        datahub_domain_id="urn:li:domain:one",
        tables=(),
    )

    async def fake_load(_db, _domain_id):
        nonlocal calls
        calls += 1
        # 保证其它调用有机会在任务完成前进入 get_inventory。
        await asyncio.sleep(0)
        return inventory

    monkeypatch.setattr(lineage_inventory, "_load_inventory", fake_load)
    try:
        results = await asyncio.gather(
            *(lineage_inventory.get_inventory(object(), "domain-1") for _ in range(5))
        )

        assert calls == 1
        assert all(result is inventory for result in results)
    finally:
        lineage_inventory._cache.clear()
        lineage_inventory._inflight.clear()
