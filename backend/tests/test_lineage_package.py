"""代码包扫描与血缘上报测试。

钉住的行为：
1. 扫一个**野包**：能解析的解析，存储过程/空文件/没有落点的逐个记原因，不抛错；
2. 表名对 URN：对不上但在本域 → blocked；带的库名不属于本域 → skipped；
3. 上报只写表级边——同一对表的多条关联键**合成一条** DataHub 边，不重复发；
4. 上报是 preview/apply 分离的：扫描不写 DataHub；apply 逐条记失败，单条失败不中断；
5. 画布补录走同一套落库，留一条 ``kind=manual`` 的档。
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from app.connectors import datahub as dh
from app.database import SessionLocal
from app.models import DomainContext, LineagePackage
from app.services import lineage_inventory, lineage_package
from app.services.lineage_inventory import DomainInventory, InventoryTable

DB = "erp_db"

#: 本仓的异步用例由 anyio 插件驱动（见 test_pipeline_lineage 的同款标记），
#: 不加标记 pytest 会直接判"不支持 async def"。
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _urn(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:mysql,{name},PROD)"


def _inventory(domain_id: str) -> DomainInventory:
    """域里有 4 张表：两张有血缘，两张孤岛（ext_credit 是本次补录的落点）。"""
    rows = [
        InventoryTable(_urn(f"{DB}.tabCustomer"), f"{DB}.tabCustomer", "mysql", 0, 2),
        InventoryTable(_urn(f"{DB}.tabSales Invoice"), f"{DB}.tabSales Invoice", "mysql", 1, 1),
        InventoryTable(_urn(f"{DB}.ext_credit"), f"{DB}.ext_credit", "mysql", 0, 0),
        InventoryTable(_urn(f"{DB}.imp_manual"), f"{DB}.imp_manual", "mysql", 0, 0),
    ]
    return DomainInventory(
        domain_id=domain_id,
        datahub_domain_id="urn:li:domain:erp",
        tables=tuple(rows),
        name_index={row.name.lower(): row.urn for row in rows}
        | {row.name.split(".")[1].lower(): row.urn for row in rows},
        databases=frozenset({DB}),
    )


@pytest.fixture
def domain_id():
    """每个用例一个独立域：datahub_domain_id 有唯一约束，复用会撞。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:erp-lineage-{uuid.uuid4().hex[:8]}",
            name="ERP血缘测试",
        )
        db.add(domain)
        db.commit()
        return domain.id


@pytest.fixture
def stub_inventory(monkeypatch, domain_id):
    async def _fake(db, target_domain_id, *, refresh=False):
        return _inventory(target_domain_id)

    monkeypatch.setattr(lineage_inventory, "get_inventory", _fake)
    return domain_id


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


GOOD_SQL = f"""
INSERT INTO `{DB}`.`ext_credit`
SELECT c.name, i.grand_total
FROM `{DB}`.`tabCustomer` c
JOIN `{DB}`.`tabSales Invoice` i ON i.customer = c.name
"""

OUT_OF_DOMAIN_SQL = f"""
INSERT INTO `{DB}`.`ext_credit`
SELECT t.id FROM `tmp_scratch`.`staging_rows` t
"""

PROCEDURE_SQL = "DELIMITER $$\nCREATE PROCEDURE p() BEGIN SELECT 1; END $$\n"


async def _scan(files: dict[str, str], domain_id: str) -> LineagePackage:
    with SessionLocal() as db:
        return await lineage_package.scan(
            db,
            domain_id=domain_id,
            filename="pack.zip",
            blob=_zip(files),
            dialect="mysql",
        )


# --------------------------------------------------------------------------- 扫描


