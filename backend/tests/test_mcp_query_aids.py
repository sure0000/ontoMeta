"""MCP 取数辅助（find_join_path / profile_values）回归。

两个工具堵的都是「SQL 语法完全合法、结果却是错的」那类失败，所以这里钉的也是那几条：

1. **找不到关联路径不是错误**。把它当错误回，模型下一步就自己编一个 JOIN——
   而「本体中这两个对象无从关联」本身就是可作答的结论。
2. **推不出 ON 就不给 sql_hint**。半截 SQL 比没有 SQL 更坏。
3. **会扇出要说出来**，并给出仍然安全的聚合，而不是让 SUM 被 JOIN 悄悄放大。
4. **profile_values 与 execute_sql 同价**。它读真实数据；写成 reader 就等于开了一个
   绕过 SQL 权限的后门——一次画像等于一句 SELECT DISTINCT。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import mcp.types as types
import pytest

from app.config import settings
from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.models import (
    DataSource,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)

PUB = EntityStatus.PUBLISHED.value


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(name: str, arguments: dict, role: str | None = "publisher"):
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
def aids_env(client):
    """order —FK→ customer（ON 可推）、order ↔ tag（N:N 会扇出）、island 孤立。"""
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:aids-{uniq}", name=f"取数域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()

        def _obj(name, display):
            o = ObjectType(
                ontology_id=onto.id, name=f"{name}_{uniq}", display_name=display,
                table_role="business_object", status=PUB,
            )
            db.add(o)
            return o

        order, customer, tag, island = (
            _obj("order", "订单"), _obj("customer", "客户"),
            _obj("tag", "标签"), _obj("island", "孤岛"),
        )
        db.flush()

        def _prop(obj, name, display, semantic, dtype):
            db.add(Property(
                object_type_id=obj.id, name=name, display_name=display,
                semantic_type=semantic, data_type=dtype, status=PUB,
            ))

        _prop(order, "customer_ref", "客户引用", "identifier", "bigint")
        _prop(order, "status", "状态", "categorical", "varchar")
        _prop(order, "amount", "金额", "measure", "decimal")
        _prop(customer, "cust_no", "客户编号", "identifier", "bigint")
        _prop(tag, "id", "ID", "identifier", "bigint")
        _prop(island, "id", "ID", "identifier", "bigint")
        db.flush()

        db.add_all([
            RelationType(
                ontology_id=onto.id, name=f"order_of_customer_{uniq}",
                display_name="归属客户", source_object_type_id=order.id,
                target_object_type_id=customer.id, cardinality="many_to_one",
                structure_type="foreign_key", status=PUB,
                # 证据里写清外键列，ON 才推得出来（列名不叫 <对象>_id，故必须靠证据）
                source_evidence=json.dumps(
                    {"foreign_key": "customer_ref", "target_field": "cust_no"}
                ),
            ),
            RelationType(
                ontology_id=onto.id, name=f"order_has_tag_{uniq}",
                display_name="订单打标", source_object_type_id=order.id,
                target_object_type_id=tag.id, cardinality="many_to_many",
                structure_type="foreign_key", status=PUB,
            ),
        ])
        db.commit()
        return {
            "uniq": uniq, "ontology_id": onto.id,
            "order": order.name, "order_id": order.id,
            "customer": customer.name, "tag": tag.name, "island": island.name,
        }


# ------------------------------------------------------------------ find_join_path


def test_find_join_path_gives_on_and_sql_hint(call_via_server, aids_env):
    body = _body(call_via_server(
        "find_join_path",
        {"from_object": aids_env["order"], "to_object": aids_env["customer"],
         "ontology_id": aids_env["ontology_id"]},
        "reader",
    ))
    assert body["success"] is True
    assert body["metadata"]["found"] == 1
    assert body["metadata"]["joinable"] is True
    path = body["data"]["paths"][0]
    hop = path["hops"][0]
    assert hop["on"] == f"{aids_env['order']}.customer_ref = {aids_env['customer']}.cust_no"
    assert path["sql_hint"].startswith(aids_env["order"])
    assert "JOIN" in path["sql_hint"]


def test_find_join_path_missing_path_is_a_conclusion_not_an_error(call_via_server, aids_env):
    """孤立对象没有路径——这是结论。报成错误，模型下一步就会自己编一个 JOIN。"""
    result = call_via_server(
        "find_join_path",
        {"from_object": aids_env["order"], "to_object": aids_env["island"],
         "ontology_id": aids_env["ontology_id"]},
        "reader",
    )
    body = _body(result)
    assert result.is_error is False
    assert body["success"] is True
    assert body["metadata"]["found"] == 0
    assert "不得自行构造 JOIN" in body["data"]["note"]


def test_find_join_path_reports_fanout_and_withholds_sql_hint(call_via_server, aids_env):
    """N:N 会放大度量；ON 推不出就不给 sql_hint——半截 SQL 比没有更坏。"""
    body = _body(call_via_server(
        "find_join_path",
        {"from_object": aids_env["order"], "to_object": aids_env["tag"],
         "ontology_id": aids_env["ontology_id"], "measure_object": aids_env["order"]},
        "reader",
    ))
    meta = body["metadata"]
    assert meta["found"] == 1
    assert "多对多" in meta["fanout_risk"]
    assert meta["safe_aggs"]  # COUNT(DISTINCT)/MIN/MAX 仍然安全
    assert body["data"]["paths"][0]["sql_hint"] is None


def test_find_join_path_resolves_ontology_from_object_id(call_via_server, aids_env):
    """不给 ontology_id 时从对象自己反查——绝不猜「当前锚定本体」。"""
    body = _body(call_via_server(
        "find_join_path",
        {"from_object": aids_env["order_id"], "to_object": aids_env["customer"]},
        "reader",
    ))
    assert body["success"] is True
    assert body["metadata"]["ontology_id"] == aids_env["ontology_id"]


def test_find_join_path_rejects_unknown_object(call_via_server, aids_env):
    result = call_via_server(
        "find_join_path",
        {"from_object": aids_env["order"], "to_object": "no_such_object",
         "ontology_id": aids_env["ontology_id"]},
        "reader",
    )
    assert result.is_error is True
    assert "to_object" in _body(result)["error"]


# ------------------------------------------------------------------- profile_values


def test_profile_values_is_priced_like_execute_sql():
    """写死 reader 就等于开了一个绕过 SQL 权限的后门。"""
    assert tool_required_role(TOOL_REGISTRY["profile_values"]) == settings.agent_run_sql_min_role
    assert tool_required_role(TOOL_REGISTRY["profile_values"]) == tool_required_role(
        TOOL_REGISTRY["execute_sql"]
    )


def test_profile_values_denied_below_sql_role(call_via_server, aids_env):
    result = call_via_server(
        "profile_values",
        {"object_id": aids_env["order_id"], "property": "status"},
        "reader",
    )
    assert result.is_error is True
    assert _body(result)["metadata"]["denied"] is True


def test_profile_values_without_warehouse_degrades(call_via_server, aids_env, monkeypatch):
    monkeypatch.setattr(
        "app.mcp.tools.query_aids.resolve_domain_data_source", lambda db: None
    )
    result = call_via_server(
        "profile_values", {"object_id": aids_env["order_id"], "property": "status"}
    )
    body = _body(result)
    assert result.is_error is False, "没有数据源是降级，不是故障"
    assert body["data"]["available"] is False
    assert "不得据此猜测字面量" in body["data"]["note"]
    assert body["metadata"]["reason"] == "no_warehouse"


def test_profile_values_reports_not_ready(call_via_server, aids_env):
    """数仓投影未就绪：如实说没有画像，而不是返回一份空取值让人当成「库里就是空的」。"""
    with SessionLocal() as db:
        db.add(DataSource(
            name=f"仓-{aids_env['uniq']}", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True,
            dsn_secret_ref="mysql+pymysql://u:p@127.0.0.1:9030/ods",
        ))
        db.commit()
    try:
        body = _body(call_via_server(
            "profile_values", {"object_id": aids_env["order_id"], "property": "status"}
        ))
        assert body["success"] is True
        assert body["data"]["available"] is False
        assert body["metadata"]["reason"] == "not_ready"
        assert body["data"]["note"]
    finally:
        with SessionLocal() as db:
            for row in db.query(DataSource).filter(
                DataSource.name == f"仓-{aids_env['uniq']}"
            ).all():
                db.delete(row)
            db.commit()


def test_profile_values_passes_doris_backend_and_scope_key(
    call_via_server, aids_env, monkeypatch
):
    """接线是最容易悄悄坏掉的部分：方言、dsn、mapping、缓存作用域一个都不能漏。

    scope_key 尤其重要——本体重新发布或换了数据源，同名字段的分布就不是同一回事，
    共用缓存键会把上一版的取值当成这一版的事实。
    """
    from app.services.query_routing import ObjectReadPreparation

    captured: dict = {}

    class _Source:
        id = "ds-1"
        name = "假仓"
        dsn_secret_ref = "doris://fake"

    monkeypatch.setattr(
        "app.mcp.tools.query_aids.resolve_domain_data_source", lambda db: _Source()
    )
    monkeypatch.setattr(
        "app.mcp.tools.query_aids.prepare_object_read",
        lambda db, **kw: ObjectReadPreparation(mapping={"projections": []}, blocked=None),
    )

    class _Profile:
        available = True
        strategy = "top_values"
        distinct_count = 3
        top_values = [{"value": "Completed", "freq": 3}]

        def to_dict(self):
            return {"available": True, "top_values": self.top_values}

    def _fake_profile(proj, obj, prop, **kwargs):
        captured.update(kwargs)
        return _Profile()

    monkeypatch.setattr("app.services.column_profiler.profile_property", _fake_profile)

    body = _body(call_via_server(
        "profile_values", {"object_id": aids_env["order_id"], "property": "status"}
    ))
    assert body["success"] is True
    assert body["data"]["top_values"][0]["value"] == "Completed"
    assert captured["backend"] == "doris"
    assert captured["dsn"] == "doris://fake"
    assert captured["mapping"] == {"projections": []}
    assert captured["scope_key"] == f"{aids_env['ontology_id']}|ds-1"


def test_profile_values_unknown_property_lists_candidates(call_via_server, aids_env):
    result = call_via_server(
        "profile_values", {"object_id": aids_env["order_id"], "property": "state"}
    )
    body = _body(result)
    assert result.is_error is True
    assert "status" in body["data"]["available_columns"]
