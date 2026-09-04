"""数据域的血缘家底：哪些表是孤岛，以及 SQL 里的表名怎么对到 DataHub URN。

补录页只讲一个数：**这个域里有多少张表是孤岛**（上下游皆空）。孤岛表在本体生成时
会被判成 data_table、所在业务环节断裂、关系推断跟着丢——这就是补录要解决的问题。

**不走 fetch_domain_bundle**：那个为每张表拉 schema + profile + 样例值，erpnext 域
1000+ 张表要跑几分钟，页面一进来就卡死。这里只问两件事——域里有哪些表、每张表
上下游各几条，用 ``fetch_domain_dataset_index`` 的轻查询（实测约 6 秒）。
字段是画布单独按表取的（``get_dataset_by_urn``），不在这份家底里。

带一个**很短的进程内缓存**：同一次"扫描 → 看清单 → 上报"的操作序列共用一次抓取；
上报后立即失效，因为孤岛数正是被这次上报改掉的东西。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.connectors.datahub import DataHubConnector
from app.models import DomainContext
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.lineage_inventory")

#: 缓存存活秒数。够一次操作序列复用，短到不会让人看见过期的孤岛数。
CACHE_TTL_SECONDS = 90.0

_cache: dict[str, tuple[float, "DomainInventory"]] = {}
# A cache only helps after the first request completes.  The page fans out to
# several endpoints at once, so coalesce concurrent misses as well; otherwise
# every endpoint starts its own expensive DataHub index scan.
_inflight: dict[str, asyncio.Task["DomainInventory"]] = {}


@dataclass(frozen=True)
class InventoryTable:
    urn: str
    #: ``库.表``——与 SQL 里写的表名同形，扫描靠它对上号。
    name: str
    platform: str | None
    upstream: int
    downstream: int

    @property
    def isolated(self) -> bool:
        """上下游皆空即孤岛。这是本体生成里判 data_table 的同一条口径。"""
        return self.upstream == 0 and self.downstream == 0


@dataclass(frozen=True)
class DomainInventory:
    domain_id: str
    datahub_domain_id: str
    tables: tuple[InventoryTable, ...]
    #: 归一表名 → URN。全名（库.表）与唯一的裸表名都进索引。
    name_index: dict[str, str] = field(default_factory=dict)
    #: 本域出现过的库名（小写），用来区分"本域内没对上"与"根本不在本域"。
    databases: frozenset[str] = frozenset()

    @property
    def total(self) -> int:
        return len(self.tables)

    @property
    def isolated_tables(self) -> tuple[InventoryTable, ...]:
        return tuple(table for table in self.tables if table.isolated)

    @property
    def with_lineage(self) -> int:
        return sum(1 for table in self.tables if not table.isolated)

    def resolve(self, table_name: str) -> str | None:
        """SQL 里的表名 → DataHub URN。对不上返回 None——不猜。"""
        key = table_name.strip().strip("`\"'").lower()
        if key in self.name_index:
            return self.name_index[key]
        if "." in key:
            return self.name_index.get(key.rsplit(".", 1)[1])
        return None

    def in_domain(self, table_name: str) -> bool:
        """带库名且库名不属于本域 → 这张表根本不在本域（跳过，不是"没对上"）。"""
        key = table_name.strip().strip("`\"'").lower()
        if "." not in key:
            return True
        return key.rsplit(".", 1)[0] in self.databases


def _build(domain: DomainContext, rows: list[dict]) -> DomainInventory:
    tables: list[InventoryTable] = []
    bare_names: dict[str, list[str]] = {}
    full_index: dict[str, str] = {}
    databases: set[str] = set()

    for row in rows:
        name = row["name"]
        tables.append(
            InventoryTable(
                urn=row["urn"],
                name=name,
                platform=row.get("platform"),
                upstream=row.get("upstream", 0),
                downstream=row.get("downstream", 0),
            )
        )
        full_index[name.lower()] = row["urn"]
        if "." in name:
            database, bare = name.rsplit(".", 1)
            databases.add(database.lower())
            bare_names.setdefault(bare.lower(), []).append(row["urn"])
        else:
            bare_names.setdefault(name.lower(), []).append(row["urn"])

    # 裸表名只在**全域唯一**时才进索引：同名表分处两个库时，认哪一个都是猜。
    for bare, urns in bare_names.items():
        if len(urns) == 1 and bare not in full_index:
            full_index[bare] = urns[0]

    return DomainInventory(
        domain_id=domain.id,
        datahub_domain_id=domain.datahub_domain_id or "",
        tables=tuple(tables),
        name_index=full_index,
        databases=frozenset(databases),
    )


async def _load_inventory(db: Session, domain_id: str) -> DomainInventory:
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise ValueError("数据域不存在")

    runtime = SettingsService().get_datahub_runtime(db)
    connector = DataHubConnector(runtime)
    try:
        rows = await connector.fetch_domain_dataset_index(domain.datahub_domain_id)
    finally:
        await connector.aclose()

    inventory = _build(domain, rows)
    _cache[domain_id] = (time.monotonic(), inventory)
    logger.info(
        "域 %s 家底：%d 张表，其中孤岛 %d",
        domain.name,
        inventory.total,
        len(inventory.isolated_tables),
    )
    return inventory


async def get_inventory(db: Session, domain_id: str, *, refresh: bool = False) -> DomainInventory:
    """取该域的家底。默认走短缓存，``refresh=True`` 强制重抓。

    同一事件循环内的并发调用共享一个 DataHub 请求任务。``shield`` 防止某个
    HTTP 请求被取消时把其它等待者的共享任务一并取消。
    """
    cached = _cache.get(domain_id)
    if not refresh and cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    task = _inflight.get(domain_id)
    # ASGI normally uses one loop per worker, but test clients and embedded
    # deployments can create more than one.  A Task cannot be awaited from a
    # different loop, so only reuse a task owned by the current loop.
    if task is not None and task.get_loop() is not asyncio.get_running_loop():
        _inflight.pop(domain_id, None)
        task = None
    if task is None:
        task = asyncio.create_task(_load_inventory(db, domain_id))
        _inflight[domain_id] = task

    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _inflight.get(domain_id) is task:
            _inflight.pop(domain_id, None)


def invalidate(domain_id: str) -> None:
    """上报后立刻失效：孤岛数正是被这次上报改掉的东西。"""
    _cache.pop(domain_id, None)
