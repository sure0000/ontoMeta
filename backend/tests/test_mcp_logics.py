"""MCP 口径工具（search_logics / get_logic / compile_metric）回归。

这三件套补的是一条**断路**：``propose_metric`` 必填 ``business_logic_id``，而在此之前
MCP 侧没有任何办法查到口径 id——只能从对象详情里碰运气反查。

除了通路，这里还钉两件容易悄悄退化的事：
1. ``get_logic`` **必须**是精简投影。``BusinessLogicDetail`` 带着给编辑界面下拉框用的
   ``available_object_types`` / ``available_properties``，在真实 ERP 本体上是 MB 级——
   哪天有人把它改回原样 dump，MCP 会话会被一条工具结果打爆。
2. 编译失败要带 **code + hint** 回灌，而不是降级成「猜一段 SQL」——口径的意义就在这。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import mcp.types as types
import pytest

from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import AuthContext
from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)

PUB = EntityStatus.PUBLISHED.value


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


def _ctx(role: str | None) -> AuthContext:
    return AuthContext(client_type="mcp_local", role=role, principal_name=f"mcp-{role}")


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(name: str, arguments: dict, role: str | None = "reader"):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(mcp_server, "resolve_auth_context", lambda: _ctx(role))
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _call


def _body(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def logic_env(client):
    """一个已发布本体 + 三条口径：已形式化 / 只有文字 / 未发布。"""
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mcplogic-{uniq}", name=f"口径域-{uniq}"
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

        order = ObjectType(
            ontology_id=onto.id,
            name=f"order_{uniq}",
            display_name="订单",
            table_role="business_object",
            status=PUB,
        )
        db.add(order)
        db.flush()
        amount = Property(
            object_type_id=order.id,
            name="amount",
            display_name="金额",
            semantic_type="measure",
            data_type="decimal",
            status=PUB,
        )
        status_prop = Property(
            object_type_id=order.id,
            name="status",
            display_name="状态",
            semantic_type="categorical",
            data_type="varchar",
            status=PUB,
        )
        db.add_all([amount, status_prop])
        db.flush()

        def _ref(rid, prop_id, prop_name):
            return {
                "ref_id": rid,
                "object_type_id": order.id,
                "object_name": order.name,
                "object_display_name": "订单",
                "property_id": prop_id,
                "property_name": prop_name,
                "property_display_name": prop_name,
            }

        ast = {
            "type": "metric",
            "description": "订单金额求和",
            "refs": [_ref("r1", amount.id, "amount"), _ref("r2", status_prop.id, "status")],
            "body": {
                "operation": "sum",
                "args": [{"ref": "r1"}],
                "filter": {"left": {"ref": "r2"}, "op": "!=", "right": {"value": "Cancelled"}},
                "group_by": [],
                "window": None,
            },
        }
        formalized = BusinessLogic(
            ontology_id=onto.id,
            name=f"gmv_{uniq}",
            display_name=f"成交额-{uniq}",
            logic_type="metric",
            expression_summary="订单金额求和，排除已取消",
            expression_json=json.dumps(ast, ensure_ascii=False),
            status=PUB,
        )
        prose_only = BusinessLogic(
            ontology_id=onto.id,
            name=f"churn_{uniq}",
            display_name=f"流失率-{uniq}",
            logic_type="metric",
            expression_summary="上月活跃、本月未活跃的客户占比",
            status=PUB,
        )
        unpublished = BusinessLogic(
            ontology_id=onto.id,
            name=f"draft_{uniq}",
            display_name=f"草稿口径-{uniq}",
            logic_type="metric",
            status=EntityStatus.SUGGESTED.value,
        )
        db.add_all([formalized, prose_only, unpublished])
        db.commit()
        return {
            "uniq": uniq,
            "ontology_id": onto.id,
            "domain_context_id": domain.id,
            "object_name": order.name,
            "formalized_id": formalized.id,
            "prose_only_id": prose_only.id,
            "unpublished_id": unpublished.id,
        }


# ------------------------------------------------------------------ search_logics


def test_search_logics_returns_published_with_formalized_flag(call_via_server, logic_env):
    body = _body(
        call_via_server("search_logics", {"ontology_id": logic_env["ontology_id"]})
    )
    assert body["success"] is True
    by_id = {item["id"]: item for item in body["data"]["logics"]}
    # 默认 published_only：草稿口径不出现
    assert logic_env["unpublished_id"] not in by_id
    assert by_id[logic_env["formalized_id"]]["formalized"] is True
    # 只有文字口径的那条要如实报 false——compile/propose 都用不了它
    assert by_id[logic_env["prose_only_id"]]["formalized"] is False


def test_search_logics_matches_keyword(call_via_server, logic_env):
    body = _body(
        call_via_server(
            "search_logics",
            {"ontology_id": logic_env["ontology_id"], "search": f"成交额-{logic_env['uniq']}"},
        )
    )
    ids = [item["id"] for item in body["data"]["logics"]]
    assert ids == [logic_env["formalized_id"]]


def test_search_logics_can_include_drafts(call_via_server, logic_env):
    body = _body(
        call_via_server(
            "search_logics",
            {"ontology_id": logic_env["ontology_id"], "published_only": False},
        )
    )
    assert logic_env["unpublished_id"] in {item["id"] for item in body["data"]["logics"]}


def test_search_logics_omits_ontology_id_instead_of_lying(call_via_server, logic_env):
    """列表读模型没有 ontology_id：宁可不给这个键，也不能一律填 None。

    填 None 的话，调用方读到的是「这条口径不属于任何本体」——一个看起来有值的错答案，
    比缺字段难发现得多。
    """
    body = _body(
        call_via_server("search_logics", {"ontology_id": logic_env["ontology_id"]})
    )
    for item in body["data"]["logics"]:
        assert "ontology_id" not in item
        assert item["domain_context_id"] == logic_env["domain_context_id"]


def test_search_logics_type_filter_marks_page_scope(call_via_server, logic_env):
    body = _body(
        call_via_server(
            "search_logics",
            {"ontology_id": logic_env["ontology_id"], "logic_type": "rule"},
        )
    )
    assert body["data"]["logics"] == []
    # total 是**筛之前**的命中数；不标出来调用方会把它当成「rule 共有 N 条」
    assert body["metadata"]["type_filtered_within_page"] is True
    assert body["metadata"]["total"] >= 2


# --------------------------------------------------------------------- get_logic


def test_get_logic_returns_lean_projection(call_via_server, logic_env):
    result = call_via_server("get_logic", {"logic_id": logic_env["formalized_id"]})
    body = _body(result)
    data = body["data"]

    assert data["ontology_id"] == logic_env["ontology_id"]
    assert data["formalized"] is True
    assert data["expression_json"]["body"]["operation"] == "sum"
    assert [obj["name"] for obj in data["related_objects"]] == [logic_env["object_name"]]
    assert {p["name"] for p in data["related_properties"]} == {"amount", "status"}
    # 编辑界面的候选全集（真实本体上是 MB 级）绝不能出现在工具结果里
    assert "available_object_types" not in data
    assert "available_properties" not in data
    assert "version_records" not in data


def test_get_logic_missing_id_reports_error(call_via_server):
    result = call_via_server("get_logic", {"logic_id": "no-such-logic"})
    assert result.is_error is True
    assert "不存在" in _body(result)["error"]


# ---------------------------------------------------------------- compile_metric


def test_compile_metric_keeps_own_filter(call_via_server, logic_env):
    result = call_via_server("compile_metric", {"logic_id": logic_env["formalized_id"]})
    body = _body(result)
    assert body["success"] is True
    data = body["data"]
    assert data["compiled"] is True
    assert "SUM" in data["sql"].upper()
    # 口径自带的过滤条件不能在编译中丢失——丢了就是算错的数
    assert "Cancelled" in data["sql"]
    assert any("订单金额求和" in line for line in data["caliber_trace"])
    assert body["metadata"]["logic_type"] == "metric"
    assert body["metadata"]["dialect"] == "doris"


def test_compile_metric_applies_dimensions(call_via_server, logic_env):
    body = _body(
        call_via_server(
            "compile_metric",
            {"logic_id": logic_env["formalized_id"], "dimensions": ["status"]},
        )
    )
    assert body["success"] is True
    assert "GROUP BY" in body["data"]["sql"].upper()
    assert body["metadata"]["dimension_count"] == 1


def test_compile_metric_reports_unformalized_logic(call_via_server, logic_env):
    result = call_via_server("compile_metric", {"logic_id": logic_env["prose_only_id"]})
    body = _body(result)
    assert result.is_error is True
    assert body["success"] is False
    assert body["data"]["compiled"] is False
    assert body["data"]["code"] == "no_expression"
    # hint 带上文字口径，调用方才知道「这条要先形式化」而不是换个 SQL 重试
    assert body["data"]["hint"]["expression_summary"]


def test_compile_metric_rejects_unpublished_logic(call_via_server, logic_env):
    result = call_via_server(
        "compile_metric", {"logic_id": logic_env["unpublished_id"]}
    )
    body = _body(result)
    assert result.is_error is True
    assert body["data"]["code"] == "logic_not_found"


def test_compile_metric_reports_unknown_dimension(call_via_server, logic_env):
    body = _body(
        call_via_server(
            "compile_metric",
            {"logic_id": logic_env["formalized_id"], "dimensions": ["no_such_column"]},
        )
    )
    assert body["success"] is False
    assert body["data"]["code"]


# -------------------------------------------------------------------- 授权门控


def test_logic_tools_require_reader(call_via_server, logic_env):
    """三件套都只读，reader 即可；但无身份一律 fail-closed。"""
    for name, args in (
        ("search_logics", {}),
        ("get_logic", {"logic_id": logic_env["formalized_id"]}),
        ("compile_metric", {"logic_id": logic_env["formalized_id"]}),
    ):
        denied = call_via_server(name, args, None)
        assert denied.is_error is True
        assert _body(denied)["metadata"]["denied"] is True

        allowed = call_via_server(name, args, "reader")
        assert _body(allowed)["success"] is True
