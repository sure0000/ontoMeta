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