async def test_scan_reports_edges_and_failures(stub_inventory):
    package = await _scan(
        {
            "credit/build.sql": GOOD_SQL,
            "legacy/proc.sql": PROCEDURE_SQL,
            "tmp/empty.sql": "   ",
            "reports/query.sql": f"SELECT * FROM `{DB}`.`tabCustomer`",
            "README.md": "not sql",
        },
        stub_inventory,
    )

    with SessionLocal() as db:
        saved = db.get(LineagePackage, package.id)
        assert saved.sql_files == 4  # README.md 不算
        # 解析成功的是 build.sql 与 query.sql；存储过程与空文件都没解析成
        assert saved.parsed_files == 2
        kinds = {item["file"]: item["kind"] for item in _failures(saved)}
        assert kinds["legacy/proc.sql"] == "parse_error"
        # 纯查询解析成功，只是没有落点——不算解析失败
        assert kinds["reports/query.sql"] == "no_landing"
        reasons = {item["file"]: item["reason"] for item in _failures(saved)}
        assert "legacy/proc.sql" in reasons and "存储过程" in reasons["legacy/proc.sql"]
        assert "tmp/empty.sql" in reasons
        # 纯查询不是解析失败，但也没有落点——要在清单里说清楚
        assert "reports/query.sql" in reasons

        edges = {(e.source_table, e.target_table, e.join_key, e.state) for e in saved.edges}
        assert (
            f"{DB}.tabCustomer",
            f"{DB}.ext_credit",
            f"{DB}.tabSales Invoice.customer = {DB}.tabCustomer.name",
            "ok",
        ) in edges
        assert saved.status == "scanned"
        assert saved.applied_edges == 0


async def test_out_of_domain_table_is_skipped_not_blocked(stub_inventory):
    package = await _scan({"x/out.sql": OUT_OF_DOMAIN_SQL}, stub_inventory)

    with SessionLocal() as db:
        saved = db.get(LineagePackage, package.id)
        (edge,) = saved.edges
        assert edge.state == "skipped"
        assert "不在本域" in (edge.reason or "")


async def test_unknown_table_in_domain_is_blocked(stub_inventory):
    package = await _scan(
        {"x/new.sql": f"INSERT INTO `{DB}`.`brand_new` SELECT c.name FROM `{DB}`.`tabCustomer` c"},
        stub_inventory,
    )

    with SessionLocal() as db:
        (edge,) = db.get(LineagePackage, package.id).edges
        assert edge.state == "blocked"
        assert edge.reason == lineage_package.REASON_TARGET_UNRESOLVED


