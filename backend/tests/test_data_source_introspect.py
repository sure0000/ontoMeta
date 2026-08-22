"""数据源内省（库/表列表）：物化弹窗据此选目标库、推荐表名。

用文件型 SQLite 做真实连接——内省走 SQLAlchemy Inspector，方言无关的行为可在此验证；
数仓引擎（Hive/Doris）的 Inspector 差异需活集群，不在单测覆盖范围。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.services.data_app_executor import ExecutionError, list_databases, list_tables


def _dsn(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'introspect.db'}"


def _seed_tables(dsn: str) -> None:
    engine = create_engine(dsn)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE dim_customer (id INTEGER, name TEXT)"))
        conn.execute(text("CREATE TABLE ods_order (id INTEGER)"))
        conn.execute(text("CREATE VIEW v_customer AS SELECT id FROM dim_customer"))


def test_list_databases_and_tables(tmp_path):
    dsn = _dsn(tmp_path)
    _seed_tables(dsn)

    assert "main" in list_databases(dsn)
    tables = list_tables(dsn)
    # 表与视图都要列出——物化覆盖写到一个视图名上会失败，用户需要看见它已被占用。
    assert tables == ["dim_customer", "ods_order", "v_customer"]


def test_list_tables_of_unknown_database_raises(tmp_path):
    """库不存在就如实报错（弹窗侧自行降级为「无已知表」），不返回空列表假装库是空的。"""
    dsn = _dsn(tmp_path)
    _seed_tables(dsn)
    with pytest.raises(ExecutionError):
        list_tables(dsn, "no_such_db")


def test_missing_driver_gives_actionable_error():
    """数仓驱动按需自装（见 requirements.txt）：没装时要说清装哪个包，而不是抛 500。"""
    try:
        import pymysql  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("环境已安装 pymysql，无法验证缺驱动路径")

    with pytest.raises(ExecutionError) as exc:
        list_databases("mysql+pymysql://u:p@127.0.0.1:3306/db")
    assert "pip install pymysql" in str(exc.value)


def _create_source(client, headers, **body) -> str:
    r = client.post("/api/data-sources", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_api_lists_databases_and_tables(client, admin_headers, tmp_path):
    dsn = _dsn(tmp_path)
    _seed_tables(dsn)
    ds_id = _create_source(
        client, admin_headers, name="内省源", kind="sqlite", dsn_secret_ref=dsn
    )

    dbs = client.get(f"/api/data-sources/{ds_id}/databases", headers=admin_headers)
    assert dbs.status_code == 200, dbs.text
    assert "main" in dbs.json()["databases"]

    tables = client.get(
        f"/api/data-sources/{ds_id}/tables",
        params={"database": "main"},
        headers=admin_headers,
    )
    assert tables.status_code == 200, tables.text
    assert "dim_customer" in tables.json()["tables"]


def test_api_rejects_source_without_connection(client, admin_headers):
    """mock 源无连接可内省：明确 400 报错，不返回空列表假装成功。"""
    ds_id = _create_source(client, admin_headers, name="样例源", kind="mock")
    r = client.get(f"/api/data-sources/{ds_id}/databases", headers=admin_headers)
    assert r.status_code == 400
    assert "Mock" in r.json()["detail"]


def test_dsn_components_echoes_password_plaintext():
    """DSN 里的密码现在明文回显（供前端 Input.Password 预填+眼睛切换），password_set 保留兼容。"""
    from app.services.data_app import DataAppService

    dsn = "postgresql+psycopg://alice:s3cr3t@db.example.com:5432/erp"
    comps = DataAppService._dsn_components("postgres", dsn)
    # 非机密连接字段原样
    assert comps["host"] == "db.example.com"
    assert comps["port"] == 5432
    assert comps["database"] == "erp"
    assert comps["username"] == "alice"
    # 密码明文回显 + set 标志
    assert comps["password"] == "s3cr3t"
    assert comps["password_set"] is True


def test_dsn_components_no_password_when_absent():
    """密码段缺失时 password 为 None、password_set=False（不发明文也不假报已设）。"""
    from app.services.data_app import DataAppService

    comps = DataAppService._dsn_components("postgres", "postgresql+psycopg://alice@db:5432/erp")
    assert comps["password"] is None
    assert comps["password_set"] is False
