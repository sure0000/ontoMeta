"""数据源 → Superset database（连接）的解析。

这一层要钉住的核心是**认不出来就报错**。指错连接不会立刻失败：Superset 会在另一台机器上
找到一张同名表，图照样画得出来，数字却是别人的。所以这里宁可拦住，也不挑一条"看起来对"的。

缓存（``data_sources.superset_database_id``）既是省网络的手段，也是人工兜底通道：
自动匹配认不出来时，直接给数据源填上编号即可。
"""

from __future__ import annotations

import uuid

import pytest

from app.connectors.superset import SupersetError
from app.database import SessionLocal
from app.models import DataSource
from app.services.superset_database import (
    SupersetDatabaseUnresolved,
    resolve_database_id,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def make_ds(db):
    """建一个临时数据源，用完删掉。"""
    created: list[str] = []

    def _make(*, dsn: str | None = "mysql://u:p@doris-fe:9030/dws", superset_database_id=None):
        row = DataSource(
            id=f"ds-{uuid.uuid4()}",
            name="测试数仓",
            kind="doris",
            purpose="warehouse",
            dsn_secret_ref=dsn,
            superset_database_id=superset_database_id,
        )
        db.add(row)
        db.commit()
        created.append(row.id)
        return row

    yield _make
    for ds_id in created:
        row = db.get(DataSource, ds_id)
        if row is not None:
            db.delete(row)
    db.commit()


class FakeClient:
    """只实现解析用得上的方法，并记录打了几次网。

    ``get_database`` **刻意不返回 sqlalchemy_uri**——真 Superset（6.1 实测）就是不返回，
    URI 只在 ``/connection`` 子资源里。第一版替身在这里多给了一个键，于是「读错端点」
    这个 bug 在测试里全绿、到真机上候选恒空，还对着用户说「一条连接都没有」。
    替身宁可比真货更吝啬，也不能比它慷慨。
    """

    def __init__(self, databases: list[dict]):
        self.databases = databases
        self.listed = 0
        self.read: list[int] = []

    def list_databases(self, page_size=100):
        self.listed += 1
        return [{"id": d["id"], "database_name": d.get("database_name")} for d in self.databases]

    def _find(self, database_id: int, operation: str) -> dict:
        for d in self.databases:
            if d["id"] == int(database_id):
                return d
        raise SupersetError(operation, "HTTP 404")

    def get_database(self, database_id):
        body = self._find(int(database_id), "get_database")
        return {"result": {k: v for k, v in body.items() if k != "sqlalchemy_uri"}}

    def get_database_connection(self, database_id):
        self.read.append(int(database_id))
        return {"result": self._find(int(database_id), "get_database_connection")}


def _db(id_: int, name: str, uri: str) -> dict:
    return {"id": id_, "database_name": name, "sqlalchemy_uri": uri}


def test_cached_id_short_circuits(db, make_ds):
    """已经解析过就不再打网——这是把结果落库的全部理由。"""
    ds = make_ds(superset_database_id=12)
    fake = FakeClient([])
    assert resolve_database_id(db, fake, ds.id) == 12
    assert fake.listed == 0


def test_matches_by_host_port_database_and_caches(db, make_ds):
    ds = make_ds()
    fake = FakeClient([
        _db(4, "别的库", "mysql://u:p@other-host:9030/dws"),
        _db(7, "数仓", "mysql://u:p@doris-fe:9030/dws"),
    ])
    assert resolve_database_id(db, fake, ds.id) == 7
    db.expire_all()
    assert db.get(DataSource, ds.id).superset_database_id == 7


def test_port_absent_on_either_side_still_matches(db, make_ds):
    """端口没写就不比端口：替它猜一个默认端口会把本该命中的连接判掉。"""
    ds = make_ds(dsn="mysql://u:p@doris-fe/dws")
    fake = FakeClient([_db(7, "数仓", "mysql://u:p@doris-fe:9030/dws")])
    assert resolve_database_id(db, fake, ds.id) == 7


def test_same_host_different_database_still_matches_when_unique(db, make_ds):
    """一条连接覆盖同实例的多个库（库是按 schema 传的），所以库名对不上不算不匹配。"""
    ds = make_ds(dsn="mysql://u:p@doris-fe:9030/ods")
    fake = FakeClient([_db(7, "数仓", "mysql://u:p@doris-fe:9030/information_schema")])
    assert resolve_database_id(db, fake, ds.id) == 7


def test_exact_database_wins_over_the_others_on_the_same_host(db, make_ds):
    ds = make_ds(dsn="mysql://u:p@doris-fe:9030/dws")
    fake = FakeClient([
        _db(7, "ODS 连接", "mysql://u:p@doris-fe:9030/ods"),
        _db(8, "DWS 连接", "mysql://u:p@doris-fe:9030/dws"),
    ])
    assert resolve_database_id(db, fake, ds.id) == 8


def test_ambiguous_candidates_are_refused_not_guessed(db, make_ds):
    """同一实例上多条连接、又没有一条库名对得上 → 报错并列出候选，绝不挑一条。"""
    ds = make_ds(dsn="mysql://u:p@doris-fe:9030/dws")
    fake = FakeClient([
        _db(7, "读写账号", "mysql://rw:p@doris-fe:9030/ods"),
        _db(8, "只读账号", "mysql://ro:p@doris-fe:9030/ads"),
    ])
    with pytest.raises(SupersetDatabaseUnresolved, match="多条连接"):
        resolve_database_id(db, fake, ds.id)
    db.expire_all()
    assert db.get(DataSource, ds.id).superset_database_id is None


def test_no_candidate_says_what_exists(db, make_ds):
    ds = make_ds()
    fake = FakeClient([_db(4, "别处", "mysql://u:p@other-host:9030/dws")])
    with pytest.raises(SupersetDatabaseUnresolved) as exc:
        resolve_database_id(db, fake, ds.id)
    assert "别处" in str(exc.value)  # 报错要说清 Superset 那边现在有什么


def test_connections_with_an_unreadable_uri_are_still_listed(db, make_ds):
    """读不到地址的连接**照样是真实存在的连接**。

    把它们从清单里抹掉，用户看到的就是「一条都没有」，然后跑去建一条其实已经有了的
    连接。地址读不出来就写「地址未知」，别把「我没看懂」说成「它不存在」。
    """
    ds = make_ds()
    fake = FakeClient([{"id": 4, "database_name": "看不到地址的连接"}])
    with pytest.raises(SupersetDatabaseUnresolved) as exc:
        resolve_database_id(db, fake, ds.id)
    message = str(exc.value)
    assert "看不到地址的连接" in message
    assert "地址未知" in message
    assert "一条都没有" not in message


def test_unparseable_local_dsn_points_at_the_manual_override(db, make_ds):
    ds = make_ds(dsn=None)
    with pytest.raises(SupersetDatabaseUnresolved, match="superset_database_id"):
        resolve_database_id(db, FakeClient([]), ds.id)


def test_landing_without_a_datasource_is_a_data_problem(db):
    """落点的数据源绑定是必填列，为空说明数据坏了——不能拿默认连接顶上。"""
    with pytest.raises(SupersetDatabaseUnresolved, match="数据异常"):
        resolve_database_id(db, FakeClient([]), None)
