"""P1 建模工单基础测试。

测试覆盖：
- 工单创建与查询
- 规格版本管理
- 确认与拒绝
- Stale 检测
- 乐观锁
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.models import ModelingCase, ModelingCaseSpec
from app.schemas.modeling import (
    ModelingCaseCreate,
    ModelingCaseSpecConfirm,
    ModelingCaseSpecSave,
    RequirementSpec,
)
from app.services.modeling_case import ModelingCaseService


def test_create_modeling_case(client: TestClient, admin_headers):
    """测试创建建模工单。"""
    data = {
        "title": "销售履约分析",
        "primary_domain_id": "domain-sales",
        "domain_ids": ["domain-sales"],
    }
    
    response = client.post("/api/modeling-cases", json=data, headers=admin_headers)
    assert response.status_code == 200
    
    result = response.json()
    assert result["title"] == "销售履约分析"
    assert result["stage"] == "collecting_requirement"
    assert result["current_revision"] == 0


def test_save_and_confirm_requirement_spec(client: TestClient, admin_headers, db):
    """测试保存和确认需求规格。"""
    # 创建工单
    case_data = ModelingCaseCreate(
        title="订单分析",
        primary_domain_id="domain-order",
    )
    case = ModelingCaseService.create(db, case_data)
    
    # 保存需求 draft
    req_payload = {
        "business_goal": "降低订单延期率",
        "business_processes": ["订单履约"],
        "subjects": ["订单", "客户"],
        "questions": ["延期率是多少"],
        "delivery": ["dashboard"],
        "acceptance_criteria": ["延期率准确"],
    }
    
    response = client.post(
        f"/api/modeling-cases/{case.id}/specs/requirement",
        json={"payload": req_payload},
        headers=admin_headers,
    )
    assert response.status_code == 200
    
    spec = response.json()
    assert spec["kind"] == "requirement"
    assert spec["status"] == "draft"
    assert spec["revision"] == 1
    
    # 确认需求
    confirm_data = {
        "confirmed_by": "user-1",
        "content_hash": spec["content_hash"],
    }
    
    response = client.post(
        f"/api/modeling-cases/{case.id}/specs/requirement/{spec['revision']}/confirm",
        json=confirm_data,
        headers=admin_headers,
    )
    assert response.status_code == 200
    
    confirmed = response.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == "user-1"
    
    # 检查工单阶段推进
    response = client.get(f"/api/modeling-cases/{case.id}", headers=admin_headers)
    assert response.status_code == 200
    
    case_out = response.json()
    assert case_out["stage"] == "requirement_confirmed"


def test_spec_content_hash_idempotent(db):
    """测试相同内容的 spec 不创建新 revision。"""
    case_data = ModelingCaseCreate(title="Test Case")
    case = ModelingCaseService.create(db, case_data)
    
    payload = {"business_goal": "测试目标", "delivery": ["dashboard"]}
    save_data = ModelingCaseSpecSave(payload=payload)
    
    # 第一次保存
    spec1 = ModelingCaseService.save_spec(db, case.id, "requirement", save_data)
    assert spec1.revision == 1
    
    # 相同内容再次保存
    spec2 = ModelingCaseService.save_spec(db, case.id, "requirement", save_data)
    assert spec2.id == spec1.id
    assert spec2.revision == 1
    
    # 不同内容保存
    payload2 = {"business_goal": "新目标", "delivery": ["report"]}
    save_data2 = ModelingCaseSpecSave(payload=payload2)
    spec3 = ModelingCaseService.save_spec(db, case.id, "requirement", save_data2)
    assert spec3.revision == 2


def test_optimistic_lock(db):
    """测试确认时的乐观锁。"""
    case_data = ModelingCaseCreate(title="Lock Test")
    case = ModelingCaseService.create(db, case_data)
    
    payload = {"business_goal": "测试", "delivery": ["dashboard"]}
    save_data = ModelingCaseSpecSave(payload=payload)
    spec = ModelingCaseService.save_spec(db, case.id, "requirement", save_data)
    
    # 正确的 hash
    confirm_data = ModelingCaseSpecConfirm(
        confirmed_by="user-1",
        content_hash=spec.content_hash,
    )
    confirmed = ModelingCaseService.confirm_spec(
        db, case.id, "requirement", spec.revision, confirm_data
    )
    assert confirmed.status == "confirmed"
    
    # 尝试用错误 hash 确认另一个 draft
    payload2 = {"business_goal": "新目标", "delivery": ["report"]}
    save_data2 = ModelingCaseSpecSave(payload=payload2)
    spec2 = ModelingCaseService.save_spec(db, case.id, "requirement", save_data2)
    
    wrong_confirm = ModelingCaseSpecConfirm(
        confirmed_by="user-2",
        content_hash="wrong-hash",
    )
    
    with pytest.raises(ValueError, match="规格已变化"):
        ModelingCaseService.confirm_spec(
            db, case.id, "requirement", spec2.revision, wrong_confirm
        )


def test_stale_detection(db):
    """测试上游变化导致下游 stale。"""
    case_data = ModelingCaseCreate(title="Stale Test")
    case = ModelingCaseService.create(db, case_data)
    
    # 确认需求 v1
    req_payload = {"business_goal": "目标 v1", "delivery": ["dashboard"]}
    req_save = ModelingCaseSpecSave(payload=req_payload)
    req_spec = ModelingCaseService.save_spec(db, case.id, "requirement", req_save)
    req_confirm = ModelingCaseSpecConfirm(
        confirmed_by="user-1",
        content_hash=req_spec.content_hash,
    )
    ModelingCaseService.confirm_spec(
        db, case.id, "requirement", req_spec.revision, req_confirm
    )
    
    # 确认上下文（依赖需求）
    ctx_payload = {"ontologies": [], "data_sources": []}
    ctx_save = ModelingCaseSpecSave(payload=ctx_payload)
    ctx_spec = ModelingCaseService.save_spec(db, case.id, "context", ctx_save)
    
    # 检查 based_on
    assert ctx_spec.based_on_json is not None
    based_on = json.loads(ctx_spec.based_on_json)
    assert len(based_on) == 1
    assert based_on[0]["kind"] == "requirement"
    
    # 确认上下文
    ctx_confirm = ModelingCaseSpecConfirm(
        confirmed_by="user-1",
        content_hash=ctx_spec.content_hash,
    )
    ctx_confirmed = ModelingCaseService.confirm_spec(
        db, case.id, "context", ctx_spec.revision, ctx_confirm
    )
    
    # 修改需求 v2
    req_payload2 = {"business_goal": "目标 v2", "delivery": ["report"]}
    req_save2 = ModelingCaseSpecSave(payload=req_payload2)
    req_spec2 = ModelingCaseService.save_spec(db, case.id, "requirement", req_save2)
    req_confirm2 = ModelingCaseSpecConfirm(
        confirmed_by="user-1",
        content_hash=req_spec2.content_hash,
    )
    ModelingCaseService.confirm_spec(
        db, case.id, "requirement", req_spec2.revision, req_confirm2
    )
    
    # 检查上下文是否 stale
    stale_check = ModelingCaseService.check_stale(db, case.id, "context")
    assert stale_check["is_stale"] is True
    assert len(stale_check["stale_upstreams"]) == 1
    assert stale_check["stale_upstreams"][0]["kind"] == "requirement"
    
    # 验证状态已更新为 stale
    db.refresh(ctx_confirmed)
    assert ctx_confirmed.status == "stale"


def test_list_and_filter(client: TestClient, admin_headers, db):
    """测试列表与筛选。"""
    # 创建多个工单
    for i in range(3):
        case_data = ModelingCaseCreate(
            title=f"Case {i}",
            owner_subject_id=f"user-{i % 2}",
        )
        ModelingCaseService.create(db, case_data)
    
    # 全部列表
    response = client.get("/api/modeling-cases", headers=admin_headers)
    assert response.status_code == 200
    cases = response.json()
    assert len(cases) >= 3
    
    # 按 owner 筛选
    response = client.get(
        "/api/modeling-cases?owner_subject_id=user-0",
        headers=admin_headers,
    )
    assert response.status_code == 200
    cases = response.json()
    assert all(c["owner_subject_id"] == "user-0" for c in cases)


def test_rbac_enforcement(client: TestClient):
    """测试 RBAC 权限控制。"""
    # 无 token 访问
    response = client.get("/api/modeling-cases")
    assert response.status_code == 401
    
    # reader 可以 GET
    reader_headers = {"X-Admin-Token": "test-admin-token"}  # admin 作为 publisher 满足所有角色
    response = client.get("/api/modeling-cases", headers=reader_headers)
    assert response.status_code == 200
    
    # 创建需要 editor
    response = client.post(
        "/api/modeling-cases",
        json={"title": "Test"},
        headers=reader_headers,
    )
    # admin token 是 publisher，满足 editor 要求
    assert response.status_code == 200


__all__ = []
