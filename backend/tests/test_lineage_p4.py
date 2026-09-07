"""P4：DDL 外键、批量上报、人工表名映射。

钉住的行为：

1. DDL 外键三种写法（表级约束 / ALTER ADD / 列级内联）都要认——``_join_keys`` 只扫
   等值谓词，一条都扫不到；
1b. **而且此前不是「产出为 0」，是「产出错的」**：``_statement_lineage`` 用
   ``find_all(exp.Table)`` 取上游，会把 ``REFERENCES customers(id)`` 里的 customers
   当成来源表，推出一条「customers 加工至 orders」的假血缘。上报进 DataHub 后
   下游会把它判成 derivation、命名成「派生出」。上游改成只从查询体里取；
2. DDL 外键是**关联不是血缘**：kind=relation，不上报 DataHub，只进本体证据；
3. 外键方向是**声明**出来的，不拿统计去二猜（空表会给出相反的答案）；
4. 上报按批发，300 条边不该是 300 次往返；整批失败要退回逐条定位到具体的边；
5. 人工映射当场修复 blocked 边——只存不修等于让人再点一次重扫，而重扫结果一样。
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from app.api import lineage as lineage_api
from app.connectors import datahub as dh
from app.database import SessionLocal
from app.models import DomainContext, LineagePackage, LineagePackageEdge
from app.services import lineage_inventory, lineage_package
from app.services.lineage_inventory import DomainInventory, InventoryTable
from app.services.sql_lineage_extractor import extract

pytestmark = pytest.mark.anyio

DB = "erp_db"


# --------------------------------------------------------------------------- 6.2 DDL 外键


def test_extracts_table_level_foreign_key():
    result = extract(
        """
        CREATE TABLE erp_db.orders (
          id INT PRIMARY KEY,
          customer_id INT,
          CONSTRAINT fk_c FOREIGN KEY (customer_id) REFERENCES erp_db.customers(id)
        );
        """
    )
    assert result.error is None
    assert [fk.render() for fk in result.foreign_keys] == [
        "erp_db.orders.customer_id = erp_db.customers.id"
    ]
    # 纯建表没有数据流动——血缘必须是空的，那不是漏解析。
    assert result.lineages == []


def test_extracts_inline_column_reference():
    """``order_id INT REFERENCES orders(id)`` 根本没有 ForeignKey 节点，只有 Reference。"""
    result = extract(
        "CREATE TABLE items (id INT, order_id INT REFERENCES orders(id));"
    )
    assert [fk.render() for fk in result.foreign_keys] == [
        "items.order_id = orders.id"
    ]


def test_extracts_alter_table_add_foreign_key():
    result = extract(
        "ALTER TABLE orders ADD CONSTRAINT fk_s FOREIGN KEY (shop_id) REFERENCES shops(id);"
    )
    assert [fk.render() for fk in result.foreign_keys] == ["orders.shop_id = shops.id"]


def test_extracts_composite_foreign_key_pairwise():
    result = extract(
        "ALTER TABLE a ADD FOREIGN KEY (x, y) REFERENCES b(p, q);"
    )
    assert [fk.render() for fk in result.foreign_keys] == [
        "a.x = b.p",
        "a.y = b.q",
    ]


def test_foreign_key_without_target_columns_is_dropped():
    """``REFERENCES t`` 不写列 → 不猜主键叫什么，丢掉。"""
    result = extract("ALTER TABLE a ADD FOREIGN KEY (x) REFERENCES b;")
    assert result.foreign_keys == []


def test_self_referencing_foreign_key_is_dropped():
    result = extract(
        "CREATE TABLE t (id INT, parent_id INT, FOREIGN KEY (parent_id) REFERENCES t(id));"
    )
    assert result.foreign_keys == []


def test_ddl_only_file_is_not_counted_as_no_landing_failure():
    """有外键就不算「没有可推的落点」——记成失败会让人以为白扫了。"""
    outcome = lineage_package._scan_members(
        [("ddl.sql", b"CREATE TABLE a (x INT, FOREIGN KEY (x) REFERENCES b(y));")],
        "mysql",
    )
    assert outcome.edges == []
    assert [row[:3] for row in outcome.relations] == [("a", "b", "a.x = b.y")]
    assert outcome.failures == []


def test_pure_query_file_is_still_reported_as_no_landing():
    outcome = lineage_package._scan_members(
        [("q.sql", b"SELECT * FROM a JOIN b ON a.x = b.y;")], "mysql"
    )
    assert outcome.relations == []
    assert [f["kind"] for f in outcome.failures] == ["no_landing"]


# --------------------------------------------------------------------------- 落库与上报口径


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


@pytest.fixture
def domain():
    db = SessionLocal()
    row = DomainContext(
        id=str(uuid.uuid4()),
        name=f"p4-{uuid.uuid4().hex[:6]}",
        datahub_domain_id="urn:li:domain:p4",
    )
    db.add(row)
    db.commit()
    domain_id = row.id
    db.close()
    yield domain_id
    db = SessionLocal()
    for package in db.query(LineagePackage).filter(
        LineagePackage.domain_context_id == domain_id
    ):
        db.query(LineagePackageEdge).filter(
            LineagePackageEdge.package_id == package.id
        ).delete()
    db.query(LineagePackage).filter(
        LineagePackage.domain_context_id == domain_id
    ).delete()
    db.query(DomainContext).filter(DomainContext.id == domain_id).delete()
    db.commit()
    db.close()
    lineage_inventory.invalidate(domain_id)


def _inventory(domain_id: str, *names: str) -> DomainInventory:
    tables = tuple(
        InventoryTable(
            urn=f"urn:li:dataset:({name})", name=f"{DB}.{name}", platform="mysql",
            upstream=0, downstream=0,
        )
        for name in names
    )
    return DomainInventory(
        domain_id=domain_id,
        datahub_domain_id="urn:li:domain:p4",
        tables=tables,
        name_index={t.name.lower(): t.urn for t in tables}
        | {n.lower(): t.urn for n, t in zip(names, tables, strict=True)},
        databases=frozenset({DB}),
    )


def _patch_inventory(monkeypatch, *names: str) -> None:
    """把域家底换成固定的一组表。``get_inventory`` 是 async，替身也必须是。"""

    async def fake(db, domain_id, refresh=False):
        return _inventory(domain_id, *names)

    monkeypatch.setattr(lineage_inventory, "get_inventory", fake)
    monkeypatch.setattr(lineage_package.lineage_inventory, "get_inventory", fake)


async def test_ddl_package_lands_relations_that_are_never_reported(domain, monkeypatch):
    """纯 DDL 包：产出 > 0，但产出的是 relation 边，不进上报队列。"""
    _patch_inventory(monkeypatch, "orders", "customers")

    db = SessionLocal()
    try:
        package = await lineage_package.scan(
            db,
            domain_id=domain,
            filename="ddl.zip",
            blob=_zip(
                {
                    "schema.sql": (
                        "CREATE TABLE erp_db.orders (id INT, customer_id INT, "
                        "FOREIGN KEY (customer_id) REFERENCES erp_db.customers(id));"
                    )
                }
            ),
        )
        edges = list(package.edges)
        assert len(edges) == 1  # 产出 > 0——这正是过去为 0 的那个 bug
        assert edges[0].kind == "relation"
        assert edges[0].join_key == "erp_db.orders.customer_id = erp_db.customers.id"

        # 上报队列里一条都没有：关联不是血缘
        receipt = await lineage_package.apply(db, package.id)
        assert receipt.applied == 0
        assert receipt.failed == 0
    finally:
        db.close()


def test_ddl_foreign_key_reaches_evidence_as_declared_and_directed(domain):
    """DDL 外键进本体证据时：置信度最高档、方向按声明、描述写「DDL 里声明的外键」。"""
    from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
    from app.services import observed_joins
    from app.services.evidence_builder import EvidenceBuilder

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="d.sql", kind="scan")
        db.add(package)
        db.flush()
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                kind="relation",
                source_table="erp_db.orders",
                target_table="erp_db.customers",
                join_key="erp_db.orders.customer_id = erp_db.customers.id",
                source_file="schema.sql",
                state="ok",
            )
        )
        db.commit()

        joins = observed_joins.load_for_domain(db, domain)
        assert len(joins) == 1
        assert joins[0].origin == observed_joins.ORIGIN_DDL_FOREIGN_KEY
        assert joins[0].confidence == 0.8
        assert joins[0].directed is True
    finally:
        db.close()

    def _ds(name, column, distinct, rows):
        return DatasetInput(
            urn=f"urn:li:dataset:({name})",
            name=f"erp_db.{name}",
            row_count=rows,
            fields=[
                FieldInput(
                    name=column, data_type="INT", unique_count=distinct,
                    sample_values=["1"],
                ),
                FieldInput(name="note", data_type="VARCHAR", sample_values=["x"]),
            ],
        )

    # customers 是空表 → 统计判不出它是主端；声明的方向必须压过统计。
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d", name="d"),
        datasets=[_ds("orders", "customer_id", 700, 1571), _ds("customers", "id", 0, 0)],
    )
    merged: dict[str, list] = {}
    EvidenceBuilder._merge_observed_joins(bundle, joins, merged)

    assert list(merged) == ["erp_db.orders"]
    edge = merged["erp_db.orders"][0]
    assert edge.target_table == "erp_db.customers"
    assert edge.origin == "ddl_foreign_key"


# --------------------------------------------------------------------------- 6.3 批量上报


async def test_apply_batches_instead_of_one_round_trip_per_edge(domain, monkeypatch):
    """300 条边不该是 300 次往返。"""
    names = [f"t{i}" for i in range(60)]
    _patch_inventory(monkeypatch, *names)

    calls: list[int] = []

    async def fake_mutate(connector, operation, urn, query, variables):
        calls.append(len(variables["input"]["edgesToAdd"]))
        return True

    monkeypatch.setattr(dh, "_mutate", fake_mutate)
    monkeypatch.setattr(dh.DataHubConnector, "aclose", lambda self: _noop())

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="p.zip", kind="scan")
        db.add(package)
        db.flush()
        for i in range(1, 60):
            db.add(
                LineagePackageEdge(
                    package_id=package.id,
                    source_table=f"{DB}.t0",
                    target_table=f"{DB}.t{i}",
                    source_file="a.sql",
                    source_urn="urn:li:dataset:(t0)",
                    target_urn=f"urn:li:dataset:(t{i})",
                    state="ok",
                )
            )
        db.commit()

        receipt = await lineage_package.apply(db, package.id)
        assert receipt.applied == 59
        # 59 条边、批大小 50 → 2 次往返，而不是 59 次
        assert len(calls) == 2
        assert sum(calls) == 59
    finally:
        db.close()


async def test_batch_failure_falls_back_to_per_edge_attribution(domain, monkeypatch):
    """整批失败不能让回执退化成「这批全挂了」——要定位到具体的边。"""
    _patch_inventory(monkeypatch, "a", "b", "c")

    bad = "urn:li:dataset:(c)"

    async def fake_mutate(connector, operation, urn, query, variables):
        adds = variables["input"]["edgesToAdd"]
        if any(edge["downstreamUrn"] == bad for edge in adds):
            raise dh.DataHubWriteError("updateLineage", urn, RuntimeError("boom"))
        return True

    monkeypatch.setattr(dh, "_mutate", fake_mutate)
    monkeypatch.setattr(dh.DataHubConnector, "aclose", lambda self: _noop())

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="p.zip", kind="scan")
        db.add(package)
        db.flush()
        for name in ("b", "c"):
            db.add(
                LineagePackageEdge(
                    package_id=package.id,
                    source_table=f"{DB}.a",
                    target_table=f"{DB}.{name}",
                    source_file="a.sql",
                    source_urn="urn:li:dataset:(a)",
                    target_urn=f"urn:li:dataset:({name})",
                    state="ok",
                )
            )
        db.commit()

        receipt = await lineage_package.apply(db, package.id)
        # 好的那条照样写进去了，坏的那条被单独点名
        assert receipt.applied == 1
        assert receipt.failed == 1
        assert len(receipt.failures) == 1
        assert receipt.failures[0]["target"] == bad
    finally:
        db.close()


async def _noop():
    return None


# --------------------------------------------------------------------------- 6.4 人工映射


async def test_mapping_repairs_blocked_edges_on_the_spot(domain, monkeypatch):
    """只存映射不回填 = 让人再点一次重扫，而重扫用同一套 resolve、结果一样。"""
    _patch_inventory(monkeypatch, "orders")

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="p.zip", kind="scan")
        db.add(package)
        db.flush()
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                source_table=f"{DB}.v_orders_legacy",  # DataHub 里没有这张
                target_table=f"{DB}.orders",
                source_file="a.sql",
                target_urn="urn:li:dataset:(orders)",
                state="blocked",
                reason=lineage_package.REASON_SOURCE_UNRESOLVED,
            )
        )
        db.commit()
        edge_id = package.edges[0].id

        repaired = await lineage_package.save_mapping(
            db,
            domain_id=domain,
            sql_table=f"{DB}.V_Orders_Legacy",  # 大小写随意
            target_urn="urn:li:dataset:(orders)",
            operator="张三",
        )
        assert repaired == 1

        edge = db.get(LineagePackageEdge, edge_id)
        assert edge.state == "ok"
        assert edge.source_urn == "urn:li:dataset:(orders)"

        mappings = lineage_package.list_mappings(db, domain)
        assert [m.sql_table for m in mappings] == [f"{DB}.v_orders_legacy"]
    finally:
        db.close()


async def test_mapping_refreshes_the_reason_even_when_still_blocked(domain, monkeypatch):
    """两端都对不上的边：映射一端后仍是 blocked，但**理由要换成另一端**。

    不换的话人映射完看见的还是原来那句，会以为映射根本没生效。
    """
    _patch_inventory(monkeypatch, "orders")

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="p.zip", kind="scan")
        db.add(package)
        db.flush()
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                source_table=f"{DB}.mystery_src",
                target_table=f"{DB}.mystery_dst",
                source_file="a.sql",
                state="blocked",
                reason=lineage_package.REASON_TARGET_UNRESOLVED,
            )
        )
        db.commit()
        edge_id = package.edges[0].id

        repaired = await lineage_package.save_mapping(
            db,
            domain_id=domain,
            sql_table=f"{DB}.mystery_dst",
            target_urn="urn:li:dataset:(orders)",
        )
        assert repaired == 0  # 还没通，不算修好

        edge = db.get(LineagePackageEdge, edge_id)
        assert edge.state == "blocked"
        assert edge.reason == lineage_package.REASON_SOURCE_UNRESOLVED
        assert edge.target_urn == "urn:li:dataset:(orders)"
    finally:
        db.close()


async def test_mapping_rejects_target_outside_the_domain(domain, monkeypatch):
    _patch_inventory(monkeypatch, "orders")
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="不在本域"):
            await lineage_package.save_mapping(
                db,
                domain_id=domain,
                sql_table="whatever",
                target_urn="urn:li:dataset:(not_here)",
            )
    finally:
        db.close()


async def test_overview_distinguishes_tables_with_no_relation_evidence(domain, monkeypatch):
    """概览将纯孤岛与已有本地关联证据的孤岛分开统计。"""
    _patch_inventory(monkeypatch, "orders", "customers", "unrelated")

    db = SessionLocal()
    try:
        package = LineagePackage(domain_context_id=domain, name="ddl.zip", kind="scan")
        db.add(package)
        db.flush()
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                kind="relation",
                source_table=f"{DB}.orders",
                target_table=f"{DB}.customers",
                join_key=f"{DB}.orders.customer_id = {DB}.customers.id",
                source_file="schema.sql",
                state="ok",
            )
        )
        db.commit()

        overview = await lineage_api.get_overview(domain, db=db)

        assert overview.no_lineage == 3
        assert overview.no_any_relation == 1
        assert overview.isolated == overview.no_lineage
    finally:
        db.close()
