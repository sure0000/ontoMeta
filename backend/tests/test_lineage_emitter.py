"""M11 物化血缘兜底上报：计划构建、URN 一致性、updateLineage 请求体、幂等与容错。

核心断言：
1. 与执行侧同一份 URN——上游用 ``ObjectType.source_ref``，下游用 ``build_dataset_urn``，
   与 DAG outlets 注入的完全一致（插件与兜底 emitter 不能各连各的）。
2. updateLineage 请求体形状对：edgesToAdd 里 upstreamUrn/downstreamUrn 正确。
3. 重复上报幂等（同一份 URN，DataHub 侧对已存在的边不重复建）。
4. 单条失败逐条记录，不中断整体。
5. 字段级映射算进计划供审计，但表级上报只发表级边（不臆造 GraphQL 形状）。
"""

from __future__ import annotations

import asyncio

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
from app.services.lineage_emitter import DEFAULT_FABRIC, LineageEmitter
from app.services.materialization_contract import MaterializationContractService

_URN = "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.{table},PROD)"
_contracts = MaterializationContractService()
_emitter = LineageEmitter()


class _RecordingConnector:
    """记录 GraphQL 请求体，用于断言 mutation 结构。比照 test_datahub_writeback。"""

    def __init__(self, fail_on_target: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._fail_on_target = fail_on_target

    async def _graphql(self, query: str, variables: dict) -> dict:
        op = query.strip().split("(")[0].replace("mutation ", "").strip()
        self.calls.append((op, variables))
        if self._fail_on_target:
            edge = variables["input"]["edgesToAdd"][0]
            if self._fail_on_target in edge["downstreamUrn"]:
                raise RuntimeError("模拟 DataHub 拒绝")
        return {op: True}

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _init_db(client):
    return client


def _seed(tag: str) -> str:
    """客户（有列映射：customer_id ← cust_id）+ 订单（同名回退）。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:le-{tag}", name=f"le-{tag}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(onto)
        db.flush()

        customer = ObjectType(
            ontology_id=onto.id,
            name="customer",
            display_name="客户",
            table_role="business_object",
            source_ref=_URN.format(table="tab_customer"),
        )
        order = ObjectType(
            ontology_id=onto.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
            source_ref=_URN.format(table="tab_order"),
        )
        db.add_all([customer, order])
        db.flush()
        db.add_all(
            [
                Property(
                    object_type_id=customer.id,
                    name="customer_id",
                    display_name="客户ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                    source_field_ref=_URN.format(table="tab_customer") + "#cust_id",
                ),
                Property(
                    object_type_id=order.id,
                    name="sales_order_id",
                    display_name="订单ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
            ]
        )
        db.commit()
        ontology_id = onto.id

    with SessionLocal() as db:
        _contracts.sync(db, ontology_id)
    return ontology_id


def _plan(ontology_id: str, **kwargs):
    with SessionLocal() as db:
        return _emitter.build_plan(db, ontology_id, engine="hive", **kwargs)


# ---------- 计划构建 ----------


def test_edges_use_ontology_source_ref_as_upstream():
    plan = _plan(_seed("upstream"))
    upstreams = {e.source_urn for e in plan.applicable}
    assert _URN.format(table="tab_customer") in upstreams
    assert _URN.format(table="tab_order") in upstreams


def test_downstream_urn_matches_build_dataset_urn():
    """下游 URN 必须与执行侧（DAG outlets）走同一个 build_dataset_urn。"""
    plan = _plan(_seed("downstream"))
    cust = next(
        e for e in plan.applicable if e.target_table.split(".")[-1] == "customer"
    )
    expected = dh.build_dataset_urn("hive", cust.target_table, DEFAULT_FABRIC)
    assert cust.target_urn == expected
    assert cust.target_urn.startswith("urn:li:dataset:(urn:li:dataPlatform:hive,")
    assert cust.target_urn.endswith(f",{DEFAULT_FABRIC})")


def test_fine_grained_column_mapping_computed_but_not_emitted():
    """列映射（含 source_field_ref 的真实形态）算进计划供审计。"""
    plan = _plan(_seed("cols"))
    cust = next(
        e for e in plan.applicable if e.target_table.split(".")[-1] == "customer"
    )
    mapping = {c["target"]: c["source"] for c in cust.columns}
    # customer_id 的物理源列是 cust_id（从 schemaField 标识里取裸列名）
    assert mapping.get("customer_id") == "cust_id"
    assert plan.to_dict()["column_mappings"] >= 1


# ---------- updateLineage 请求体 ----------


def test_apply_sends_update_lineage_edges():
    conn = _RecordingConnector()
    with SessionLocal() as db:
        result = asyncio.run(
            _emitter.apply(db, _seed("apply"), engine="hive", connector=conn)
        )
    assert result["failed"] == 0
    assert result["applied"] == len(conn.calls) >= 2
    assert {op for op, _ in conn.calls} == {"updateLineage"}

    edge_call = next(v for _, v in conn.calls)
    added = edge_call["input"]["edgesToAdd"]
    assert len(added) == 1
    assert added[0]["upstreamUrn"].startswith("urn:li:dataset:")
    assert added[0]["downstreamUrn"].startswith("urn:li:dataset:")
    assert edge_call["input"]["edgesToRemove"] == []


def test_repeated_apply_is_idempotent_same_urns():
    """两次上报产同一份 URN——DataHub 侧对已存在的边不重复建。"""
    onto = _seed("idem")
    first = _RecordingConnector()
    second = _RecordingConnector()
    with SessionLocal() as db:
        asyncio.run(_emitter.apply(db, onto, engine="hive", connector=first))
    with SessionLocal() as db:
        asyncio.run(_emitter.apply(db, onto, engine="hive", connector=second))
    edges_of = lambda c: sorted(
        (
            v["input"]["edgesToAdd"][0]["upstreamUrn"],
            v["input"]["edgesToAdd"][0]["downstreamUrn"],
        )
        for _, v in c.calls
    )
    assert edges_of(first) == edges_of(second)


def test_single_failure_does_not_abort_batch():
    conn = _RecordingConnector(fail_on_target="dim.customer")
    with SessionLocal() as db:
        result = asyncio.run(
            _emitter.apply(db, _seed("fail"), engine="hive", connector=conn)
        )
    assert result["failed"] == 1
    assert result["applied"] >= 1
    assert result["errors"][0]["target"].split(".")[-1] == "customer"
