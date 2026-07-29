"""M7 本体 → DataHub 回写：计划构建、安全约束、mutation 请求体。

三条安全约束优先级最高：
1. 只回写已发布本体（草稿命名会变，推上去会污染全域元数据）
2. 空值不覆盖 DataHub 已有内容（回写是补充语义，不是清空别人的成果）
3. 单条失败不中断整体，逐条记录便于重放
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.connectors import datahub as dh
from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.services.datahub_writeback import DataHubWritebackService

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp_ods.tab_customer,PROD)"
_DOMAIN_URN = "urn:li:domain:sales"


class _RecordingConnector:
    """记录 GraphQL 请求体，用于断言 mutation 结构。"""

    use_mock = False

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._fail_on = fail_on

    async def _graphql(self, query: str, variables: dict) -> dict:
        op = query.strip().split("(")[0].replace("mutation ", "").strip()
        self.calls.append((op, variables))
        if self._fail_on and self._fail_on in op:
            raise RuntimeError("模拟 DataHub 拒绝")
        return {op: True}

    async def aclose(self) -> None:
        pass


def _seed(status: str, *, with_description: bool = True, with_term: bool = False) -> str:
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=_DOMAIN_URN + tag, name=f"m7-{tag}")
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=status, version=1
        )
        db.add(onto)
        db.flush()
        customer = ObjectType(
            ontology_id=onto.id,
            name="customer",
            display_name="客户",
            description="客户主数据" if with_description else None,
            table_role="business_object",
            source_ref=_URN,
            canonical_term_id="urn:li:glossaryTerm:Customer" if with_term else None,
        )
        # 无 source_ref 的对象：无法定位 DataHub 数据集
        orphan = ObjectType(
            ontology_id=onto.id, name="orphan", display_name="孤儿表",
            table_role="business_object",
        )
        db.add_all([customer, orphan])
        db.flush()
        db.add_all([
            Property(object_type_id=customer.id, name="customer_id",
                     display_name="客户ID", data_type="bigint"),
            # 无业务语义的字段：不应产生回写
            Property(object_type_id=customer.id, name="col_x", data_type="varchar",
                     display_name=""),
        ])
        db.commit()
        return onto.id


# ---------- 安全约束 ----------


def test_draft_ontology_is_blocked(client):
    """草稿态命名仍会变动，绝不能推到 DataHub。"""
    plan = DataHubWritebackService().build_plan(
        SessionLocal(), _seed(OntologyStatus.DRAFT.value)
    )
    assert plan.blocked_reason and "仅已发布" in plan.blocked_reason
    assert plan.changes == []


def test_published_ontology_builds_plan(client):
    plan = DataHubWritebackService().build_plan(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value)
    )
    assert plan.blocked_reason is None
    ops = {c.operation for c in plan.applicable}
    assert ops == {"dataset_description", "domain", "field_description"}


def test_empty_value_never_overwrites(client):
    """本体没写业务语义时跳过——回写是补充，不是清空。"""
    plan = DataHubWritebackService().build_plan(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value, with_description=False)
    )
    desc = [c for c in plan.changes if c.operation == "dataset_description"]
    # display_name 仍在 → 有值可写
    assert any(c.will_apply for c in desc)
    # 无业务语义的字段不产生回写
    fields = [c for c in plan.changes if c.operation == "field_description"]
    assert all(".col_x" not in c.target for c in fields)


def test_object_without_source_ref_is_skipped_not_silent(client):
    plan = DataHubWritebackService().build_plan(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value)
    )
    orphan = [c for c in plan.changes if c.target == "orphan"]
    assert len(orphan) == 1
    assert orphan[0].will_apply is False
    assert "无 source_ref" in orphan[0].skipped_reason


def test_missing_ontology_raises(client):
    with pytest.raises(LookupError):
        DataHubWritebackService().build_plan(SessionLocal(), "nope")


# ---------- mutation 请求体 ----------


def test_apply_sends_expected_mutations(client):
    conn = _RecordingConnector()
    result = asyncio.run(DataHubWritebackService().apply(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value, with_term=True),
        connector=conn,
    ))
    assert result["failed"] == 0
    ops = [op for op, _ in conn.calls]
    assert "updateDescription" in ops
    assert "setDomain" in ops
    assert "addTerms" in ops

    desc_call = next(v for op, v in conn.calls if op == "updateDescription")
    assert desc_call["input"]["resourceUrn"] == _URN
    assert desc_call["input"]["description"] == "客户 · 客户主数据"

    field_call = next(
        v for op, v in conn.calls
        if op == "updateDescription" and v["input"].get("subResource")
    )
    assert field_call["input"]["subResource"] == "customer_id"
    assert field_call["input"]["subResourceType"] == "DATASET_FIELD"

    domain_call = next(v for op, v in conn.calls if op == "setDomain")
    assert domain_call["entityUrn"] == _URN
    assert domain_call["domainUrn"].startswith("urn:li:domain:")

    terms_call = next(v for op, v in conn.calls if op == "addTerms")
    assert terms_call["input"]["termUrns"] == ["urn:li:glossaryTerm:Customer"]


def test_single_failure_does_not_abort_batch(client):
    """单条失败逐条记录，其余照常回写——便于定位与重放。"""
    conn = _RecordingConnector(fail_on="setDomain")
    result = asyncio.run(DataHubWritebackService().apply(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value), connector=conn
    ))
    assert result["failed"] == 1
    assert result["applied"] >= 1
    assert result["errors"][0]["operation"] == "domain"
    assert "模拟 DataHub 拒绝" in result["errors"][0]["error"]


def test_mock_mode_makes_no_http_calls(client):
    """USE_MOCK_DATAHUB 下不得发起任何真实写入。"""

    class _MockConn(_RecordingConnector):
        use_mock = True

    conn = _MockConn()
    result = asyncio.run(DataHubWritebackService().apply(
        SessionLocal(), _seed(OntologyStatus.PUBLISHED.value), connector=conn
    ))
    assert conn.calls == []
    assert result["applied"] > 0
    assert result["mock"] is True


# ---------- API ----------


def test_plan_endpoint(client, admin_headers):
    ontology_id = _seed(OntologyStatus.PUBLISHED.value)
    body = client.get(
        f"/api/ontologies/{ontology_id}/datahub/writeback-plan", headers=admin_headers
    ).json()
    assert body["blocked_reason"] is None
    assert body["applicable"] > 0
    assert body["skipped"] >= 1  # orphan 表


def test_plan_endpoint_404(client, admin_headers):
    assert client.get(
        "/api/ontologies/nope/datahub/writeback-plan", headers=admin_headers
    ).status_code == 404


def test_writeback_requires_publisher(client, admin_headers):
    """回写会影响全域元数据，必须 publisher。"""
    created = client.post(
        "/api/principals", headers=admin_headers,
        json={"name": "m7-reviewer", "role": "reviewer"},
    ).json()
    resp = client.post(
        f"/api/ontologies/{_seed(OntologyStatus.PUBLISHED.value)}/datahub/writeback",
        headers={"X-Admin-Token": created["token"]}, json={},
    )
    assert resp.status_code == 403
    client.delete(f"/api/principals/{created['id']}", headers=admin_headers)


def test_writeback_endpoint_in_mock_mode(client, admin_headers):
    """测试环境 USE_MOCK_DATAHUB=true —— 端点可用且不发真实请求。"""
    ontology_id = _seed(OntologyStatus.PUBLISHED.value)
    body = client.post(
        f"/api/ontologies/{ontology_id}/datahub/writeback", headers=admin_headers, json={}
    ).json()
    assert body["mock"] is True
    assert body["failed"] == 0
