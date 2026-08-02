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
