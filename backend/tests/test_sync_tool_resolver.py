"""搬运工具的自动决策 + ``/warehouse/sync-tools`` 端点。

工具已不再由物化弹窗逐次选（见 services/sync_tool_resolver 的模块注释）。这里测的是那个
决策本身，以及**它对外唯一的窗口**——那个端点。端点此前是 500（引用了签名里没有的 `db`），
前端 `.catch(() => null)` 把它吞成「工具列表为空」，故这里先钉住它必须 200。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.sync_tool_resolver import (
    SyncToolResolutionError,
    required_modes,
    resolve_sync_tool,
)


def _airflow(**over) -> SimpleNamespace:
    base = dict(sync_channel="runner", sync_tool="", sync_tool_images={})
    base.update(over)
    return SimpleNamespace(**base)


# ---------- 决策本身 ----------


def test_runner_channel_does_not_pin_a_tool():
    """runner 通道下 ontoMeta 不指定工具——档位由执行侧逐表自选，指定了也不作数。"""
    choice = resolve_sync_tool(_airflow(), modes=("full", "incremental"))
    assert choice.tool is None
    assert choice.auto is True
    assert choice.label == "auto"
    assert "逐表自选" in choice.detail


def test_runner_channel_says_a_pinned_tool_has_no_effect():
    """强制指定在 runner 通道下无效。默默照收会让人以为换工具能改变结果。"""
    choice = resolve_sync_tool(_airflow(sync_tool="datax"))
    assert choice.auto is False
    assert "不影响执行" in choice.detail


def test_docker_channel_picks_seatunnel_by_default():
    choice = resolve_sync_tool(_airflow(sync_channel="docker"), modes=("full",))
    assert choice.tool == "seatunnel"
    assert choice.auto is True


def test_docker_channel_skips_tools_without_an_image():
    """DataX 无官方镜像：没在设置页配过就不该被选中（选了必然 pull 失败）。"""
    choice = resolve_sync_tool(_airflow(sync_channel="docker"), modes=("full",))
    assert choice.tool != "datax"


def test_docker_channel_reports_modes_it_cannot_cover():
    """覆盖不全时如实说明，不静默降级——这些表会进 unsupported，得在提交前说清。"""
    choice = resolve_sync_tool(
        _airflow(sync_channel="docker", sync_tool="datax", sync_tool_images={"datax": "x/datax:1"}),
        modes=("full", "cdc"),
    )
    assert choice.tool == "datax"
    assert choice.uncovered_modes == ("cdc",)


def test_docker_channel_without_any_image_fails_loudly():
    """所有工具都没镜像 = 选不出来。必须在提交前失败，不能产一个注定 pull 失败的 DAG。"""
    import app.services.sync_tool_resolver as resolver

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(resolver, "available_sync_tools", lambda overrides=None: [])
        with pytest.raises(SyncToolResolutionError) as exc:
            resolve_sync_tool(_airflow(sync_channel="docker"))
    assert "没有任何搬运工具有可用镜像" in str(exc.value)


def test_unknown_pinned_tool_is_rejected_before_submit():
    with pytest.raises(SyncToolResolutionError):
        resolve_sync_tool(_airflow(sync_channel="docker", sync_tool="nosuchtool"))


def test_flink_cannot_be_pinned_as_a_sync_tool():
    """flink 已退出搬运：同步只走 seatunnel/datax，强指 flink 为搬运工具两条通道都拒。"""
    for channel in ("docker", "runner"):
        with pytest.raises(SyncToolResolutionError) as exc:
            resolve_sync_tool(_airflow(sync_channel=channel, sync_tool="flink"))
        assert "flink" in str(exc.value)


def test_docker_auto_never_picks_flink_even_with_an_image():
    """flink 有官方镜像，但已退出搬运：auto 只能落在 seatunnel/datax，绝不选 flink。"""
    import app.services.sync_tool_resolver as resolver

    # 只留 flink 可用（它默认有镜像）：剔除后无可用搬运工具，应显式报错而非选中 flink。
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(resolver, "available_sync_tools", lambda overrides=None: ["flink"])
        with pytest.raises(SyncToolResolutionError) as exc:
            resolve_sync_tool(_airflow(sync_channel="docker"), modes=("full",))
    assert "没有任何搬运工具" in str(exc.value)


def test_required_modes_dedups_and_defaults_to_full():
    contracts = [
        SimpleNamespace(load_strategy="incremental"),
        SimpleNamespace(load_strategy="INCREMENTAL"),
        SimpleNamespace(load_strategy=None),
    ]
    assert required_modes(contracts) == ("full", "incremental")


# ---------- 端点 ----------


def test_sync_tools_endpoint_returns_200(client, admin_headers):
    """回归：这个端点曾因签名缺 db 而 500，前端把它吞成「工具列表为空」。"""
    resp = client.get("/api/warehouse/sync-tools", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel"] in ("runner", "docker")
    # 工具列表保留作诊断与设置页强制指定用：镜像拿不到的标不可用，但**不从列表里删掉**。
    names = {t["name"] for t in body["tools"]}
    assert {"seatunnel", "datax", "flink"} <= names
    datax = next(t for t in body["tools"] if t["name"] == "datax")
    assert datax["available"] is False and datax["reason"]


def test_sync_tools_endpoint_explains_its_choice(client, admin_headers):
    """自动决策必须可解释——detail 是它唯一露出来的地方。"""
    body = client.get("/api/warehouse/sync-tools", headers=admin_headers).json()
    assert body["detail"]
    assert body["auto"] is True


def test_sync_tools_endpoint_returns_null_modes_when_runner_unreachable(
    client, admin_headers
):
    """问不到执行侧能力就回 null：宁可不置灰，也不凭工具适配器猜可用的装载方式。"""
    body = client.get(
        "/api/warehouse/sync-tools?engine=hive", headers=admin_headers
    ).json()
    # 测试环境没有 runner（默认通道是 runner 且未配 endpoint）。
    assert body["modes"] is None
    assert body["modes_detail"]
