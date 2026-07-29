"""M4 数仓方言接入：backend 识别、方言委托给 Adapter、Chat BI 执行端点。

关键约束：数仓引擎的方言翻译**必须委托给 app/warehouse 的 Adapter**，
不能在执行器里另开一套——否则同一引擎存在两份方言逻辑，迟早分叉。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import ChatBiConversation, ChatBiMessage, DataSource, DomainContext
from app.services import data_app_executor as ex
from app.warehouse import get_adapter


# ---------- backend 识别 ----------


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("sqlite:////tmp/a.db", "sqlite"),
        ("duckdb:///x", "duckdb"),
        ("postgresql+psycopg://u@h/db", "postgres"),
        ("mysql+pymysql://u@h/db", "mysql"),
        ("hive://h:10000/dwd_erp", "hive"),
        ("kyuubi://h:10009/dwd_erp", "kyuubi"),
        ("doris://u@h:9030/ads", "doris"),
        ("starrocks://u@h:9030/ads", "starrocks"),
        ("clickhouse+http://h:8123/ads", "clickhouse"),
    ],
)
def test_backend_detection(dsn, expected):
    assert ex._backend_of(dsn) == expected


def test_mysql_protocol_dsn_still_reads_as_mysql():
    """Doris/StarRocks 走 MySQL 线协议；写成 mysql:// 时无法区分，按 mysql 处理。"""
    assert ex._backend_of("mysql+pymysql://u@doris-host:9030/ads") == "mysql"


# ---------- 方言委托 ----------


def test_hive_dialect_delegates_to_adapter():
    sql = "SELECT * FROM t WHERE d > DATE_SUB(CURDATE(), INTERVAL 7 DAY)"
    assert ex._translate_dialect(sql, "hive") == get_adapter("hive").translate_sql(sql)
    assert "current_date()" in ex._translate_dialect(sql, "hive")


def test_kyuubi_reuses_hive_adapter():
    """Kyuubi 是 Spark SQL 网关，方言等同 Hive。"""
    sql = "SELECT CURDATE()"
    assert ex._translate_dialect(sql, "kyuubi") == ex._translate_dialect(sql, "hive")


def test_sqlite_branch_unchanged():
    """本地分析引擎的既有行为不能被改动。"""
    sql = "SELECT * FROM t WHERE d > DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
    assert ex._translate_dialect(sql, "sqlite") == (
        "SELECT * FROM t WHERE d > date('now','-30 day')"
    )


def test_unknown_backend_passes_through():
    sql = "SELECT CURDATE()"
    assert ex._translate_dialect(sql, "oracle") == sql


# ---------- 只读校验对数仓 SQL 同样生效 ----------


def test_read_only_guard_applies_to_warehouse_sql():
    ok, reason = ex.is_read_only("INSERT OVERWRITE TABLE dwd_erp.x SELECT 1")
    assert not ok and reason


def test_select_on_warehouse_table_passes():
    ok, _ = ex.is_read_only("SELECT customer_id FROM dim_erp.customer")
    assert ok


# ---------- Chat BI 执行端点 ----------


def _seed_message(tag: str, sql: str | None, *, dsn: str, kind: str = "duckdb") -> dict:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:m4-{tag}", name=f"m4-{tag}"
        )
        db.add(domain)
        db.flush()
        conv = ChatBiConversation(domain_id=domain.id, title="t")
        db.add(conv)
        db.flush()
        msg = ChatBiMessage(
            conversation_id=conv.id,
            role="assistant",
            content="answer",
            payload=json.dumps({"suggested_sql": sql}) if sql is not None else None,
        )
        source = DataSource(name=f"ds-{tag}", kind=kind, dsn_secret_ref=dsn)
        db.add_all([msg, source])
        db.commit()
        return {"message_id": msg.id, "data_source_id": source.id}


def test_execute_returns_rows(client, admin_headers, tmp_path):
    """端到端：payload 里的 suggested_sql 被真正执行并返回数据。"""
    db_file = tmp_path / "m4.db"
    import sqlite3

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE customer (customer_id INT, customer_name TEXT)")
    conn.execute("INSERT INTO customer VALUES (1,'甲'),(2,'乙')")
    conn.commit()
    conn.close()

    ids = _seed_message(
        "ok", "SELECT customer_id, customer_name FROM customer",
        dsn=f"sqlite:///{db_file}", kind="sqlite",
    )
    resp = client.post(
        f"/api/chat-bi/messages/{ids['message_id']}/execute",
        headers=admin_headers,
        json={"data_source_id": ids["data_source_id"], "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == 2
    assert {c["key"] for c in body["columns"]} == {"customer_id", "customer_name"}
    assert body["rows"][0]["customer_name"] == "甲"


def test_execute_rejects_message_without_sql(client, admin_headers):
    ids = _seed_message("nosql", None, dsn="sqlite:///:memory:", kind="sqlite")
    resp = client.post(
        f"/api/chat-bi/messages/{ids['message_id']}/execute",
        headers=admin_headers,
        json={"data_source_id": ids["data_source_id"]},
    )
    assert resp.status_code == 404
    assert "没有可执行的 SQL" in resp.json()["detail"]


def test_execute_rejects_mock_data_source(client, admin_headers):
    ids = _seed_message("mock", "SELECT 1", dsn="", kind="mock")
    resp = client.post(
        f"/api/chat-bi/messages/{ids['message_id']}/execute",
        headers=admin_headers,
        json={"data_source_id": ids["data_source_id"]},
    )
    assert resp.status_code == 404
    assert "未配置连接串" in resp.json()["detail"]


def test_execute_rejects_non_readonly_sql(client, admin_headers, tmp_path):
    """只读校验对 Chat BI 生成的 SQL 同样生效。"""
    db_file = tmp_path / "m4b.db"
    import sqlite3

    sqlite3.connect(db_file).close()
    ids = _seed_message(
        "write", "DELETE FROM customer", dsn=f"sqlite:///{db_file}", kind="sqlite"
    )
    resp = client.post(
        f"/api/chat-bi/messages/{ids['message_id']}/execute",
        headers=admin_headers,
        json={"data_source_id": ids["data_source_id"]},
    )
    assert resp.status_code == 400
    assert "只读校验" in resp.json()["detail"]


def test_execute_unknown_message_returns_404(client, admin_headers):
    ids = _seed_message("x", "SELECT 1", dsn="sqlite:///:memory:", kind="sqlite")
    resp = client.post(
        "/api/chat-bi/messages/does-not-exist/execute",
        headers=admin_headers,
        json={"data_source_id": ids["data_source_id"]},
    )
    assert resp.status_code == 404
