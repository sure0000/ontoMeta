"""Superset 可视化工具：角色、口径闸门、以及"没配好"与"调用失败"要分开。

工具层最容易退化的两件事：

1. **写工具的角色掉到 reader**——建图会往外部系统写东西，reader 不该能做；
2. **把"Superset 没配"报成调用失败**——前者要把人指回设置页，后者才让人去查网络。
   两者混在一起时，用户会对着一个网络问题反复重试一个配置问题。

具体的编排决策在 ``test_superset_service.py``，图表形状在 ``test_superset_spec.py``，
这里只覆盖工具这一层。
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.services import superset_service as svc
from app.services.settings_service import SupersetRuntimeConfig

AUTH = AuthContext(user_id=None, client_type="mcp_local", role="publisher")

TOOLS = (
    "list_superset_datasets",
    "ensure_superset_dataset",
    "create_superset_chart",
    "update_superset_chart",
    "create_superset_dashboard",
    "list_superset_assets",
)


def call(name: str, arguments: dict | None = None):
    return asyncio.run(TOOL_REGISTRY[name].execute(arguments or {}, AUTH))


def test_write_tools_need_editor_reads_need_reader():
    """建图/建看板会往 Superset 写东西，reader 不该能做。"""
    assert tool_required_role(TOOL_REGISTRY["list_superset_datasets"]) == "reader"
    assert tool_required_role(TOOL_REGISTRY["list_superset_assets"]) == "reader"
    for name in (
        "ensure_superset_dataset",
        "create_superset_chart",
        "update_superset_chart",
        "create_superset_dashboard",
    ):
        assert tool_required_role(TOOL_REGISTRY[name]) == "editor", name


@pytest.mark.parametrize("name", TOOLS)
def test_not_configured_is_reported_as_a_configuration_problem(name, monkeypatch):
    """报错要带 gate=superset_not_configured：这样 Agent 知道该让人去设置页，而不是重试。"""

    def _boom(db):
        raise svc.SupersetNotConfigured("Superset 组件未启用")

    monkeypatch.setattr(svc, "runtime", _boom)
    # 给一份**合法**的规格：规格不合法会先被形状自检拦掉，那是另一条路径。
    result = call(
        name,
        {
            "dataset_ref": "obj:x@serving",
            "chart_id": 1,
            "name": "n",
            "viz": "bar",
            "dataset_id": 1,
            "dimensions": ["channel"],
            "metrics": [{"aggregate": "COUNT"}],
            "title": "t",
            "chart_ids": [1],
        },
    )
    assert result.success is False
    assert result.metadata.get("gate") == "superset_not_configured"


def test_bad_chart_shape_is_rejected_before_any_call(monkeypatch):
    """形状不对在编译器就拒了，不该先去 Superset 建一张打不开的图。"""
    called: list[str] = []
    monkeypatch.setattr(
        svc, "runtime", lambda db: (_ for _ in ()).throw(AssertionError("不该走到这里"))
    )
    result = call(
        "create_superset_chart",
        {"name": "指标卡", "viz": "kpi", "dataset_id": 1, "dimensions": ["channel"],
         "metrics": [{"aggregate": "COUNT"}]},
    )
    assert result.success is False
    assert "维度" in result.error
    assert called == []


def test_missing_dataset_ref_is_a_clear_error():
    result = call("ensure_superset_dataset", {})
    assert result.success is False
    assert "dataset_ref" in result.error


def test_missing_chart_id_on_update():
    result = call("update_superset_chart", {"name": "n", "viz": "bar", "dataset_id": 1})
    assert result.success is False
    assert "chart_id" in result.error


class _FakeClient:
    def __init__(self):
        self.charts: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        pass


def test_create_chart_passes_the_actor_through(monkeypatch):
    """建的人要留在登记簿里，否则"这张图是谁建的"没人答得上。"""
    cfg = SupersetRuntimeConfig(
        base_url="http://s:8088", username="u", password="p",
        database_id=1, public_base_url="http://s:8088", enabled=True,
    )
    seen: dict = {}
    monkeypatch.setattr(svc, "runtime", lambda db: cfg)
    monkeypatch.setattr(svc, "client", lambda cfg: _FakeClient())

    def _create(db, cfg, sc, spec, **kwargs):
        seen.update(kwargs)
        seen["viz"] = spec.viz
        return {"chart_id": 1, "url": "http://s:8088/explore/?slice_id=1", "viz": spec.viz}

    monkeypatch.setattr(svc, "create_chart", _create)
    auth = AuthContext(client_type="mcp_local", role="editor", principal_name="alice")
    result = asyncio.run(
        TOOL_REGISTRY["create_superset_chart"].execute(
            {
                "name": "各渠道订单量",
                "viz": "bar",
                "dataset_id": 7,
                "dimensions": ["channel"],
                "metrics": [{"aggregate": "SUM", "column": "amount"}],
                "dataset_ref": "obj:abc@serving",
            },
            auth,
        )
    )
    assert result.success is True
    assert seen["created_by"] == "alice"
    assert seen["dataset_ref"] == "obj:abc@serving"
    assert seen["viz"] == "bar"


def test_dashboard_reports_embed_unavailable_without_failing(monkeypatch):
    """没开嵌入不是建失败：看板可用，只是不能内嵌，回执要说清。"""
    cfg = SupersetRuntimeConfig(
        base_url="http://s:8088", username="u", password="p",
        database_id=1, public_base_url="http://s:8088", enabled=True,
    )
    monkeypatch.setattr(svc, "runtime", lambda db: cfg)
    monkeypatch.setattr(svc, "client", lambda cfg: _FakeClient())
    monkeypatch.setattr(
        svc,
        "create_dashboard",
        lambda *a, **k: {
            "dashboard_id": 3,
            "url": "http://s:8088/superset/dashboard/3/",
            "embedded_uuid": None,
            "embed_error": "HTTP 404",
            "chart_ids": [1],
            "asset_id": "x",
        },
    )
    result = call("create_superset_dashboard", {"title": "销售看板", "chart_ids": [1]})
    assert result.success is True
    assert result.metadata["embed_unavailable"] == "HTTP 404"
