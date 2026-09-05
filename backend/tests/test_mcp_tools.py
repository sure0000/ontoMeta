"""MCP 工具集：注册表、各工具的真实行为、以及 stdio 服务器的回调层。

这批用例的由来：Phase 2 的工具最初是照着一份**想象的** ORM 写的（``ObjectType.role``、
``SyncTask``、``services.query_gateway`` 都不存在），导入就炸而无人发现——因为当时唯一
的「测试」是一个手跑的脚本。所以这里按真实 schema 播种、逐个工具调用一遍：字段名一旦
再漂，用例立刻红。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.mcp.tools import TOOL_REGISTRY, AuthContext
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.models.data_app import DataSource

AUTH = AuthContext(user_id=None, client_type="mcp_local")

# MCP 工具清单。少一个就是漏注册（注册靠 tools/__init__ 的 import 副作用，
# 新增模块忘了加进去时不会有任何报错，只是工具悄悄消失）。
EXPECTED_TOOLS = {
    "query_ontology",
    "query_objects",
    "query_object_detail",
    "query_relations",
    "list_datasources",
    "execute_sql",
    "validate_sql",
    "list_tasks",
    "get_task_status",
    "wait_task_status",
    "propose_sync",
    "propose_transform",
    "propose_materialize",
    "propose_metric",
    "draft_task",
    "validate_task",
    "confirm_task",
    "execute_task",
    "get_ontology_overview",
    "start_task_flow",
    "advance_task_flow",
}


def call(name: str, arguments: dict | None = None):
    tool = TOOL_REGISTRY[name]
    return asyncio.run(tool.execute(arguments or {}, AUTH))


@pytest.fixture
def seeded_ontology():
    """一个带对象/属性/关系的已发布本体。返回 (ontology_id, object_id, relation_id)。"""
    suffix = uuid4().hex[:8]
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"mcp-domain-{suffix}", name=f"MCP 测试域 {suffix}"
        )
        db.add(domain)
        db.flush()

        ontology = Ontology(
            domain_context_id=domain.id,
            version=1,
            status=OntologyStatus.PUBLISHED.value,
        )
        db.add(ontology)
        db.flush()

        customer = ObjectType(
            ontology_id=ontology.id,
            name=f"customer_{suffix}",
            display_name="客户",
            description="客户主数据",
            table_role="business_object",
            status="published",
            source_ref=(
                "urn:li:dataset:(urn:li:dataPlatform:mysql,"
                f"erp.tabCustomer_{suffix},PROD)"
            ),
        )
        order = ObjectType(
            ontology_id=ontology.id,
            name=f"order_{suffix}",
            display_name="订单",
            table_role="business_object",
            status="published",
            source_ref=(
                "urn:li:dataset:(urn:li:dataPlatform:mysql,"
                f"erp.tabOrder_{suffix},PROD)"
            ),
        )
        db.add_all([customer, order])
        db.flush()

        db.add(
            Property(
                object_type_id=customer.id,
                name="customer_id",
                display_name="客户编号",
                data_type="VARCHAR",
                semantic_type="identifier",
                status="published",
            )
        )
        relation = RelationType(
            ontology_id=ontology.id,
            name=f"order_belongs_to_customer_{suffix}",
            display_name="属于",
            source_object_type_id=order.id,
            target_object_type_id=customer.id,
            structure_type="reference",
            cardinality="many_to_one",
            status="published",
        )
        db.add(relation)
        db.commit()
        ids = (ontology.id, customer.id, relation.id, f"customer_{suffix}")
    finally:
        db.close()
    return ids


# --------------------------------------------------------------------------
# 注册表与 schema
# --------------------------------------------------------------------------


def test_all_phase2_tools_registered():
    assert EXPECTED_TOOLS <= set(TOOL_REGISTRY)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_schema_is_wellformed(name):
    """MCP 客户端拿 input_schema 当 JSON-Schema 用，写歪了模型就填不出参数。"""
    tool = TOOL_REGISTRY[name]
    assert tool.name == name
    assert tool.description.strip()

    schema = tool.input_schema
    assert schema["type"] == "object"
    props = schema.get("properties", {})
    assert isinstance(props, dict)
    # required 必须都在 properties 里，否则模型被要求填一个没定义的参数
    assert set(schema.get("required", [])) <= set(props)
    for spec in props.values():
        assert spec.get("type") in {
            "string", "integer", "number", "boolean", "object", "array",
        }
        assert spec.get("description")
    json.dumps(schema)  # 必须可序列化


# --------------------------------------------------------------------------
# 本体 / 对象 / 关系查询
# --------------------------------------------------------------------------


def test_query_ontology_finds_seeded(seeded_ontology):
    ontology_id, *_ = seeded_ontology
    result = call("query_ontology", {"ontology_id": ontology_id})
    assert result.success, result.error
    rows = result.data["ontologies"]
    assert [r["id"] for r in rows] == [ontology_id]
    assert rows[0]["published"] is True


def test_query_objects_returns_real_columns(seeded_ontology):
    """回归：曾按 ``ObjectType.role`` / ``source_table_name`` 查询，两列都不存在。"""
    ontology_id, object_id, _rel_id, object_name = seeded_ontology
    result = call("query_objects", {"ontology_id": ontology_id})
    assert result.success, result.error

    objects = result.data["objects"]
    assert {o["id"] for o in objects} >= {object_id}
    target = next(o for o in objects if o["id"] == object_id)
    assert target["name"] == object_name
    assert target["table_role"] == "business_object"
    # 派生字段必须由服务层带出来，绕开它直接查表就会恒为 "none"
    assert target["source_provenance"] == "datahub"
    assert result.metadata["total"] >= len(objects)


def test_query_objects_filters_by_role_and_search(seeded_ontology):
    ontology_id, _object_id, _rel_id, object_name = seeded_ontology
    hit = call(
        "query_objects",
        {"ontology_id": ontology_id, "role": "business_object", "search": object_name},
    )
    assert hit.success
    assert [o["name"] for o in hit.data["objects"]] == [object_name]

    miss = call("query_objects", {"ontology_id": ontology_id, "role": "bridge"})
    assert miss.success
    assert miss.data["objects"] == []


def test_query_object_detail_includes_properties_and_relations(seeded_ontology):
    _ontology_id, object_id, _rel_id, _name = seeded_ontology
    result = call("query_object_detail", {"object_id": object_id})
    assert result.success, result.error
    assert [p["name"] for p in result.data["properties"]] == ["customer_id"]
    assert result.metadata["incoming_relation_count"] == 1


def test_query_object_detail_missing_object_fails():
    result = call("query_object_detail", {"object_id": "does-not-exist"})
    assert not result.success
    assert "不存在" in result.error


def test_query_relations_returns_both_ends(seeded_ontology):
    """回归：曾按 ``source_object_id`` / ``relation_type`` 查询，真实列名是
    ``source_object_type_id`` / ``structure_type``。"""
    ontology_id, _object_id, relation_id, _name = seeded_ontology
    result = call("query_relations", {"ontology_id": ontology_id})
    assert result.success, result.error
    relation = next(r for r in result.data["relations"] if r["id"] == relation_id)
    assert relation["structure_type"] == "reference"
    assert relation["source_object_name"] and relation["target_object_name"]


# --------------------------------------------------------------------------
# 数据源目录
# --------------------------------------------------------------------------


def test_list_datasources_never_leaks_credentials(db):
    source = DataSource(
        name=f"mcp-src-{uuid4().hex[:8]}",
        kind="mysql",
        purpose="business_source",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://user:SECRET@host/db",
    )
    db.add(source)
    db.commit()

    result = call("list_datasources", {"purpose": "business_source"})
    assert result.success, result.error
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "SECRET" not in payload
    assert "dsn_secret_ref" not in payload
    assert source.id in {d["id"] for d in result.data["datasources"]}


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,valid",
    [
        ("SELECT id, name FROM customer WHERE id > 100", True),
        ("WITH t AS (SELECT 1 AS a) SELECT a FROM t", True),
        ("DROP TABLE customer", False),
        ("DELETE FROM customer", False),
        ("UPDATE customer SET name = 'x'", False),
        ("SELECT 1; SELECT 2", False),
        ("", False),
    ],
)
def test_validate_sql(sql, valid):
    result = call("validate_sql", {"sql": sql})
    assert result.success  # 校验工具本身总能给出结论
    assert result.data["valid"] is valid
    if not valid:
        assert result.data["reason"]


def test_execute_sql_rejects_writes_before_touching_db():
    result = call("execute_sql", {"sql": "DELETE FROM customer"})
    assert not result.success
    assert result.metadata["validation_error"] is True


def test_execute_sql_without_warehouse_fails_closed(db):
    """没有显式配置的默认 Doris 仓 → 明确拒绝，而不是猜一个数据源去跑。"""
    if (
        db.query(DataSource)
        .filter(DataSource.is_default_warehouse.is_(True))
        .first()
        is not None
    ):
        pytest.skip("本环境配了默认 Doris 仓，fail-closed 分支不适用")
    result = call("execute_sql", {"sql": "SELECT 1"})
    assert not result.success
    assert "默认 Doris" in result.error
    assert result.metadata["executed"] is False


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------


@pytest.fixture
def default_warehouse(db):
    """一个「启用的默认 Doris 仓」。

    ``is_default_warehouse`` 上有唯一偏索引（全库只能有一行为真），而整套用例共用
    同一个 SQLite 文件——所以这里能复用就复用，自己建的用完必须删掉，否则后面所有
    「没有默认仓」的用例都会被这一行污染。
    """
    existing = (
        db.query(DataSource)
        .filter(DataSource.is_default_warehouse.is_(True))
        .first()
    )
    if existing is not None:
        yield existing
        return

    warehouse = DataSource(
        name=f"mcp-doris-{uuid4().hex[:6]}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        is_default_warehouse=True,
        dsn_secret_ref="mysql+pymysql://u:p@127.0.0.1:9030/internal",
    )
    db.add(warehouse)
    db.commit()
    try:
        yield warehouse
    finally:
        db.delete(warehouse)
        db.commit()


@pytest.fixture
def seeded_task(seeded_ontology):
    ontology_id, *_ = seeded_ontology
    db = SessionLocal()
    try:
        artifact = GovernanceArtifact(
            kind="sync",
            name=f"同步 · MCP 测试 {uuid4().hex[:6]}",
            ontology_id=ontology_id,
            intent="把客户主数据同步进数仓",
            spec_json=json.dumps(
                {
                    "target_ods_database": "ods",
                    "target_ods_table": "ods_erp_customer",
                    "refresh_cron": "0 2 * * *",
                    "mode": "full",
                },
                ensure_ascii=False,
            ),
            status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json=json.dumps(
                {"ok": True, "rows": 42, "dag_id": "om_sync_demo"}, ensure_ascii=False
            ),
        )
        db.add(artifact)
        db.commit()
        ids = (artifact.id, ontology_id)
    finally:
        db.close()
    return ids


def test_list_tasks_projects_landing_and_receipt(seeded_task):
    artifact_id, ontology_id = seeded_task
    result = call("list_tasks", {"ontology_id": ontology_id})
    assert result.success, result.error
    task = next(t for t in result.data["tasks"] if t["id"] == artifact_id)
    assert task["kind"] == "sync"
    assert task["target"]["refresh_cron"] == "0 2 * * *"
    assert task["receipt_summary"]["rows"] == 42


def test_list_tasks_rejects_unknown_kind():
    result = call("list_tasks", {"kind": "backup"})
    assert not result.success
    assert "kind" in result.error


def test_get_task_status_reads_receipt(seeded_task):
    artifact_id, _ontology_id = seeded_task
    result = call("get_task_status", {"task_id": artifact_id, "include_spec": True})
    assert result.success, result.error
    assert result.data["status"] == ArtifactStatus.SUCCEEDED.value
    assert result.data["spec"]["mode"] == "full"
    assert result.metadata["state"]


def test_get_task_status_unknown_id_fails():
    result = call("get_task_status", {"task_id": "nope"})
    assert not result.success


# --------------------------------------------------------------------------
# 提案
# --------------------------------------------------------------------------


def test_propose_sync_reports_missing_context_with_candidates(seeded_ontology):
    """缺参不该等到用户点「去校验并执行」才在 Drafter 里炸——当场说清并附真实候选。"""
    ontology_id, *_ = seeded_ontology
    result = call(
        "propose_sync",
        {"intent": "同步客户主数据", "context": {"ontology_id": ontology_id}},
    )
    assert not result.success
    assert set(result.data["missing"]) == {
        "source_datasource_id",
        "target_datasource_id",
    }
    assert "source_datasource_id_options" in result.data
    assert "target_datasource_id_options" in result.data


def test_propose_requires_intent():
    result = call("propose_transform", {"context": {"ontology_id": "x"}})
    assert not result.success
    assert "intent" in result.error


def test_propose_sync_derives_ods_landing_from_drafter(
    seeded_ontology, default_warehouse, db
):
    """提案的 Spec 必须由真 Drafter 派生：ODS 落点、调度、装载方式都不是调用方说了算。"""
    ontology_id, _object_id, _rel_id, object_name = seeded_ontology
    suffix = uuid4().hex[:6]
    source = DataSource(
        name=f"mcp-erp-{suffix}",
        kind="mysql",
        purpose="business_source",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://u:p@h/erp",
    )
    db.add(source)
    db.commit()
    warehouse = default_warehouse

    result = call(
        "propose_sync",
        {
            "intent": "把客户主数据同步进数仓",
            "context": {
                "ontology_id": ontology_id,
                "object_type": object_name,
                "source_datasource_id": source.id,
                "target_datasource_id": warehouse.id,
                # 落点不给选：即便传进来也必须被 Drafter 的派生值覆盖
                "target_ods_table": "我说了算",
                "refresh_cron": "0 3 * * *",
            },
        },
    )
    assert result.success, result.error

    spec = result.data["proposal"]["spec"]
    assert spec["object_type"] == object_name
    assert spec["target_ods_table"] != "我说了算"
    assert spec["target_ods_table"].startswith("ods_")
    assert spec["refresh_cron"] == "0 3 * * *"
    assert spec["target_datasource_id"] == warehouse.id
    # 提案是只读的：不得落下任何制品
    assert (
        db.query(GovernanceArtifact)
        .filter(GovernanceArtifact.ontology_id == ontology_id)
        .count()
        == 0
    )
    # 调用方原样 POST 这份载荷即可落草稿
    assert result.data["draft_payload"]["kind"] == "sync"
    assert result.data["draft_payload"]["ontology_id"] == ontology_id
    assert "issues" in result.data["validation"]


def test_propose_sync_refuses_object_without_physical_source(
    seeded_ontology, default_warehouse, db
):
    """手工建模对象没有可搬的源，同步提案必须被拒。

    拒绝理由来自 ``_sync_context_errors``（源数据源与 source_ref 对不上）而不是 Drafter
    的「只能物化建表」——因为手工对象的 source_ref 里根本没有平台/库表可匹配，闸门在
    Drafter 之前就命中了。这与 chat_bi 的 propose_action 是同一顺序，此处钉住。
    """
    ontology_id, *_ = seeded_ontology
    suffix = uuid4().hex[:6]
    manual = ObjectType(
        ontology_id=ontology_id,
        name=f"manual_obj_{suffix}",
        display_name="人工建模对象",
        table_role="business_object",
        status="published",
        source_ref=f"manual:mysql:manual_obj_{suffix}",
    )
    source = DataSource(
        name=f"mcp-erp2-{suffix}", kind="mysql", purpose="business_source",
        enabled=True, dsn_secret_ref="mysql+pymysql://u:p@h/erp",
    )
    db.add_all([manual, source])
    db.commit()

    result = call(
        "propose_sync",
        {
            "intent": "同步人工建模对象",
            "context": {
                "ontology_id": ontology_id,
                "object_type": manual.name,
                "source_datasource_id": source.id,
                "target_datasource_id": default_warehouse.id,
            },
        },
    )
    assert not result.success
    assert "来源不匹配" in result.error


def test_propose_sync_surfaces_drafter_rejection(
    seeded_ontology, default_warehouse, db
):
    """Drafter 的拒绝理由要原样回给调用方，不被包装成一句泛泛的失败。"""
    ontology_id, *_ = seeded_ontology
    source = DataSource(
        name=f"mcp-erp3-{uuid4().hex[:6]}", kind="mysql", purpose="business_source",
        enabled=True, dsn_secret_ref="mysql+pymysql://u:p@h/erp",
    )
    db.add(source)
    db.commit()

    result = call(
        "propose_sync",
        {
            "intent": "同步一个不存在的对象",
            "context": {
                "ontology_id": ontology_id,
                "object_type": "no_such_object",
                "source_datasource_id": source.id,
                "target_datasource_id": default_warehouse.id,
            },
        },
    )
    assert not result.success
    assert "未在本体中找到匹配的对象" in result.error


# --------------------------------------------------------------------------
# stdio 服务器回调层
# --------------------------------------------------------------------------


def _server_module():
    pytest.importorskip("mcp", reason="未安装 MCP SDK（requirements 里的 mcp）")
    from app.mcp import server

    return server


def test_server_lists_every_registered_tool():
    server = _server_module()
    result = asyncio.run(server.handle_list_tools(None, None))
    listed = {t.name for t in result.tools}
    assert EXPECTED_TOOLS <= listed
    for tool in result.tools:
        assert tool.description
        assert tool.input_schema["type"] == "object"


def test_server_marks_tool_failure_as_error():
    """失败必须置 ``is_error``。

    只把错误写进 JSON 正文的话，客户端看到的是一次「成功」调用——模型得自己从文本里
    读出失败，重试与降级策略全部失灵。
    """
    server = _server_module()
    import mcp.types as types

    params = types.CallToolRequestParams(
        name="query_object_detail", arguments={"object_id": "does-not-exist"}
    )
    result = asyncio.run(server.handle_call_tool(None, params))
    assert result.is_error is True
    assert json.loads(result.content[0].text)["success"] is False


def test_server_rejects_unknown_tool():
    server = _server_module()
    import mcp.types as types

    params = types.CallToolRequestParams(name="no_such_tool", arguments={})
    result = asyncio.run(server.handle_call_tool(None, params))
    assert result.is_error is True
    assert "未知工具" in json.loads(result.content[0].text)["error"]


def test_server_returns_structured_content_on_success():
    server = _server_module()
    import mcp.types as types

    params = types.CallToolRequestParams(
        name="validate_sql", arguments={"sql": "SELECT 1"}
    )
    result = asyncio.run(server.handle_call_tool(None, params))
    assert result.is_error is False
    assert result.structured_content["data"]["valid"] is True


def test_tools_tolerate_bad_scalar_arguments():
    """客户端理应按 schema 校验类型，但工具不能把「理应」当前提。"""
    result = call("query_objects", {"ontology_id": "nope", "limit": "全部"})
    assert result.success
    assert result.metadata["count"] == 0

    tasks = call("list_tasks", {"limit": None})
    assert tasks.success