async def test_scan_does_not_write_datahub(stub_inventory, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _spy(connector, upstream, downstream):
        calls.append((upstream, downstream))
        return True

    monkeypatch.setattr(dh, "add_lineage_edge", _spy)
    await _scan({"credit/build.sql": GOOD_SQL}, stub_inventory)

    assert calls == []


# --------------------------------------------------------------------------- 上报


@pytest.fixture
def stub_datahub(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _add(connector, upstream, downstream):
        calls.append((upstream, downstream))
        return True

    async def _aclose(self):
        return None

    monkeypatch.setattr(dh, "add_lineage_edge", _add)
    monkeypatch.setattr(dh.DataHubConnector, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(dh.DataHubConnector, "aclose", _aclose)
    return calls


async def test_apply_writes_one_edge_per_table_pair(stub_inventory, stub_datahub):
    """两条关联键之间只有一对表——DataHub 只收表级边，发一条就够。"""
    sql = f"""
    INSERT INTO `{DB}`.`ext_credit`
    SELECT c.name FROM `{DB}`.`tabCustomer` c
    JOIN `{DB}`.`tabSales Invoice` i ON i.customer = c.name AND i.territory = c.territory
    """
    package = await _scan({"credit/multi.sql": sql}, stub_inventory)

    with SessionLocal() as db:
        receipt = await lineage_package.apply(db, package.id)

    pairs = set(stub_datahub)
    assert (_urn(f"{DB}.tabSales Invoice"), _urn(f"{DB}.ext_credit")) in pairs
    assert len(pairs) == len(stub_datahub), "同一对表被重复发送"
    assert receipt.failed == 0
    # ext_credit 上报前是孤岛，上报后脱离
    assert receipt.resolved == 1

    with SessionLocal() as db:
        saved = db.get(LineagePackage, package.id)
        assert saved.status == "applied"
        assert saved.applied_edges == receipt.applied
        assert all(edge.applied_at is not None for edge in saved.edges if edge.state == "ok")


async def test_apply_only_selected_targets_leaves_package_partial(stub_inventory, stub_datahub):
    package = await _scan(
        {
            "a.sql": GOOD_SQL,
            "b.sql": f"INSERT INTO `{DB}`.`imp_manual` SELECT c.name FROM `{DB}`.`tabCustomer` c",
        },
        stub_inventory,
    )

    with SessionLocal() as db:
        receipt = await lineage_package.apply(db, package.id, targets=[f"{DB}.ext_credit"])

    assert receipt.applied > 0
    with SessionLocal() as db:
        saved = db.get(LineagePackage, package.id)
        assert saved.status == "partial"
        pending = [e for e in saved.edges if e.state == "ok" and e.applied_at is None]
        assert {e.target_table for e in pending} == {f"{DB}.imp_manual"}


async def test_apply_records_failures_without_stopping(stub_inventory, monkeypatch):
    async def _flaky(connector, upstream, downstream):
        if "tabCustomer" in upstream:
            raise dh.DataHubWriteError("updateLineage", downstream, RuntimeError("boom"))
        return True

    async def _aclose(self):
        return None

    monkeypatch.setattr(dh, "add_lineage_edge", _flaky)
    monkeypatch.setattr(dh.DataHubConnector, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(dh.DataHubConnector, "aclose", _aclose)

    package = await _scan({"credit/build.sql": GOOD_SQL}, stub_inventory)
    with SessionLocal() as db:
        receipt = await lineage_package.apply(db, package.id)

    assert receipt.failed >= 1
    assert receipt.applied >= 1  # 另一条仍然写进去了
    assert receipt.failures and "boom" in receipt.failures[0]["error"]


async def test_reapply_is_a_noop(stub_inventory, stub_datahub):
    package = await _scan({"credit/build.sql": GOOD_SQL}, stub_inventory)
    with SessionLocal() as db:
        await lineage_package.apply(db, package.id)
    calls_after_first = len(stub_datahub)

    with SessionLocal() as db:
        second = await lineage_package.apply(db, package.id)

    assert second.applied == 0
    assert len(stub_datahub) == calls_after_first


# --------------------------------------------------------------------------- 画布


async def test_manual_apply_writes_and_keeps_a_record(stub_inventory, stub_datahub):
    with SessionLocal() as db:
        receipt = await lineage_package.apply_manual(
            db,
            domain_id=stub_inventory,
            edges=[
                {
                    "source_table": f"{DB}.tabCustomer",
                    "target_table": f"{DB}.imp_manual",
                    "join_keys": [f"{DB}.tabCustomer.name = {DB}.imp_manual.cust_code"],
                }
            ],
            label="画布测试",
        )

    assert receipt.applied == 1
    assert receipt.resolved == 1
    assert (_urn(f"{DB}.tabCustomer"), _urn(f"{DB}.imp_manual")) in stub_datahub

    with SessionLocal() as db:
        record = (
            db.query(LineagePackage)
            .filter(LineagePackage.kind == "manual", LineagePackage.name == "画布测试")
            .one()
        )
        assert record.status == "applied"
        (edge,) = record.edges
        # 关联键只存在本地：DataHub 的 updateLineage 只收表级边
        assert edge.join_key.endswith("imp_manual.cust_code")


async def test_manual_apply_skips_self_reference(stub_inventory, stub_datahub):
    with SessionLocal() as db:
        receipt = await lineage_package.apply_manual(
            db,
            domain_id=stub_inventory,
            edges=[{"source_table": f"{DB}.tabCustomer", "target_table": f"{DB}.tabCustomer"}],
        )

    assert receipt.applied == 0
    assert stub_datahub == []


def _failures(package: LineagePackage) -> list[dict]:
    import json

    return json.loads(package.failures_json or "[]")
