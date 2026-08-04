"""Airflow 编排连接配置端点 + 物化运行状态端点。

凭据只回「是否已设 + 掩码」；``available`` 是「真的能用」（启用 + endpoint）而非仅「勾了启用」。
投递目录不再入设置（属部署基础设施，由 config 给默认）。
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
    assert body["api_version"] == "v1"


def test_enabled_with_endpoint_is_available(client, admin_headers):
    # 投递目录不再入设置（由 config 给默认），可用与否只看启用 + endpoint。
    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "enabled": True},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["available"] is True

    # 未启用 → 不可用，物化无法执行
    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "enabled": False},
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


def test_sync_tool_defaults_to_auto_and_rejects_unknown_names(client, admin_headers):
    """搬运工具的**唯一**人工入口（物化弹窗已不再逐次选）。空 = 自动。"""
    body = client.get("/api/settings/airflow", headers=admin_headers).json()
    assert body["sync_tool"] == ""

    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "sync_tool": "SeaTunnel"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["sync_tool"] == "seatunnel"  # 归一化，免得大小写造出两个事实

    # 名字写错要在这里就挡掉：否则物化会在提交时才报「未知搬运工具」。
    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "sync_tool": "nosuchtool"},
        headers=admin_headers,
    )
    assert r.status_code == 422


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
