"""MCP 血缘 / 落点 / 运行记录工具回归。

这三件补的是「已经发生过什么」。钉住四件最容易悄悄退化的事：

1. **加工血缘要能和业务关系分开**。``GraphEdge.structure_type`` 曾经从未被填过
   （两个构造点都漏了），于是 derivation 边和外键边在图上长得一模一样——
   下游把「外键指向客户」当成「数据来自客户」，影响面分析就全错了。
2. **空的已发布血缘图不等于没有血缘**。发布只提升业务对象，derivation 边常年停在
   草稿；不把「被过滤掉了多少条」说出来，调用方会把空图读成「这个对象没有上游」。
3. **没有落点登记就是没落地**，不能按命名规则推一个表名；主体不唯一时给候选，不猜。
4. **失败了却没有失败原因，要说破**。远端 Airflow/Flink 跑挂时投递回执自陈的是
   「投递成功」，沉默地少一个字段会让调用方要么当没事、要么自己编一个原因。
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
    RelationType,
)
from app.models.agent import ArtifactStatus, GovernanceArtifact

PUB = EntityStatus.PUBLISHED.value
DRAFT = EntityStatus.SUGGESTED.value


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(name: str, arguments: dict, role: str | None = "reader"):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(
            mcp_server,
            "resolve_auth_context",
            lambda: AuthContext(
                client_type="mcp_local", role=role, principal_name=f"mcp-{role}"
            ),
        )
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _call


def _body(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def ops_env(client):
    """ods_order --derivation--> dwd_order（草稿血缘）；order --fk--> customer（已发布业务关系）。"""
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mcpops-{uniq}", name=f"运维域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()

        def _obj(name, display, status=PUB):
            o = ObjectType(
                ontology_id=onto.id,
                name=f"{name}_{uniq}",
                display_name=display,
                table_role="business_object",
                status=status,
            )
            db.add(o)
            return o

        order = _obj("order", f"订单-{uniq}")
        customer = _obj("customer", f"客户-{uniq}")
        ods_order = _obj("ods_order", f"ODS订单-{uniq}", status=DRAFT)
        dwd_order = _obj("dwd_order", f"DWD订单-{uniq}", status=DRAFT)
        db.flush()

        db.add_all(
            [
                RelationType(
                    ontology_id=onto.id,
                    name=f"order_of_customer_{uniq}",
                    display_name="归属客户",
                    source_object_type_id=order.id,
                    target_object_type_id=customer.id,
                    cardinality="many_to_one",
                    structure_type="foreign_key",
                    status=PUB,
                ),
                # 加工血缘：DataHub ingest 落成 derivation 边，且常年停在草稿
                RelationType(
                    ontology_id=onto.id,
                    name=f"ods_to_dwd_{uniq}",
                    display_name="加工为",
                    source_object_type_id=ods_order.id,
                    target_object_type_id=dwd_order.id,
                    cardinality="one_to_one",
                    structure_type="derivation",
                    status=DRAFT,
                ),
            ]
        )
        logic = BusinessLogic(
            ontology_id=onto.id,
            name=f"gmv_{uniq}",
            display_name=f"成交额-{uniq}",
            logic_type="metric",
            expression_summary="订单金额求和",
            status=PUB,
        )
        db.add(logic)
        db.commit()
        return {
            "uniq": uniq,
            "ontology_id": onto.id,
            "order_id": order.id,
            "customer_id": customer.id,
            "ods_order_id": ods_order.id,
            "dwd_order_id": dwd_order.id,
            "logic_id": logic.id,
        }


# --------------------------------------------------------------------- get_lineage


def test_get_lineage_separates_derivation_from_business_relation(call_via_server, ops_env):
    """结构类型必须原样传到边上——两类边在图上长得一样，影响面分析就全错。"""
    body = _body(
        call_via_server(
            "get_lineage",
            {"center_id": ops_env["dwd_order_id"], "published_only": False},
        )
    )
    assert body["success"] is True
    upstream = body["data"]["direct_upstream"]
    assert [u["object_id"] for u in upstream] == [ops_env["ods_order_id"]]
    assert upstream[0]["is_derivation"] is True
    assert upstream[0]["structure_type"] == "derivation"
    assert body["metadata"]["derivation_edge_count"] == 1


def test_get_lineage_business_relation_is_not_derivation(call_via_server, ops_env):
    body = _body(call_via_server("get_lineage", {"center_id": ops_env["order_id"]}))
    downstream = body["data"]["direct_downstream"]
    assert [d["object_id"] for d in downstream] == [ops_env["customer_id"]]
    assert downstream[0]["is_derivation"] is False
    assert body["metadata"]["derivation_edge_count"] == 0


def test_get_lineage_reports_derivation_hidden_by_publish_gate(call_via_server, ops_env):
    """已发布视图里空 ≠ 没有血缘：得说出草稿里还压着多少条，否则等于报了个错答案。"""
    body = _body(call_via_server("get_lineage", {"center_id": ops_env["order_id"]}))
    meta = body["metadata"]
    assert meta["derivation_edge_count"] == 0
    assert meta["unpublished_derivation_edges"] >= 1
    assert "published_only=false" in meta["lineage_note"]


def test_get_lineage_rejects_unpublished_center_by_default(call_via_server, ops_env):
    result = call_via_server("get_lineage", {"center_id": ops_env["ods_order_id"]})
    body = _body(result)
    assert result.is_error is True
    assert "未发布" in body["error"]
    assert "published_only=false" in body["data"]["hint"]

    allowed = _body(
        call_via_server(
            "get_lineage",
            {"center_id": ops_env["ods_order_id"], "published_only": False},
        )
    )
    assert allowed["success"] is True


def test_get_lineage_mermaid_marks_derivation_edges(call_via_server, ops_env):
    body = _body(
        call_via_server(
            "get_lineage",
            {
                "center_id": ops_env["dwd_order_id"],
                "published_only": False,
                "include_mermaid": True,
            },
        )
    )
    mermaid = body["data"]["mermaid"]
    assert "==>" in mermaid  # 加工血缘用粗箭头
    assert ":::center" in mermaid


def test_get_lineage_rejects_unknown_center(call_via_server):
    result = call_via_server("get_lineage", {"center_id": "no-such-object"})
    assert result.is_error is True
    assert "不存在" in _body(result)["error"]


# --------------------------------------------------------------------- get_landing


def test_get_landing_says_not_landed_instead_of_guessing(call_via_server, ops_env):
    """没有登记就是没落地。这条挡住的是「照 ods_{域}_{表} 拼一个不存在的表名」。"""
    body = _body(
        call_via_server(
            "get_landing", {"target_kind": "object", "target_id": ops_env["order_id"]}
        )
    )
    assert body["success"] is True
    facts = {f["key"]: f["value"] for f in body["data"]["facts"]}
    assert facts["state"] == "not_landed"
    assert "不要按命名规则推测表名" in body["data"]["note"]
    assert body["metadata"]["resolved"] is True


def test_get_landing_returns_candidates_with_domain(call_via_server, ops_env):
    """跨本体同名很常见（odoo 和 erpnext 各有一个「公司」）：候选必须带数据域。"""
    body = _body(
        call_via_server(
            "get_landing", {"target_kind": "object", "keyword": "订单", }
        )
    )
    assert body["success"] is True
    candidates = body["data"].get("candidates") or []
    if len(candidates) == 1:
        pytest.skip("本次数据集里主体唯一，候选分支不适用")
    assert body["metadata"]["resolved"] is False
    assert all("domain_name" in item for item in candidates)


def test_get_landing_scopes_by_ontology(call_via_server, ops_env):
    body = _body(
        call_via_server(
            "get_landing",
            {
                "target_kind": "object",
                "keyword": f"订单-{ops_env['uniq']}",
                "ontology_id": ops_env["ontology_id"],
            },
        )
    )
    assert body["metadata"]["resolved"] is True
    assert body["data"]["target_id"] == ops_env["order_id"]


def test_get_landing_rejects_cross_ontology_target(call_via_server, ops_env):
    result = call_via_server(
        "get_landing",
        {
            "target_kind": "object",
            "target_id": ops_env["order_id"],
            "ontology_id": "another-ontology",
        },
    )
    assert result.is_error is True
    assert _body(result)["data"]["actual_ontology_id"] == ops_env["ontology_id"]


def test_get_landing_reads_logic_subject(call_via_server, ops_env):
    body = _body(
        call_via_server(
            "get_landing", {"target_kind": "logic", "target_id": ops_env["logic_id"]}
        )
    )
    assert body["success"] is True
    assert body["data"]["family"] == "landing"
    assert "ADS 落点" in (body["data"]["note"] or "")


def test_get_landing_needs_a_subject(call_via_server):
    result = call_via_server("get_landing", {"target_kind": "object"})
    assert result.is_error is True
    assert "keyword" in _body(result)["error"]


# ------------------------------------------------------------------ get_ops_record


@pytest.fixture
def failed_artifact(ops_env):
    """远端跑挂的任务：投递回执自陈「成功」，终态却是 failed。"""
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="sync",
            name=f"同步 · 远端失败-{ops_env['uniq']}",
            status=ArtifactStatus.FAILED.value,
            ontology_id=ops_env["ontology_id"],
            spec_json="{}",
            execution_receipt_json=json.dumps(
                {"dag_id": "ontometa_flink_x", "state": "queued", "ok": True}
            ),
        )
        db.add(artifact)
        db.commit()
        return artifact.id


def test_ops_record_task_run_flags_failure_without_reason(
    call_via_server, ops_env, failed_artifact
):
    body = _body(
        call_via_server(
            "get_ops_record",
            {
                "family": "task_run",
                "ontology_id": ops_env["ontology_id"],
                "artifact_id": failed_artifact,
            },
        )
    )
    facts = {f["key"]: f["value"] for f in body["data"]["facts"]}
    assert facts["status"] == "failed"
    assert "failure" not in facts  # 回执自陈「投递成功」，给不出原因
    assert body["metadata"]["failed_without_reason"] == [failed_artifact]
    assert "get_task_status" in body["metadata"]["hint"]


def test_ops_record_reads_global_families_without_ontology(call_via_server):
    body = _body(call_via_server("get_ops_record", {"family": "datasource"}))
    assert body["success"] is True
    assert body["metadata"]["scope"] == "global"


def test_ops_record_rejects_conversation_bound_family(call_via_server):
    """decision 族按会话组织；MCP 无会话，塞个假 id 就会读到别人的决策记录。"""
    result = call_via_server("get_ops_record", {"family": "decision"})
    body = _body(result)
    assert result.is_error is True
    assert "会话" in body["data"]["hint"]
    assert {f["key"] for f in body["data"]["available_families"]} >= {"task_run", "component"}


def test_ops_record_rejects_conversation_scope(call_via_server, ops_env):
    result = call_via_server(
        "get_ops_record",
        {"family": "task_run", "scope": "conversation", "ontology_id": ops_env["ontology_id"]},
    )
    assert result.is_error is True
    assert "conversation" in _body(result)["error"]


def test_ops_record_landing_family_points_at_get_landing(call_via_server):
    result = call_via_server("get_ops_record", {"family": "landing"})
    assert result.is_error is True
    assert "get_landing" in _body(result)["data"]["hint"]


def test_ops_record_ontology_scoped_family_needs_ontology(call_via_server):
    result = call_via_server("get_ops_record", {"family": "draft_run"})
    body = _body(result)
    assert result.is_error is True
    assert "ontology_id" in body["error"]
    assert "query_ontology" in body["data"]["hint"]


# -------------------------------------------------------------------- 授权门控


def test_ops_tools_require_reader(call_via_server, ops_env):
    for name, args in (
        ("get_lineage", {"center_id": ops_env["order_id"]}),
        ("get_landing", {"target_kind": "object", "target_id": ops_env["order_id"]}),
        ("get_ops_record", {"family": "datasource"}),
    ):
        denied = call_via_server(name, args, None)
        assert denied.is_error is True
        assert _body(denied)["metadata"]["denied"] is True

        allowed = call_via_server(name, args, "reader")
        assert _body(allowed)["success"] is True
