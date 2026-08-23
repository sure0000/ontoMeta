"""数据应用阶段 2：只读执行器安全校验、真实 SQLite 执行、对外只读 API + scope。"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, Property
from app.services.data_app_executor import (
    ExecutionError,
    execute_sql,
    is_read_only,
)


# --------------------------------------------------------------- executor unit


@pytest.mark.parametrize(
    "sql,ok",
    [
        ("SELECT * FROM orders", True),
        ("WITH t AS (SELECT 1 AS a) SELECT a FROM t", True),
        ("select channel, sum(amount) from orders group by channel", True),
        ("INSERT INTO orders VALUES (1)", False),
        ("UPDATE orders SET amount = 0", False),
        ("DELETE FROM orders", False),
        ("DROP TABLE orders", False),
        ("SELECT 1; SELECT 2", False),
        ("SELECT * FROM orders; DROP TABLE orders", False),
        ("", False),
    ],
)
def test_is_read_only(sql, ok):
    result, _reason = is_read_only(sql)
    assert result is ok


def test_execute_sql_sqlite(tmp_path):
    db_path = tmp_path / "phys.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE orders (channel TEXT, amount REAL);
        INSERT INTO orders VALUES ('A', 100), ('A', 50), ('B', 30);
        """
    )
    conn.commit()
    conn.close()

    dsn = f"sqlite:///{db_path}"
    columns, rows = execute_sql(
        dsn=dsn,
        sql="SELECT channel, SUM(amount) AS sum_amount FROM orders GROUP BY channel",
        limit=100,
    )
    keys = {c["key"] for c in columns}
    assert "channel" in keys and "sum_amount" in keys
    by_channel = {r["channel"]: r["sum_amount"] for r in rows}
    assert by_channel["A"] == 150
    assert by_channel["B"] == 30


def test_execute_sql_enforces_limit(tmp_path):
    db_path = tmp_path / "phys2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE t (x INTEGER);"
        + "".join(f"INSERT INTO t VALUES ({i});" for i in range(10))
    )
    conn.commit()
    conn.close()
    _cols, rows = execute_sql(dsn=f"sqlite:///{db_path}", sql="SELECT x FROM t", limit=3)
    assert len(rows) == 3


def test_execute_sql_rejects_write(tmp_path):
    db_path = tmp_path / "phys3.db"
    sqlite3.connect(db_path).close()
    with pytest.raises(ExecutionError):
        execute_sql(dsn=f"sqlite:///{db_path}", sql="DROP TABLE t", limit=1)


def test_execute_sql_mapping(tmp_path):
    db_path = tmp_path / "phys4.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE physical_orders (chan TEXT, amt REAL);"
        "INSERT INTO physical_orders VALUES ('A', 10);"
    )
    conn.commit()
    conn.close()
    # 本体名 orders/channel/amount → 物理 physical_orders/chan/amt
    columns, rows = execute_sql(
        dsn=f"sqlite:///{db_path}",
        sql="SELECT channel, amount FROM orders",
        limit=10,
        mapping={
            "tables": {"orders": "physical_orders"},
            "columns": {"channel": "chan", "amount": "amt"},
        },
    )
    assert rows and rows[0]["chan"] == "A"


# ------------------------------------------------- published app 夹具


def _seed_published_app_with_source(dsn: str | None = None):
    """返回 (domain_id, app_id)。创建已发布数据应用（渠道汇总金额）。"""
    from app.services.data_app import DataAppService

    db = SessionLocal()
    try:
        domain = DomainContext(datahub_domain_id=f"urn:v1app:{uuid.uuid4()}", name="外部域")
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
            generated_by="llm",
        )
        db.add(ontology)
        db.flush()
        obj = ObjectType(ontology_id=ontology.id, name="orders", display_name="订单", status="published")
        db.add(obj)
        db.flush()
        channel = Property(object_type_id=obj.id, name="channel", display_name="渠道", semantic_type="category", status="published")
        amount = Property(object_type_id=obj.id, name="amount", display_name="金额", semantic_type="amount", status="published")
        db.add_all([channel, amount])
        db.commit()

        svc = DataAppService()
        data_source_id = None
        if dsn:
            ds = svc.create_data_source(db, name="phys", kind="sqlite", dsn_secret_ref=dsn)
            data_source_id = ds.id
        app = svc.create_app(
            db,
            domain_id=domain.id,
            app_type="data_table",
            name="渠道金额表",
            description=None,
            source="manual",
            spec=None,
            datasets=[
                {
                    "name": "渠道金额",
                    "primary_object_type_id": obj.id,
                    "data_source_id": data_source_id,
                    "binding": {
                        "primary_object_type_id": obj.id,
                        "measures": [{"ref": {"kind": "property", "id": amount.id, "name": "amount"}, "agg": "sum"}],
                        "dimensions": [{"kind": "property", "id": channel.id, "name": "channel", "display_name": "渠道"}],
                        "filters": [],
                        "row_limit": 100,
                    },
                }
            ],
        )
        svc.publish_app(db, app.id, version_comment="v1")
        return domain.id, app.id
    finally:
        db.close()


# ------------------------------------------------- 参数化筛选 / 下钻（runtime filters）


def test_preview_runtime_filter_real_source(client, admin_headers, tmp_path):
    db_path = tmp_path / "orders_rt.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE orders (channel TEXT, amount REAL);"
        "INSERT INTO orders VALUES ('A', 100), ('A', 50), ('B', 30);"
    )
    conn.commit()
    conn.close()

    _domain_id, app_id = _seed_published_app_with_source(dsn=f"sqlite:///{db_path}")
    # 取数据集 id
    detail = client.get(f"/api/data-apps/{app_id}", headers=admin_headers).json()
    ds_id = detail["datasets"][0]["id"]

    # 历史保存的 SQLite/业务源绑定不参与执行；未配置 Doris 时 fail-closed。
    res = client.post(
        f"/api/data-apps/{app_id}/datasets/{ds_id}/preview",
        headers=admin_headers,
        json={"limit": 100, "runtime_filters": []},
    )
    assert res.status_code == 200, res.text
    assert res.json()["rows"] == []
    assert res.json()["execution_blocked"] is True

    # 带下钻参数仍不得 fallback 到保存的 SQLite 数据源。
    res = client.post(
        f"/api/data-apps/{app_id}/datasets/{ds_id}/preview",
        headers=admin_headers,
        json={
            "limit": 100,
            "runtime_filters": [
                {"ref": {"kind": "property", "name": "channel"}, "op": "eq", "value": "A"}
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["rows"] == []
    assert res.json()["execution_blocked"] is True


def test_preview_runtime_filter_mock(client, admin_headers):
    _domain_id, app_id = _seed_published_app_with_source()
    detail = client.get(f"/api/data-apps/{app_id}", headers=admin_headers).json()
    ds_id = detail["datasets"][0]["id"]

    full = client.post(
        f"/api/data-apps/{app_id}/datasets/{ds_id}/preview",
        headers=admin_headers,
        json={"limit": 20, "runtime_filters": []},
    ).json()
    assert full["used_mock"] is False
    assert full["execution_blocked"] is True
    assert full["rows"] == []
    sample_channel = "A"

    drilled = client.post(
        f"/api/data-apps/{app_id}/datasets/{ds_id}/preview",
        headers=admin_headers,
        json={
            "limit": 20,
            "runtime_filters": [
                {"ref": {"kind": "property", "name": "channel"}, "op": "eq", "value": sample_channel}
            ],
        },
    ).json()
    assert drilled["execution_blocked"] is True
    assert drilled["rows"] == []
