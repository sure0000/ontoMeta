"""runner HTTP 接口：TestClient 端到端（healthz/capabilities/probe/jobs + 幂等）。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from sync_runner.app import app
from sync_runner.contract import CONTRACT_VERSION

client = TestClient(app)


def _seed_sqlite(tmp_path):
    src = f"sqlite:///{tmp_path}/s.db"
    tgt = f"sqlite:///{tmp_path}/t.db"
    se, te = create_engine(src), create_engine(tgt)
    with se.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS cust (id INTEGER, nm TEXT)"))
        if c.execute(text("SELECT count(*) FROM cust")).scalar() == 0:
            c.execute(text("INSERT INTO cust VALUES (1,'a'),(2,'b')"))
    with te.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS dim_cust (id INTEGER, name TEXT)"))
    se.dispose()
    te.dispose()
    return src, tgt


def _wait(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/jobs/{job_id}").json()
        if st["state"] in ("success", "failed"):
            return st
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def _submit(tmp_path, monkeypatch, key, mode="full"):
    src, tgt = _seed_sqlite(tmp_path)
    monkeypatch.setenv("SYNC_CONN_S_URL", src)
    monkeypatch.setenv("SYNC_CONN_T_URL", tgt)
    body = {
        "spec": {
            "name": "j",
            "source": {"alias": "s", "platform": "sqlite", "table": "cust"},
            "target": {"alias": "t", "platform": "sqlite", "table": "dim_cust"},
            "columns": [
                {"source": "id", "target": "id"},
                {"source": "nm", "target": "name"},
            ],
            "mode": mode,
        },
        "idempotency_key": key,
    }
    return client.post("/jobs", json=body).json()


def test_healthz_and_capabilities():
    assert client.get("/healthz").json()["contract_version"] == CONTRACT_VERSION
    caps = client.get("/capabilities").json()
    assert caps["contract_version"] == CONTRACT_VERSION
    assert "native" in caps["backends"]
    assert "sqlite" in caps["sources"] and "sqlite" in caps["sinks"]


def test_probe_missing_secret_is_unreachable():
    out = client.post("/probe", json={"alias": "nope_alias"}).json()
    assert out["reachable"] is False
    assert "SYNC_CONN_NOPE_ALIAS_URL" in out["detail"]


def test_probe_reachable_and_reads_table(tmp_path, monkeypatch):
    src, _ = _seed_sqlite(tmp_path)
    monkeypatch.setenv("SYNC_CONN_S_URL", src)
    out = client.post("/probe", json={"alias": "s", "table": "cust"}).json()
    assert out["reachable"] is True and out["can_read_table"] is True


def test_submit_runs_and_reports_rows(tmp_path, monkeypatch):
    r = _submit(tmp_path, monkeypatch, key="run1__taskA")
    assert r["state"] in ("queued", "running")
    st = _wait(r["job_id"])
    assert st["state"] == "success"
    assert st["rows_read"] == 2 and st["rows_written"] == 2
    assert st["target"] == "dim_cust"
    # 档位是 runner 逐表自选的，必须如实回报（契约 v3）：不然「这张表用了哪一档」
    # 只存在于 runner 的日志里，ontoMeta 侧无从对账。
    assert st["backend"] == "native"


def test_idempotent_key_returns_same_job(tmp_path, monkeypatch):
    r1 = _submit(tmp_path, monkeypatch, key="run2__taskB")
    _wait(r1["job_id"])
    r2 = _submit(tmp_path, monkeypatch, key="run2__taskB")
    assert r2["job_id"] == r1["job_id"]  # 同键不搬第二遍


def test_unsupported_combo_fails_loudly(tmp_path, monkeypatch):
    """native 搬不了的组合（oracle 源）显式失败，不静默降级。"""
    monkeypatch.setenv("SYNC_CONN_S_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.setenv("SYNC_CONN_T_URL", f"sqlite:///{tmp_path}/t.db")
    body = {
        "spec": {
            "name": "j",
            "source": {"alias": "s", "platform": "oracle", "table": "x"},
            "target": {"alias": "t", "platform": "sqlite", "table": "y"},
            "mode": "full",
        },
        "idempotency_key": "run3__taskC",
    }
    r = client.post("/jobs", json=body).json()
    st = _wait(r["job_id"])
    assert st["state"] == "failed"
    assert "native" in (st["error"] or "")


def test_get_unknown_job_404():
    assert client.get("/jobs/deadbeef").status_code == 404


def test_probe_reports_unprobeable_alias_distinctly(monkeypatch):
    """配了但没有可直连 URL 的别名（如 Hive 目标）不能报「连不通」。

    Hive 的数据由 Zeta 集群经 metastore/HDFS 写，runner 手上只有 metastore 地址。
    判成连不通会让人去修一个根本不存在的连接串——比不检查更糟。
    """
    monkeypatch.setenv("SYNC_CONN_ONTOMETA_DS_HIVE_DW_METASTORE_URI", "thrift://hms:9083")
    body = client.post("/probe", json={"alias": "ontometa_ds_hive_dw"}).json()
    assert body["reachable"] is False
    assert body["checkable"] is False
    assert "metastore_uri" in body["detail"]

    # 完全没配的别名仍然是「连不通」，两者不能混为一谈
    body = client.post("/probe", json={"alias": "nobody"}).json()
    assert body["reachable"] is False and body["checkable"] is True


# ---------- 连接配置（凭据只进不出） ----------


def test_secret_write_requires_token_and_is_disabled_without_one(tmp_path, monkeypatch):
    """没配 token 时写接口直接 403，而不是敞着让任何人改凭据。"""
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("SYNC_RUNNER_TOKEN", raising=False)
    r = client.put("/secrets/erp_readonly", json={"url": "sqlite:///x.db"})
    assert r.status_code == 403 and "SYNC_RUNNER_TOKEN" in r.json()["detail"]

    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    assert client.put("/secrets/erp_readonly", json={"url": "sqlite:///x.db"}).status_code == 401
    r = client.put(
        "/secrets/erp_readonly",
        json={"url": "sqlite:///x.db"},
        headers={"Authorization": "Bearer t0ken"},
    )
    assert r.status_code == 200


def test_saved_secret_is_usable_and_never_returned_in_clear(tmp_path, monkeypatch):
    """写进去能用（probe 通得过），但列出时机密键只报「已设置」。"""
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    auth = {"Authorization": "Bearer t0ken"}
    db = tmp_path / "src.db"
    with create_engine(f"sqlite:///{db}").begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
    client.put(
        "/secrets/erp_readonly",
        json={"url": f"sqlite:///{db}", "password": "s3cret", "metastore_uri": "thrift://h:9083"},
        headers=auth,
    )
    assert client.post("/probe", json={"alias": "erp_readonly"}).json()["reachable"] is True

    item = next(i for i in client.get("/secrets", headers=auth).json()["items"] if i["alias"] == "erp_readonly")
    assert item["source"] == "store"
    # 机密键不回明文；非机密键（地址）回明文，否则排查「连到哪去了」无从下手
    assert item["values"]["password"] == "<已设置>"
    assert "s3cret" not in str(item)
    assert item["values"]["metastore_uri"] == "thrift://h:9083"


def test_env_managed_alias_cannot_be_overwritten_by_api(tmp_path, monkeypatch):
    """环境变量提供的别名不接受写入——它优先级更高，静默覆盖会「保存成功但不生效」。"""
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    monkeypatch.setenv("SYNC_CONN_PINNED_URL", "sqlite:///pinned.db")
    auth = {"Authorization": "Bearer t0ken"}
    r = client.put("/secrets/pinned", json={"url": "sqlite:///other.db"}, headers=auth)
    assert r.status_code == 409 and "环境变量" in r.json()["detail"]
    # 但要列得出来，否则「没有这个别名」与「有但改不了」分不清
    item = next(i for i in client.get("/secrets", headers=auth).json()["items"] if i["alias"] == "pinned")
    assert item["source"] == "env"


def test_delete_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    auth = {"Authorization": "Bearer t0ken"}
    client.put("/secrets/tmp_alias", json={"url": "sqlite:///a.db"}, headers=auth)
    assert client.delete("/secrets/tmp_alias", headers=auth).json()["removed"] is True
    assert client.delete("/secrets/tmp_alias", headers=auth).json()["removed"] is False


def test_env_alias_is_split_on_known_field_not_last_underscore(tmp_path, monkeypatch):
    """别名和字段名里都可能有下划线，按最后一个下划线切会切出错误的别名。"""
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    monkeypatch.setenv("SYNC_CONN_ONTOMETA_DS_HIVE_DW_METASTORE_URI", "thrift://h:9083")
    items = {i["alias"]: i for i in client.get("/secrets", headers={"Authorization": "Bearer t0ken"}).json()["items"]}
    assert "ontometa_ds_hive_dw" in items
    assert items["ontometa_ds_hive_dw"]["values"] == {"metastore_uri": "thrift://h:9083"}
    assert "ontometa_ds_hive_dw_metastore" not in items


def test_password_embedded_in_url_is_not_returned(tmp_path, monkeypatch):
    """URL 里内嵌的密码不能回给调用方。

    只按键名判机密的话，``{"url": "mysql://root:pw@h/db"}`` 会被当成非机密整串回出去，
    密码就跟着漏进 UI——而连接串带密码恰恰是最常见的写法。
    """
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_RUNNER_TOKEN", "t0ken")
    auth = {"Authorization": "Bearer t0ken"}
    client.put(
        "/secrets/with_inline_pw",
        json={"url": "mysql+pymysql://root:hunter2@db.internal:3306/erp"},
        headers=auth,
    )
    body = client.get("/secrets", headers=auth).text
    assert "hunter2" not in body
    item = next(i for i in client.get("/secrets", headers=auth).json()["items"] if i["alias"] == "with_inline_pw")
    # 主机/库名仍要看得见，否则排查「连到哪去了」无从下手
    assert "db.internal:3306/erp" in item["values"]["url"]
