"""Airflow 编排连接配置端点 + 物化运行状态端点。

凭据只回「是否已设 + 掩码」；``available`` 是「真的能用」（启用 + endpoint + SSH 主机）
而非仅「勾了启用」。Airflow 握着两条互不相干的连接（调度 API / DAG 投递 SSH），
故有两个独立的测试端点。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models.agent import GovernanceArtifact


@pytest.fixture(autouse=True)
def _reset_airflow(client, admin_headers):
    yield
    client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://localhost:8081", "enabled": False},
        headers=admin_headers,
    )


def test_defaults_to_disabled(client, admin_headers):
    body = client.get("/api/settings/airflow", headers=admin_headers).json()
    assert body["enabled"] is False
    assert body["available"] is False  # 没配就是不可用，物化会直接报错


def test_ineffective_fields_are_gone(client, admin_headers):
    """token / api_version / 私钥路径都不再是配置项——填了也不生效的东西不该占着表单。

    · token：Airflow REST 是 basic auth，没有任何部署路径产出过 bearer token；
    · api_version：由客户端 404 时自协商（见 connectors/airflow.py）；
    · ssh_key_path：那是 ontoMeta 主机上的文件，归该机 ~/.ssh/config 管，
      Web 表单里填一个别处的路径只会得到一个测不出真假的配置。
    """
    body = client.get("/api/settings/airflow", headers=admin_headers).json()
    for gone in ("token_set", "token", "api_version", "ssh_key_path"):
        assert gone not in body, gone


def test_ssh_delivery_tests_separately_from_the_api(client, admin_headers, monkeypatch):
    """两条连接分开测：SSH 通不通与调度 API 通不通是两件事，不该糊成一次拨测。"""
    # 缺主机/目录：直接说清缺什么，而不是去连一个空主机等超时
    r = client.post("/api/settings/airflow/test-ssh", headers=admin_headers)
    assert r.status_code == 400 and "SSH 主机" in r.json()["detail"]

    client.put(
        "/api/settings/airflow",
        json={
            "endpoint": "http://airflow:8080",
            "enabled": True,
            "ssh_host": "airflow-host",
            "ssh_user": "deploy",
            "dags_dir": "/opt/airflow/dags",
        },
        headers=admin_headers,
    )
    seen = {}

    def _fake_probe(cfg):
        seen["host"] = cfg.ssh_host
        seen["dags_dir"] = cfg.dags_dir
        return True, "airflow-host:/opt/airflow/dags 可写"

    monkeypatch.setattr(
        "app.services.materialize_preflight.probe_ssh_pipeline", _fake_probe
    )
    r = client.post("/api/settings/airflow/test-ssh", headers=admin_headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen == {"host": "airflow-host", "dags_dir": "/opt/airflow/dags"}


def test_enabled_with_endpoint_is_available(client, admin_headers):
    # 可用与否看启用 + endpoint + SSH 主机（投递通道只剩 SSH，没有主机就没法交 DAG）。
    r = client.put(
        "/api/settings/airflow",
        json={
            "endpoint": "http://airflow:8080",
            "enabled": True,
            "ssh_host": "airflow-host",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["available"] is True

    # 未启用 → 不可用，物化无法执行
    r = client.put(
        "/api/settings/airflow",
        json={
            "endpoint": "http://airflow:8080",
            "enabled": False,
            "ssh_host": "airflow-host",
        },
        headers=admin_headers,
    )
    assert r.json()["available"] is False

    # 启用 + endpoint 但没有 SSH 主机 → 仍不可用
    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "enabled": True, "ssh_host": ""},
        headers=admin_headers,
    )
    assert r.json()["available"] is False


def test_password_is_masked_and_preserved(client, admin_headers):
    client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "username": "admin", "password": "s3cret"},
        headers=admin_headers,
    )
    body = client.get("/api/settings/airflow", headers=admin_headers).json()
    assert body["password_set"] is True
    assert "s3cret" not in json.dumps(body)  # 明文绝不回传

    # 不传密码 = 保留原值，不会被清空
    client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "username": "admin"},
        headers=admin_headers,
    )
    assert client.get("/api/settings/airflow", headers=admin_headers).json()["password_set"]


def test_status_rejects_receipt_without_dagrun(client, admin_headers):
    """回执里没 DagRun 信息（如旧的直连回执/提交未成功）——明确报错，不返回空状态糊弄前端。"""
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="materialize",
            name="无 DagRun 回执",
            status="succeeded",
            execution_receipt_json=json.dumps({"ok": True}),
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    r = client.get(f"/api/warehouse/materialize/{artifact_id}/status", headers=admin_headers)
    assert r.status_code == 400
    assert "DagRun" in r.json()["detail"]


def test_status_404_for_unknown_artifact(client, admin_headers):
    r = client.get("/api/warehouse/materialize/no-such-id/status", headers=admin_headers)
    assert r.status_code == 404


def _artifact_with_batches(batches: list[dict]) -> str:
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="materialize",
            name="批次回执",
            status="succeeded",
            execution_receipt_json=json.dumps({"ok": True, "batches": batches}),
        )
        db.add(artifact)
        db.commit()
        return artifact.id


def test_task_result_reports_which_backend_moved_the_table(
    client, admin_headers, monkeypatch
):
    """runner 逐表自选档位，回执得说清这张表实际用了哪一档（契约 v3 的 backend）。"""
    artifact_id = _artifact_with_batches(
        [
            {"dag_id": "dag_a", "dag_run_id": "r1"},
            {"dag_id": "dag_b", "dag_run_id": "r2"},
        ]
    )

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        def get_xcom(self, dag_id, run_id, task_id, key="return_value"):
            # 第一批没有这个任务：端点要逐批找下去，而不是就此认输。
            if dag_id != "dag_b":
                return None
            return {"backend": "seatunnel", "job_id": "j1", "rows_written": 42}

        def close(self):
            pass

    # 端点在函数体内 import，故 patch 连接器模块上的名字即可拦到。
    monkeypatch.setattr("app.connectors.airflow.AirflowClient", _Fake)

    r = client.get(
        f"/api/warehouse/materialize/{artifact_id}/tasks/sync_dim_a/result",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "seatunnel"
    assert body["dag_id"] == "dag_b"
    assert body["rows_written"] == 42


def test_task_result_is_empty_not_error_when_task_has_no_xcom(
    client, admin_headers, monkeypatch
):
    """建表/切换任务不产 XCom，任务没跑完也没有值——这不是错误，别红一个空回执。"""
    artifact_id = _artifact_with_batches([{"dag_id": "dag_a", "dag_run_id": "r1"}])

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        def get_xcom(self, *a, **kw):
            return None

        def close(self):
            pass

    monkeypatch.setattr("app.connectors.airflow.AirflowClient", _Fake)

    r = client.get(
        f"/api/warehouse/materialize/{artifact_id}/tasks/create_tables/result",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["backend"] is None
