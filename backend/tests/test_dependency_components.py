"""基础设施组件是固定的那几样：不能增、不能删、不部署，只登记连接。

ontoMeta 连接的是已经跑着的服务，所以面板上没有「新增」这个动作——组件行由
``ensure_components`` 兜底补齐。这里钉住三件事：

1. 空库也能列出全部固定组件（否则面板是空的，而用户没有"新增"可点）；
2. MCP 借同一张表存运行期开关，但它不是外部服务，不该出现在基础设施面板里；
3. 创建/删除/部署/卸载这几条路由确实没了（留着就等于把部署选项藏起来而非去掉）。
"""

from __future__ import annotations

import pytest

from app.services import dependency_service as ds
from app.services.dependency_service import DependencyComponentService


@pytest.fixture
def svc():
    return DependencyComponentService()


def test_fixed_components_are_materialized_on_an_empty_table(svc, db):
    """空库也要能把固定组件列全——面板没有「新增」，缺的行只能由后端补。"""
    svc.ensure_components(db)
    keys = [r.key for r in svc.list_components(db)]
    assert keys == list(ds.COMPONENT_CATALOG)  # 顺序即面板显示顺序


def test_ensure_is_idempotent_and_keeps_what_is_configured(svc, db):
    """重复调用不能造出第二行，也不能把已填的连接冲掉。"""
    svc.ensure_components(db)
    svc.save_datahub(db, {"gms_url": "http://gms:8080", "frontend_url": "http://gms:9002"})
    svc.ensure_components(db)

    rows = [r for r in svc.list_components(db) if r.key == "datahub"]
    assert len(rows) == 1
    assert svc.get_datahub(db)["gms_url"] == "http://gms:8080"


def test_new_airflow_row_starts_disabled(svc, db):
    """补出来的 Airflow 行不能是启用态：一启用，物化就会真的往占位地址上投 DAG。"""
    svc.ensure_components(db)
    airflow = next(r for r in svc.list_components(db) if r.key == "airflow")
    assert airflow.enabled is False


def test_mcp_is_not_an_infrastructure_component(svc, db):
    """MCP 是 ontoMeta 自己的一层（配置在「Agent 接入」页），不进基础设施面板。"""
    svc.ensure_components(db)
    svc.save_mcp(db, {"mcp_http_enabled": True})

    assert "mcp" not in [r.key for r in svc.list_components(db)]
    assert "mcp" not in [c["key"] for c in svc.schema()["components"]]
    # 但它的配置照常读得回来——只是不在这张表上露出
    assert svc.get_mcp(db)["mcp_http_enabled"] is True


def test_schema_carries_no_deployment_vocabulary(svc):
    """schema 里不该再有部署方式/部署参数：那是前端渲染部署选项的唯一来源。"""
    schema = svc.schema()
    assert set(schema) == {
        "components",
        "connection_schemas",
        "connection_groups",
        "connection_statuses",
    }
    assert schema["connection_statuses"] == ["unknown", "connected", "failed"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/settings/dependencies"),
        ("delete", "/api/settings/dependencies/whatever"),
        ("post", "/api/settings/dependencies/whatever/deploy"),
        ("post", "/api/settings/dependencies/whatever/teardown"),
        ("post", "/api/settings/llm-services"),
        ("delete", "/api/settings/llm-services/whatever"),
    ],
)
def test_add_and_deploy_routes_are_gone(client, admin_headers, method, path):
    kwargs = {"json": {}} if method == "post" else {}
    resp = getattr(client, method)(path, headers=admin_headers, **kwargs)
    assert resp.status_code in (404, 405), resp.text
