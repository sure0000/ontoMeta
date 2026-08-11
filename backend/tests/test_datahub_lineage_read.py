"""L2：DataHub 实时血缘读接口（get_lineage_around）单元测试。

钉住：GraphQL 请求体（含 fragment 变量作用域）、方向过滤（UPSTREAM/DOWNSTREAM/BOTH）、
不过滤 domain（跨域/外部表也返回）、查不到/网络失败返回空边不抛错、节点去重保序。
"""

from __future__ import annotations

import asyncio

import pytest

from app.connectors.datahub import (
    DataHubConnector,
    _parse_lineage_edges_from_entities,
)

_ODS = "urn:li:dataset:(urn:li:dataPlatform:hive,ods.brand,PROD)"
_DWD = "urn:li:dataset:(urn:li:dataPlatform:hive,dwd.brand,PROD)"
_ERP = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tab_brand,PROD)"


class _FakeConnector:
    """复用 DataHubConnector 的解析逻辑，但用假 GraphQL 响应。"""

    def __init__(self, response: dict | None = None, raise_on_call: bool = False):
        self.response = response
        self.raise_on_call = raise_on_call
        self.calls: list[tuple[str, dict]] = []

    async def _graphql(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        if self.raise_on_call:
            raise RuntimeError("网络故障")
        return self.response or {}


def _dataset(urn: str, *, up: list[str] = (), down: list[str] = ()) -> dict:
    def _rel(u: str) -> dict:
        return {"entity": {"urn": u, "type": "DATASET", "name": u.rsplit(",", 2)[-2]}}

    return {
        "urn": urn,
        "name": urn.rsplit(",", 2)[-2],
        "platform": {"name": "hive"},
        "upstreamLineage": {"relationships": [_rel(u) for u in up]},
        "downstreamLineage": {"relationships": [_rel(u) for u in down]},
    }


def test_parse_edges_includes_both_directions():
    """解析器同时吃 upstream/downstream，且保序去重。"""
    entity = _dataset(_DWD, up=[_ODS], down=[_ERP])
    edges = _parse_lineage_edges_from_entities([entity])
    assert len(edges) == 2
    # 两条边都在，方向正确（顺序：downstream 先扫，upstream 后扫）
    pairs = {(e.source_urn, e.target_urn) for e in edges}
    assert (_ODS, _DWD) in pairs  # upstream: ODS -> DWD
    assert (_DWD, _ERP) in pairs  # downstream: DWD -> ERP（本表被下游消费）


def test_parse_edges_dedupes():
    entity = _dataset(_DWD, up=[_ODS, _ODS])
    edges = _parse_lineage_edges_from_entities([entity])
    assert len(edges) == 1


def test_parse_edges_skips_non_dataset():
    """非 DATASET 实体（如 DataJob）不产生边。"""
    entity = {
        "urn": _DWD,
        "downstreamLineage": {
            "relationships": [
                {"entity": {"urn": "urn:li:dataJob:j1", "type": "DATA_JOB"}}
            ]
        },
    }
    edges = _parse_lineage_edges_from_entities([entity])
    assert edges == []


# --------------------------------------------------------------------------- 连接器


def test_get_lineage_around_sends_graphql_with_variables():
    conn = _FakeConnector(response={"dataset": _dataset(_DWD, up=[_ODS], down=[_ERP])})

    async def run():
        # 把假连接器的 _graphql 接到真连接器的方法上（鸭子类型）
        real = DataHubConnector.__new__(DataHubConnector)
        real._graphql = conn._graphql  # type: ignore[method-assign]
        return await real.get_lineage_around(_DWD, direction="BOTH", count=50)

    result = asyncio.run(run())
    query, variables = conn.calls[0]
    assert variables == {"urn": _DWD, "count": 50}
    assert "lineage(input: { direction: DOWNSTREAM" in query
    assert "lineage(input: { direction: UPSTREAM" in query
    assert result["center_urn"] == _DWD
    assert len(result["edges"]) == 2


def test_get_lineage_around_filters_by_direction():
    conn = _FakeConnector(response={"dataset": _dataset(_DWD, up=[_ODS], down=[_ERP])})

    async def run():
        real = DataHubConnector.__new__(DataHubConnector)
        real._graphql = conn._graphql  # type: ignore[method-assign]
        up = await real.get_lineage_around(_DWD, direction="UPSTREAM")
        down = await real.get_lineage_around(_DWD, direction="DOWNSTREAM")
        return up, down

    up, down = asyncio.run(run())
    assert [e["source_urn"] for e in up["edges"]] == [_ODS]
    assert [e["target_urn"] for e in down["edges"]] == [_ERP]


def test_get_lineage_around_includes_cross_domain():
    """不过滤 domain：跨域/外部表（mysql erp）也返回。"""
    conn = _FakeConnector(response={"dataset": _dataset(_DWD, up=[_ERP])})
    real = DataHubConnector.__new__(DataHubConnector)
    real._graphql = conn._graphql  # type: ignore[method-assign]

    result = asyncio.run(real.get_lineage_around(_DWD, direction="UPSTREAM"))
    assert result["edges"][0]["source_urn"] == _ERP  # 外部 mysql 表也在


def test_get_lineage_around_network_failure_returns_empty():
    """网络失败 → 返回空边，不抛错（血缘是增强）。"""
    conn = _FakeConnector(raise_on_call=True)
    real = DataHubConnector.__new__(DataHubConnector)
    real._graphql = conn._graphql  # type: ignore[method-assign]

    result = asyncio.run(real.get_lineage_around(_DWD))
    assert result["edges"] == []
    assert result["nodes"] == []


def test_get_lineage_around_missing_dataset_returns_empty():
    conn = _FakeConnector(response={})  # dataset 不存在
    real = DataHubConnector.__new__(DataHubConnector)
    real._graphql = conn._graphql  # type: ignore[method-assign]

    result = asyncio.run(real.get_lineage_around(_DWD))
    assert result["edges"] == []


def test_get_lineage_around_invalid_direction_raises():
    real = DataHubConnector.__new__(DataHubConnector)
    with pytest.raises(ValueError):
        asyncio.run(real.get_lineage_around(_DWD, direction="SIDEWAYS"))


def test_get_lineage_around_nodes_deduped_preserving_order():
    """nodes 去重保序：中心 + 边端点。"""
    entity = _dataset(_DWD, up=[_ODS], down=[_ODS])  # ODS 同时是上游和下游
    conn = _FakeConnector(response={"dataset": entity})
    real = DataHubConnector.__new__(DataHubConnector)
    real._graphql = conn._graphql  # type: ignore[method-assign]

    result = asyncio.run(real.get_lineage_around(_DWD))
    urns = [n["urn"] for n in result["nodes"]]
    assert urns == [_DWD, _ODS]  # 中心在前，ODS 只出现一次
