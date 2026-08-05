"""G3：规约落库自治理 + 版本戳 + 存量 re-lint。

service 层用 SessionLocal 直连；API 层用 client（走 AdminAuthMiddleware）。每个写库用例
自清理 GovernanceStandardRecord，避免污染共享测试库（只登记 1.0.0==DEFAULT，即便残留也无害）。
"""

from __future__ import annotations

import pytest

from app.database import Base, SessionLocal, engine
from app.governance import active_standard
from app.governance.standard import DEFAULT_STANDARD
from app.models.governance import GovernanceStandardRecord
from app.services.governance_standard import GovernanceStandardService


@pytest.fixture
def db():
    # 服务层用例不经 app 启动，显式建表（create_all 幂等，只补缺表）。
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(GovernanceStandardRecord).delete()
        session.commit()
        session.close()


def test_get_active_falls_back_to_default_when_no_record(db):
    svc = GovernanceStandardService()
    assert svc.get_active(db) is DEFAULT_STANDARD
    # active_standard(db) 委托到 service，无记录时同样回默认
    assert active_standard(db) is DEFAULT_STANDARD


def test_publish_then_active_reflects_it(db):
    svc = GovernanceStandardService()
    rec = svc.publish(db, "1.0.0", note="首次发布")
    assert rec.status == "published"
    assert rec.activated_at is not None
    assert rec.payload_json  # 快照已落
    assert svc.active_version(db) == "1.0.0"
    assert active_standard(db).version == "1.0.0"


def test_publish_unknown_version_rejected(db):
    svc = GovernanceStandardService()
    with pytest.raises(ValueError):
        svc.publish(db, "9.9.9")


def test_publish_supersedes_previous(db):
    svc = GovernanceStandardService()
    svc.publish(db, "1.0.0")
    svc.publish(db, "1.0.0", note="再发一次")
    published = (
        db.query(GovernanceStandardRecord)
        .filter(GovernanceStandardRecord.status == "published")
        .all()
    )
    assert len(published) == 1  # 任一时刻至多一个 published
    assert len(svc.history(db)) == 2  # 审计留全


def test_available_versions_includes_default(db):
    svc = GovernanceStandardService()
    assert DEFAULT_STANDARD.version in svc.available_versions()


def test_relint_shape_on_empty_ontology(db):
    svc = GovernanceStandardService()
    out = svc.relint(db, "nonexistent-ontology-id")
    assert out["standard_version"] == DEFAULT_STANDARD.version
    assert out["table_count"] == 0
    assert out["violations"] == []


# ---------- API 冒烟 ----------


def test_api_get_active_standard(client, admin_headers):
    resp = client.get("/api/governance/standard", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_version"] == DEFAULT_STANDARD.version
    assert DEFAULT_STANDARD.version in body["available_versions"]
    assert "prompt_card" in body and body["prompt_card"]


def test_api_publish_and_history(client, admin_headers):
    try:
        resp = client.post(
            "/api/governance/standard/publish",
            headers=admin_headers,
            json={"version": "1.0.0", "note": "api 冒烟"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "published"

        hist = client.get("/api/governance/standard/history", headers=admin_headers)
        assert hist.status_code == 200
        assert any(r["version"] == "1.0.0" for r in hist.json())

        bad = client.post(
            "/api/governance/standard/publish",
            headers=admin_headers,
            json={"version": "0.0.0"},
        )
        assert bad.status_code == 400
    finally:
        s = SessionLocal()
        s.query(GovernanceStandardRecord).delete()
        s.commit()
        s.close()
