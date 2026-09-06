"""口径创作 / 接数据 / 建模工单三族 MCP 工具的回归。

这三族是为「删掉 Data Agent」补齐的能力面。它们各有一条**错了不报错**的边界，
所以这里钉的不是「能调通」，而是那几条边界：

1. **编译不过就不许落库**。放手让模型写表达式的前提是编译器把关，不是提示词。
2. **凭据永远不经过 agent**。入参里出现口令要被丢弃并如实回报，不是默默忽略。
3. **重跑本体草稿不是原地覆盖**。已有发布本体时必须先让人知道后果。
4. **确认要绑到被审查的那一版**。content_hash 是乐观锁，不是可有可无的入参。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import mcp.types as types
import pytest

from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.models import (
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


def _call_tool(name: str, arguments: dict, role: str = "publisher"):
    tool = TOOL_REGISTRY[name]
    auth = AuthContext(client_type="mcp_local", role=role, principal_name=f"mcp-{role}")
    return asyncio.run(tool.execute(arguments, auth))


@pytest.fixture
def authoring_env(client):
    """一个已发布本体：订单（金额 measure / 状态 categorical）。"""
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:auth-{uniq}", name=f"口径域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
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
        db.add_all(
            [
                Property(
                    object_type_id=order.id, name="amount", display_name="金额",
                    semantic_type="measure", data_type="decimal", status=PUB,
                ),
                Property(
                    object_type_id=order.id, name="status", display_name="状态",
                    semantic_type="categorical", data_type="varchar", status=PUB,
                ),
            ]
        )
        db.commit()
        return {
            "uniq": uniq,
            "domain_id": domain.id,
            "ontology_id": onto.id,
            "order": order.name,
        }


# ---------------------------------------------------------------- 口径创作


def test_compile_logic_expression_does_not_write(authoring_env):
    result = _call_tool(
        "compile_logic_expression",
        {
            "display_name": f"订单总额-{authoring_env['uniq']}",
            "logic_type": "metric",
            "ontology_id": authoring_env["ontology_id"],
            "fields": [{"alias": "amt", "object": authoring_env["order"], "property": "amount"}],
            "body": {"operation": "SUM", "args": [{"ref": "amt"}]},
        },
        role="reader",
    )
    assert result.success, result.error
    assert result.data["compiled_sql"]
    assert result.data["caliber_trace"]
    # 编译成功 ≠ 已落库。把这一位丢掉，「已编译通过」就会被转述成「指标已建好」。
    assert result.metadata["written"] is False

    from app.models import BusinessLogic

    with SessionLocal() as db:
        assert (
            db.query(BusinessLogic)
            .filter(BusinessLogic.display_name == f"订单总额-{authoring_env['uniq']}")
            .first()
            is None
        )


def test_compile_returns_fixable_signal_on_unknown_field(authoring_env):
    """编不过要把「有哪些字段可用」带回去——只回一句失败，下一步就只能靠猜。"""
    result = _call_tool(
        "compile_logic_expression",
        {
            "display_name": "瞎编指标",
            "logic_type": "metric",
            "ontology_id": authoring_env["ontology_id"],
            "fields": [
                {"alias": "x", "object": authoring_env["order"], "property": "并不存在的列"}
            ],
            "body": {"operation": "SUM", "args": [{"ref": "x"}]},
        },
        role="reader",
    )
    assert not result.success
    assert result.metadata.get("code")
    assert result.metadata.get("validation_error") is True


def test_create_logic_refuses_to_write_when_expression_fails(authoring_env):
    """带表达式建口径时，编不过就不许落库——守卫是编译器，不是提示词。"""
    from app.models import BusinessLogic

    result = _call_tool(
        "create_logic",
        {
            "display_name": f"编不过的指标-{authoring_env['uniq']}",
            "logic_type": "metric",
            "domain_id": authoring_env["domain_id"],
            "fields": [{"alias": "s", "object": authoring_env["order"], "property": "status"}],
            # 对 categorical 字段求和：语义类型不允许，编译器会拒
            "body": {"operation": "SUM", "args": [{"ref": "s"}]},
        },
    )
    assert not result.success
    with SessionLocal() as db:
        assert (
            db.query(BusinessLogic)
            .filter(BusinessLogic.display_name == f"编不过的指标-{authoring_env['uniq']}")
            .first()
            is None
        )


def test_create_logic_without_expression_requires_description(authoring_env):
    """只给名字的口径等于没有口径——description 是它唯一承载含义的字段。"""
    result = _call_tool(
        "create_logic",
        {
            "display_name": "光有名字",
            "logic_type": "metric",
            "domain_id": authoring_env["domain_id"],
        },
    )
    assert not result.success
    assert "description" in result.error


def test_create_logic_writes_a_draft_with_compiled_expression(authoring_env):
    result = _call_tool(
        "create_logic",
        {
            "display_name": f"订单总额-{authoring_env['uniq']}",
            "logic_type": "metric",
            "domain_id": authoring_env["domain_id"],
            "fields": [{"alias": "amt", "object": authoring_env["order"], "property": "amount"}],
            "body": {"operation": "SUM", "args": [{"ref": "amt"}]},
        },
    )
    assert result.success, result.error
    assert result.metadata["written"] is True
    assert result.metadata["formalized"] is True
    assert result.data["compiled_sql"]

    from app.models import BusinessLogic

    with SessionLocal() as db:
        row = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.display_name == f"订单总额-{authoring_env['uniq']}")
            .first()
        )
        assert row is not None
        # 建出来的是草稿，不是已发布口径。
        assert row.status != EntityStatus.PUBLISHED.value


def test_authoring_write_tools_need_editor():
    """只编不写的是 reader，落库的必须抬到 editor——两者不是一个开关的两个取值。"""
    assert tool_required_role(TOOL_REGISTRY["compile_logic_expression"]) == "reader"
    assert tool_required_role(TOOL_REGISTRY["create_logic"]) == "editor"
    assert tool_required_role(TOOL_REGISTRY["update_logic_expression"]) == "editor"


def test_lint_spec_says_it_checked_nothing_when_there_is_no_table_name():
    """没有物理表名时一条规则都没跑过：`compliant` 必须是 null，不能是「合规」。"""
    result = _call_tool("lint_spec", {"kind": "metric", "spec": {"foo": "bar"}}, role="reader")
    assert result.success
    assert result.data["compliant"] is None
    assert result.data["checked_rules"] == []
    assert "note" in result.data


# ---------------------------------------------------------------- 接数据


def test_list_onboarding_targets_reports_unconfigured_sources(client):
    result = _call_tool("list_onboarding_targets", {}, role="reader")
    assert result.success, result.error
    assert "domains" in result.data
    for source in result.data["data_sources"]:
        # 「登记了」与「能用了」是两件事，目录必须把这一位说出来。
        assert "connection_configured" in source


def test_create_datasource_drops_credentials(client):
    name = f"MCP 测试源-{uuid.uuid4().hex[:6]}"
    result = _call_tool(
        "create_datasource",
        {
            "name": name,
            "kind": "mysql",
            "password": "hunter2",
            "dsn": "mysql://root:hunter2@10.0.0.1/db",
        },
    )
    assert result.success, result.error
    assert set(result.data["dropped_args"]) == {"dsn", "password"}
    assert result.data["connection_configured"] is False

    from app.models.data_app import DataSource

    with SessionLocal() as db:
        row = db.query(DataSource).filter(DataSource.name == name).first()
        assert row is not None
        # 口令没有以任何形式落库。
        assert not (row.dsn_secret_ref or "")


def test_create_datasource_refuses_duplicate_name(client):
    name = f"MCP 重名源-{uuid.uuid4().hex[:6]}"
    assert _call_tool("create_datasource", {"name": name, "kind": "mysql"}).success
    again = _call_tool("create_datasource", {"name": name, "kind": "mysql"})
    assert not again.success
    assert "同名" in again.error


def test_start_ontology_draft_blocks_silent_republish(authoring_env):
    """该域已有发布本体：重跑不是原地覆盖，必须先让人知道后果。"""
    result = _call_tool(
        "start_ontology_draft", {"domain_id": authoring_env["domain_id"], "scope": "draft"}
    )
    assert not result.success
    assert "合并" in result.error
    assert result.data["has_published_ontology"] is True


def test_start_ontology_draft_rejects_invented_domain(client):
    result = _call_tool("start_ontology_draft", {"domain_id": "不存在的域"})
    assert not result.success
    assert "不存在" in result.error


# ---------------------------------------------------------------- 建模工单


def test_modeling_case_spec_roundtrip_and_optimistic_lock(client):
    created = _call_tool("create_modeling_case", {"title": "月度销售分析"}, role="editor")
    assert created.success, created.error
    case_id = created.data["case_id"]

    saved = _call_tool(
        "save_modeling_spec",
        {
            "case_id": case_id,
            "kind": "requirement",
            "payload": {"business_goal": "看清各区域月度销售趋势"},
        },
        role="editor",
    )
    assert saved.success, saved.error
    revision, content_hash = saved.data["revision"], saved.data["content_hash"]

    # 确认必须绑到被审查的那一版：拿一个假 hash 去确认要失败。
    bad = _call_tool(
        "confirm_modeling_spec",
        {
            "case_id": case_id,
            "kind": "requirement",
            "revision": revision,
            "content_hash": "0" * 64,
        },
        role="reviewer",
    )
    assert not bad.success

    ok = _call_tool(
        "confirm_modeling_spec",
        {
            "case_id": case_id,
            "kind": "requirement",
            "revision": revision,
            "content_hash": content_hash,
        },
        role="reviewer",
    )
    assert ok.success, ok.error
    assert ok.data["spec"]["status"] != "draft"


def test_save_modeling_spec_rejects_invented_field_names(client):
    """规格 payload 是强类型的：字段名对不上要当场拒绝，不是默默存下来。

    对话侧那份实现是直接写表的平行路径，写进去的 `analysis_scope` / `primary_subject`
    等字段名与 RequirementSpec（extra: forbid）对不上——从对话建的规格读得出来、按
    schema 校验却过不去。走服务层这条路就不会。
    """
    created = _call_tool("create_modeling_case", {"title": "字段名回归"}, role="editor")
    case_id = created.data["case_id"]
    result = _call_tool(
        "save_modeling_spec",
        {
            "case_id": case_id,
            "kind": "requirement",
            "payload": {"business_goal": "x", "analysis_scope": "自造的字段名"},
        },
        role="editor",
    )
    assert not result.success
    assert result.metadata.get("validation_error") is True


def test_modeling_confirm_needs_reviewer(call_via_server, client):
    """确认是「这一版被采纳了」的表态，editor 不该能自己拍板。"""
    assert tool_required_role(TOOL_REGISTRY["confirm_modeling_spec"]) == "reviewer"
    body = _body(
        call_via_server(
            "confirm_modeling_spec",
            {"case_id": "x", "kind": "requirement", "revision": 1, "content_hash": "y"},
            role="editor",
        )
    )
    assert body["metadata"].get("denied") is True


# ---------------------------------------------------------------- 落点目录


def test_list_datasets_lists_registered_landings_only(authoring_env):
    """新建的本体一张落点都没登记：目录必须回空，而不是按命名规则推一批出来。"""
    result = _call_tool(
        "list_datasets", {"ontology_id": authoring_env["ontology_id"]}, role="reader"
    )
    assert result.success, result.error
    assert result.data["items"] == []
    assert result.data["total"] == 0


def test_list_datasets_rejects_unknown_ontology(client):
    result = _call_tool("list_datasets", {"ontology_id": "不存在"}, role="reader")
    assert not result.success
    assert "本体不存在" in result.error


# ---------------------------------------------------------------- 语义证明的拒绝信号


def test_unknown_table_says_whether_the_object_merely_lacks_publication(client):
    """「不对应任何已发布业务对象」有两种成因，下一步完全不同。

    真机上出现过：对象存在、落点也搬好了，只是本体侧还没发布。拒绝信号只说「表不对应
    任何已发布业务对象」，调用方于是花了四次额外调用才拼出真相。
    """
    import uuid as _uuid

    from app.models import EntityStatus as _ES

    uniq = _uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:unpub-{uniq}", name=f"未发布域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()
        # 已发布本体里的一个**未发布**对象。
        obj = ObjectType(
            ontology_id=onto.id, name=f"pending_{uniq}", display_name="待发布对象",
            table_role="business_object", status=_ES.EDITED.value,
        )
        db.add(obj)
        db.commit()
        onto_id, name = onto.id, obj.name

    result = _call_tool("execute_sql", {"sql": f"SELECT * FROM {name}", "ontology_ids": [onto_id]})
    assert not result.success
    hint = (result.data or {}).get("hint") or {}
    assert hint.get("unpublished_match"), hint
    assert "还没发布" in hint.get("fix", "")
