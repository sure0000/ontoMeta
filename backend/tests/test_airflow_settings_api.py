"""Airflow 编排配置端点 + 物化运行状态端点。

凭据只回「是否已设 + 掩码」；``available`` 是「真的能用」而非「勾了启用」——
少了投递目录就编排不了，此时必须诚实报 false，否则物化会在提交阶段才炸。
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
    assert body["available"] is False  # 没配就是不可用，物化据此回落到 direct
    assert body["api_version"] == "v1"


def test_enabled_without_dirs_is_not_available(client, admin_headers, tmp_path):
    r = client.put(
        "/api/settings/airflow",
        json={"endpoint": "http://airflow:8080", "enabled": True},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    # 勾了启用但没有 DAG 投递目录 → 编排不了，如实报 false
    assert r.json()["available"] is False

    r = client.put(
        "/api/settings/airflow",
        json={
            "endpoint": "http://airflow:8080",
            "dags_dir": str(tmp_path / "dags"),
            "jobs_dir": str(tmp_path / "jobs"),
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert r.json()["available"] is True


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


def test_status_rejects_direct_mode_receipt(client, admin_headers):
    """直连执行没有 DagRun 可查——明确报错，不返回一个空状态糊弄前端。"""
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="materialize",
            name="直连物化",
            status="succeeded",
            execution_receipt_json=json.dumps({"execute_mode": "direct", "ok": True}),
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    r = client.get(f"/api/warehouse/materialize/{artifact_id}/status", headers=admin_headers)
    assert r.status_code == 400
    assert "直连" in r.json()["detail"]


def test_status_404_for_unknown_artifact(client, admin_headers):
    r = client.get("/api/warehouse/materialize/no-such-id/status", headers=admin_headers)
    assert r.status_code == 404
