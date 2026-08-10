"""搬运工具决策与 ``/warehouse/sync-tools`` 端点——统一执行架构适配层测试。

统一执行架构下搬运工具恒为 Flink SQL on YARN(与 transform/metric 同一执行路径),
不再有 runner/docker 多通道或 seatunnel/datax 选择逻辑。``sync_tool_resolver``
已改为适配层,恒返回 flink + 支持 full/incremental/cdc 全集。

本测试验证适配层行为:恒定答案 + 端点可达 + modes 无过滤。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.sync_tool_resolver import (
    required_modes,
    resolve_sync_tool,
    engine_modes,
)


def _airflow(**over) -> SimpleNamespace:
    """模拟 Airflow settings(统一架构下这些参数已不影响结果)。"""
    base = dict(sync_channel="runner", sync_tool="", sync_tool_images={})
    base.update(over)
    return SimpleNamespace(**base)


# ---------- 统一执行架构:恒定答案 ----------


def test_resolve_always_returns_flink():
    """统一架构下搬运恒为 Flink SQL on YARN,不再有通道/工具选择。"""
    choice = resolve_sync_tool(_airflow(), modes=("full", "incremental"))
    assert choice.tool == "flink"
    assert choice.channel == "flink_on_yarn"
    assert choice.auto is True
    assert choice.label == "flink"
    assert "统一执行架构" in choice.detail
    assert choice.uncovered_modes == ()


def test_resolve_ignores_legacy_parameters():
    """airflow settings / forced tool 参数已不影响结果(为兼容保留签名)。"""
    choice1 = resolve_sync_tool(_airflow(sync_channel="docker", sync_tool="datax"))
    choice2 = resolve_sync_tool(_airflow(sync_channel="runner"), forced="seatunnel")
    assert choice1.tool == "flink" and choice2.tool == "flink"


def test_engine_modes_always_returns_full_set():
    """Flink SQL generator 支持所有引擎的 full/incremental/cdc,不再按工具能力过滤。"""
    for engine in ("hive", "postgres", "doris", "starrocks", "clickhouse"):
        modes, detail = engine_modes(_airflow(), engine)
        assert modes == ["full", "incremental", "cdc"]
        assert "Flink SQL" in detail


def test_required_modes_dedups_and_sorts():
    """required_modes 仍用于 preflight 汇总本批装载方式(不影响工具选择,仅统计)。"""
    contracts = [
        SimpleNamespace(load_strategy="incremental"),
        SimpleNamespace(load_strategy="INCREMENTAL"),
        SimpleNamespace(load_strategy="cdc"),
        SimpleNamespace(load_strategy=None),
    ]
    assert required_modes(contracts) == ("cdc", "full", "incremental")


# ---------- 端点 ----------


def test_sync_tools_endpoint_returns_200(client, admin_headers):
    """``/warehouse/sync-tools`` 端点可达,恒返回 flink_on_yarn 通道。"""
    resp = client.get("/api/warehouse/sync-tools", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel"] == "flink_on_yarn"
    assert body["auto"] is True
    assert body["detail"]


def test_sync_tools_endpoint_returns_all_modes(client, admin_headers):
    """统一架构下 modes 恒为 full/incremental/cdc 全集,不再按执行侧能力过滤。"""
    body = client.get(
        "/api/warehouse/sync-tools?engine=hive", headers=admin_headers
    ).json()
    assert body["modes"] == ["full", "incremental", "cdc"]
    assert "Flink SQL" in body["modes_detail"]
