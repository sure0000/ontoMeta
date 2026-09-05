"""dsh 验收报告 P0 五条的回归。

每条钉的都是**实测踩到过的**具体失败，不是抽象不变量：

1. skill 只走 prompts 时，只桥接 tools 的客户端（dsh 就是）拿到 0 份指引 → get_playbook。
2. `get_landing(keyword="company")` 静默认了别的数据域那个同名对象，还回一句权威口吻的
   「未落地，不要按命名规则推测表名」。
3. `get_ontology_overview` 一份回包里草稿域计数与已发布域分布同名并列。
4. 一个 publisher 令牌加一句话就能把任务推到远端 Airflow 真跑。
5. 审计趋势桶被打成 UTC、其余时间裸着回，前端一转差整整一个时区。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

import asyncio

import mcp.types as types

from app.database import SessionLocal
from app.mcp import introspection
from app.mcp import server as mcp_server
from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.mcp.tools._common import artifact_approval_digest
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
)
from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.models.mcp_audit import McpAuditLog

PUB = EntityStatus.PUBLISHED.value
DRAFT = EntityStatus.SUGGESTED.value


def _body(result):
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(
        name: str,
        arguments: dict,
        role: str | None = "reader",
        principal_id: str | None = None,
        client_type: str = "mcp_local",
    ):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(
            mcp_server,
            "resolve_auth_context",
            lambda: AuthContext(
                client_type=client_type,
                role=role,
                principal_id=principal_id,
                principal_name=f"mcp-{role}",
            ),
        )
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _call


@pytest.fixture
def seeded_ontology(client):
    """一个已发布本体，外加一条草稿对象——好让 in_scope / draft 两个口径真的不等。"""
    uniq = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:p0-{uniq}", name=f"P0域-{uniq}"
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
        db.add_all(
            [
                ObjectType(
                    ontology_id=onto.id,
                    name=f"order_{uniq}",
                    display_name=f"订单-{uniq}",
                    table_role="business_object",
                    status=PUB,
                ),
                ObjectType(
                    ontology_id=onto.id,
                    name=f"stage_{uniq}",
                    display_name=f"暂存-{uniq}",
                    table_role="technical",
                    status=DRAFT,
                ),
            ]
        )
        db.commit()
        return onto.id


@pytest.fixture
def ops_env(client):
    """`订单-x` 精确命中一个，另外两个只是子串——用来分辨精确匹配与撞名。"""
    uniq = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:p0ops-{uniq}", name=f"P0运维域-{uniq}"
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

        def _obj(name, display, status=PUB):
            obj = ObjectType(
                ontology_id=onto.id,
                name=f"{name}_{uniq}",
                display_name=display,
                table_role="business_object",
                status=status,
            )
            db.add(obj)
            return obj

        order = _obj("order", f"订单-{uniq}")
        _obj("ods_order", f"ODS订单-{uniq}", status=DRAFT)
        _obj("dwd_order", f"DWD订单-{uniq}", status=DRAFT)
        db.commit()
        return {"uniq": uniq, "ontology_id": onto.id, "order_id": order.id}


# ------------------------------------------------------------------ P0-1 playbook


def test_playbook_is_a_tool_so_prompt_less_clients_still_get_guidance(call_via_server):
    """指引必须走 tools 通道。

    dsh 的 MCP 客户端文档原文：Only tools are bridged: MCP resources and prompts are
    not supported。实测它加载的 5 份 skill 全部来自本地文件，MCP 的 prompt 一个都没进去。
    只挂在 list_prompts 上，等于远程接入方拿到 29 个工具和零份指引。
    """
    assert "get_playbook" in TOOL_REGISTRY
    assert tool_required_role(TOOL_REGISTRY["get_playbook"]) == "reader"

    index = _body(call_via_server("get_playbook", {}, "reader"))
    assert index["success"] is True
    topics = {item["topic"] for item in index["data"]["topics"]}
    assert {"ontometa-discovery", "ontometa-query", "ontometa-task-execute"} <= topics
    # 清单得能让模型选得动：每条都要有适用场景，不能只有名字。
    assert all(item["when_to_use"] for item in index["data"]["topics"])


def test_playbook_returns_the_same_body_prompts_serve(call_via_server):
    body = _body(call_via_server("get_playbook", {"topic": "ontometa-query"}, "reader"))
    assert body["success"] is True
    text = body["data"]["body"]
    assert text.startswith("---\n")
    assert "name: ontometa-query" in text
    assert "## 通用底线" in text
    assert body["data"]["output_contract"]["version"] == "1"
    assert body["data"]["output_contract"]["max_detail_rows"] == 10


def test_playbook_rejects_unknown_topic_with_the_list(call_via_server):
    result = call_via_server("get_playbook", {"topic": "ontometa-nope"}, "reader")
    assert result.is_error is True
    body = _body(result)
    assert "ontometa-discovery" in body["data"]["available_topics"]


def test_server_instructions_point_at_the_tool_not_only_prompts():
    from app.mcp.server import SERVER_INSTRUCTIONS

    assert "get_playbook" in SERVER_INSTRUCTIONS
    # 第一段就得说，写在末尾等于没写。
    assert "get_playbook" in SERVER_INSTRUCTIONS.split("\n\n")[0]


# ------------------------------------------------- P0-2 get_landing 跨域撞名


@pytest.fixture()
def collision_env(client):
    """两个数据域各有一个同名 `company`／「公司」，一个已发布、一个还是草稿。

    这正是真库上的形状：odoo 的 company 已发布，erpnext 的 company 是 edited。
    """
    uniq = uuid4().hex[:8]
    name = f"company_{uniq}"
    display = f"公司-{uniq}"
    with SessionLocal() as db:
        made = {}
        for label, status in (("published", PUB), ("draft", DRAFT)):
            domain = DomainContext(
                datahub_domain_id=f"urn:li:domain:collide-{label}-{uniq}",
                name=f"{label}域-{uniq}",
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
            obj = ObjectType(
                ontology_id=onto.id,
                name=name,
                display_name=display,
                table_role="business_object",
                status=status,
            )
            db.add(obj)
            db.flush()
            made[label] = {"object_id": obj.id, "ontology_id": onto.id}
        db.commit()
    return {"name": name, "display": display, **made}


def test_get_landing_does_not_let_publish_state_disambiguate_domains(
    call_via_server, collision_env
):
    """曾经的错答案：候选集先按 published_only 过滤再判唯一性。

    草稿那个被滤掉、只剩已发布那个 → 判定「唯一」→ 直接认了别的域的对象，
    还回一句权威口吻的「未落地，不要按命名规则推测表名」。发布状态成了跨域消歧器。
    """
    body = _body(
        call_via_server(
            "get_landing",
            {"target_kind": "object", "keyword": collision_env["name"]},
            "reader",
        )
    )
    assert body["success"] is True
    assert body["metadata"]["resolved"] is False, "同名跨域必须给候选，不许自己挑一个"

    ids = {item["id"] for item in body["data"]["candidates"]}
    assert collision_env["published"]["object_id"] in ids
    assert collision_env["draft"]["object_id"] in ids, "未发布的那个也得出现在候选里"
    # 候选要能分辨：数据域 + 发布状态，两者缺一都挑不出来。
    for item in body["data"]["candidates"]:
        assert item["domain_name"]
        assert item["status"]


def test_get_landing_resolves_when_scoped_to_one_ontology(
    call_via_server, collision_env
):
    """歧义的出路是给 ontology_id，而不是让工具去猜。"""
    body = _body(
        call_via_server(
            "get_landing",
            {
                "target_kind": "object",
                "keyword": collision_env["name"],
                "ontology_id": collision_env["draft"]["ontology_id"],
            },
            "reader",
        )
    )
    assert body["metadata"]["resolved"] is True
    assert body["data"]["target_id"] == collision_env["draft"]["object_id"]


def test_get_landing_reads_unpublished_subject_with_an_explicit_marker(
    call_via_server, collision_env
):
    """未发布主体照样读落点，但状态要说破。

    落点登记与发布状态无关（query_object_detail 的 landing 块一直这么给）。
    此前多加的那道发布闸把「erpnext 公司」直接报成「主体不存在或未发布」，
    逼得调用方绕到另一个工具去读同一份事实。
    """
    body = _body(
        call_via_server(
            "get_landing",
            {
                "target_kind": "object",
                "target_id": collision_env["draft"]["object_id"],
            },
            "reader",
        )
    )
    assert body["success"] is True
    assert body["metadata"]["subject_status"] == DRAFT
    assert DRAFT in body["data"]["note"]


def test_get_landing_prefers_exact_match_over_substring(call_via_server, ops_env):
    """`订单-a1b2` 精确命中一个，`ODS订单-a1b2`／`DWD订单-a1b2` 只是子串——认精确那个。

    与上面那条不冲突：`company` 的两个候选都是**精确**同名，所以那里不许猜。
    """
    body = _body(
        call_via_server(
            "get_landing",
            {"target_kind": "object", "keyword": f"订单-{ops_env['uniq']}"},
            "reader",
        )
    )
    assert body["metadata"]["resolved"] is True
    assert body["data"]["target_id"] == ops_env["order_id"]


# --------------------------------------------- P0-3 get_ontology_overview 口径


def test_overview_never_puts_two_scopes_under_one_name(call_via_server, seeded_ontology):
    """草稿域计数与已发布域分布曾经在同一份回包里同名并列。

    模型会说「有 1035 个对象，其中 44 个业务对象」——两个数字来自不同的世界。
    """
    body = _body(
        call_via_server(
            "get_ontology_overview", {"ontology_id": seeded_ontology}, "reader"
        )
    )
    assert body["success"] is True
    # 含混的名字必须从 ontology 块里消失，只在 counts 里按口径出现。
    for key in ("object_type_count", "relation_type_count", "business_logic_count"):
        assert key not in body["data"]["ontology"]

    counts = body["data"]["counts"]
    assert counts["scope"] == "published"
    assert "in_scope_object_types" in counts and "draft_object_types" in counts
    # 分布与清单必须与 in_scope_* 同口径。
    assert counts["in_scope_object_types"] == sum(
        body["data"]["object_distribution"]["by_role"].values()
    )
    assert body["metadata"]["counts_scope"]["in_scope_*"]


def test_overview_draft_scope_counts_stay_available(call_via_server, seeded_ontology):
    """拆开不是删掉：草稿域的数还得能拿到，否则「还有多少没发布」就问不了了。"""
    body = _body(
        call_via_server(
            "get_ontology_overview",
            {"ontology_id": seeded_ontology, "published_only": False},
            "reader",
        )
    )
    counts = body["data"]["counts"]
    assert counts["scope"] == "draft"
    assert counts["draft_object_types"] is not None


# ------------------------------------------ P0-4 代执行授权闸（与角色正交）


def _artifact(**kwargs) -> str:
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"p0-gate-{uuid4().hex[:8]}",
            spec_json="{}",
            validation_report_json='{"blocking_count": 0}',
            **kwargs,
        )
        db.add(artifact)
        db.commit()
        return artifact.id


def test_publisher_alone_cannot_confirm_without_per_task_approval(call_via_server):
    """这条挡的是审计里真实发生过的事：一个 publisher 令牌 + 一句话推到远端 Airflow。"""
    task_id = _artifact(status=ArtifactStatus.VALIDATED.value)
    result = call_via_server("confirm_task", {"task_id": task_id}, "publisher")
    assert result.is_error is True
    body = _body(result)
    assert body["metadata"]["gate"] == "agent_execution_approval"
    # 不能报成权限不足，否则模型会去换令牌、重试。
    assert body["metadata"].get("denied") is not True
    assert "换令牌没用" in body["data"]["note"]


def test_publisher_alone_cannot_execute_without_per_task_approval(
    call_via_server, monkeypatch
):
    """授权可能在 confirm 与 execute 之间被收回，所以真正打到远端的这一步要再查一次。"""
    task_id = _artifact(status=ArtifactStatus.CONFIRMED.value)
    monkeypatch.setattr(
        "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
        lambda _id: pytest.fail("未授权的任务不得派发 worker"),
    )
    result = call_via_server("execute_task", {"task_id": task_id}, "publisher")
    assert result.is_error is True
    assert _body(result)["metadata"]["gate"] == "agent_execution_approval"


def test_approval_lets_the_agent_through(call_via_server):
    task_id = _artifact(
        status=ArtifactStatus.VALIDATED.value, agent_execution_approved=True
    )
    body = _body(call_via_server("confirm_task", {"task_id": task_id}, "publisher"))
    assert body["success"] is True
    assert body["data"]["status"] == ArtifactStatus.CONFIRMED.value


def test_stdio_host_confirmation_requires_explicit_opt_in_and_digest(
    call_via_server, monkeypatch
):
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        settings_service = SettingsService()
        original = settings_service.get_mcp_settings(db)
        settings_service.update_mcp_settings(
            db,
            {
                "mcp_require_execution_approval": True,
                "mcp_allow_stdio_interactive_approval": True,
            },
        )
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"p0-host-{uuid4().hex[:8]}",
            status=ArtifactStatus.VALIDATED.value,
            validation_report_json='{"blocking_count": 0}',
            spec_json='{"target_table":"host_test"}',
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id
        digest = artifact_approval_digest(artifact)

    try:
        confirmation = {
            "approved": True,
            "channel": "ask_user_question",
            "digest": digest,
        }
        body = _body(
            call_via_server(
                "confirm_task",
                {"task_id": task_id, "host_confirmation": confirmation},
                "publisher",
                principal_id="host-principal",
            )
        )
        assert body["success"] is True
        assert body["data"]["approval_source"] == "stdio_host_interactive"

        monkeypatch.setattr(
            "app.mcp.tools.lifecycle.spawn_artifact_execution_worker",
            lambda _task_id: None,
        )
        body = _body(
            call_via_server(
                "execute_task",
                {"task_id": task_id, "host_confirmation": confirmation},
                "publisher",
                principal_id="host-principal",
            )
        )
        assert body["success"] is True
        assert body["data"]["approval_source"] == "stdio_host_interactive"
    finally:
        with SessionLocal() as db:
            SettingsService().update_mcp_settings(db, original)


def test_stdio_host_confirmation_rejects_stale_digest(call_via_server):
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        settings_service = SettingsService()
        original = settings_service.get_mcp_settings(db)
        settings_service.update_mcp_settings(
            db,
            {
                "mcp_require_execution_approval": True,
                "mcp_allow_stdio_interactive_approval": True,
            },
        )
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"p0-host-stale-{uuid4().hex[:8]}",
            status=ArtifactStatus.VALIDATED.value,
            validation_report_json='{"blocking_count": 0}',
            spec_json='{"target_table":"host_stale"}',
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id

    try:
        result = call_via_server(
            "confirm_task",
            {
                "task_id": task_id,
                "host_confirmation": {
                    "approved": True,
                    "channel": "ask_user_question",
                    "digest": "stale",
                },
            },
            "publisher",
            principal_id="host-principal",
        )
        body = _body(result)
        assert result.is_error is True
        assert body["metadata"]["gate"] == "host_interactive_approval_stale"
        assert "重新展示并确认" in body["error"]
    finally:
        with SessionLocal() as db:
            SettingsService().update_mcp_settings(db, original)


def test_stdio_host_confirmation_is_not_available_to_http(call_via_server):
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        settings_service = SettingsService()
        original = settings_service.get_mcp_settings(db)
        settings_service.update_mcp_settings(
            db,
            {
                "mcp_require_execution_approval": True,
                "mcp_allow_stdio_interactive_approval": True,
            },
        )
        artifact = GovernanceArtifact(
            kind="metric",
            name=f"p0-host-http-{uuid4().hex[:8]}",
            status=ArtifactStatus.VALIDATED.value,
            validation_report_json='{"blocking_count": 0}',
            spec_json="{}",
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id
        digest = artifact_approval_digest(artifact)

    try:
        result = call_via_server(
            "confirm_task",
            {
                "task_id": task_id,
                "host_confirmation": {
                    "approved": True,
                    "channel": "ask_user_question",
                    "digest": digest,
                },
            },
            "publisher",
            principal_id="http-principal",
            client_type="mcp_remote",
        )
        body = _body(result)
        assert result.is_error is True
        assert body["metadata"]["gate"] == "host_interactive_approval_not_allowed"
    finally:
        with SessionLocal() as db:
            SettingsService().update_mcp_settings(db, original)


def test_no_mcp_tool_can_grant_the_approval(client):
    """闸门的全部意义在于 agent 不能自己给自己发许可。

    只要有任何一个 MCP 工具能写这几列，这道闸就等于不存在。
    """
    import inspect

    from app.mcp import tools as tools_pkg

    offenders = []
    for module in vars(tools_pkg).values():
        if not inspect.ismodule(module) or not (module.__name__ or "").startswith(
            "app.mcp.tools."
        ):
            continue
        src = inspect.getsource(module)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "agent_execution_approved" in stripped and "=" in stripped:
                # 只读比较（== / getattr）没问题，赋值才是问题。
                head = stripped.split("=")[0]
                if "agent_execution_approved" in head and "==" not in stripped:
                    offenders.append(f"{module.__name__}: {stripped}")
    assert not offenders, f"MCP 工具不得写代执行授权：{offenders}"


def test_rest_is_the_writer_and_it_round_trips(client, admin_headers):
    task_id = _artifact(status=ArtifactStatus.VALIDATED.value)

    granted = client.post(
        f"/api/agents/artifacts/{task_id}/agent-approval",
        json={"approved": True, "operator": "运维小王"},
        headers=admin_headers,
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["agent_execution_approved"] is True
    assert granted.json()["agent_execution_approved_by"] == "运维小王"

    revoked = client.post(
        f"/api/agents/artifacts/{task_id}/agent-approval",
        json={"approved": False},
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    payload = revoked.json()
    assert payload["agent_execution_approved"] is False
    # 收回时署名和时间要一起清掉，留着会让人以为授权还在。
    assert payload["agent_execution_approved_by"] is None
    assert payload["agent_execution_approved_at"] is None


def test_approval_defaults_to_required(client):
    """没配过的老部署带逐条闸，且不会默认信任 stdio 宿主断言。"""
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        runtime = SettingsService().get_mcp_runtime(db)
        assert runtime.mcp_require_execution_approval is True
        assert runtime.mcp_allow_stdio_interactive_approval is False


def test_settings_read_side_never_lies_about_the_gate(client, admin_headers):
    """开关的显示值必须和运行期同一个缺省。

    这个字段是在设置行建好之后才加的，库里那行没有这个 key。读侧不补缺省的话，
    界面上开关显示"关"、运行期其实开着——比没有开关更糟。
    """
    read = client.get("/api/mcp/settings", headers=admin_headers)
    assert read.status_code == 200
    assert read.json()["mcp_require_execution_approval"] is True
    assert read.json()["mcp_allow_stdio_interactive_approval"] is False

    # 关掉要真能关掉：exclude_unset + truthy 过滤会把 False 悄悄吃掉。
    off = client.put(
        "/api/mcp/settings",
        json={"mcp_require_execution_approval": False},
        headers=admin_headers,
    )
    assert off.status_code == 200, off.text
    assert off.json()["mcp_require_execution_approval"] is False
    with SessionLocal() as db:
        from app.services.settings_service import SettingsService

        assert (
            SettingsService().get_mcp_runtime(db).mcp_require_execution_approval is False
        )

    back = client.put(
        "/api/mcp/settings",
        json={"mcp_require_execution_approval": True},
        headers=admin_headers,
    )
    assert back.json()["mcp_require_execution_approval"] is True

    host_on = client.put(
        "/api/mcp/settings",
        json={"mcp_allow_stdio_interactive_approval": True},
        headers=admin_headers,
    )
    assert host_on.status_code == 200
    assert host_on.json()["mcp_allow_stdio_interactive_approval"] is True
    client.put(
        "/api/mcp/settings",
        json={"mcp_allow_stdio_interactive_approval": False},
        headers=admin_headers,
    )


# ----------------------------------------------------- P0-5 审计时间序列化


def test_timeline_buckets_share_the_clock_with_last_call_at(client):
    """同一页面上，趋势桶曾经比「最近调用」还晚 8 小时。

    created_at 落库是裸时间（server_default=now()），last_call_at 也裸着回；
    唯独趋势桶被 datetime.fromtimestamp(..., tz=utc) 打上 +00:00 —— 同一个钟点，
    一个裸一个带偏移，前端 new Date().toLocaleString() 一转就差整整一个时区。
    """
    now = datetime.now()
    with SessionLocal() as db:
        db.add(
            McpAuditLog(
                created_at=now - timedelta(minutes=5),
                tool_name=f"p0-clock-{uuid4().hex[:6]}",
                principal_role="reader",
                success=True,
                duration_ms=3,
            )
        )
        db.commit()

        stats = introspection.compute_stats(db, window_minutes=24 * 60)

    assert stats["timeline"], "窗口内应有桶"
    for bucket in stats["timeline"]:
        assert "+" not in bucket["bucket"] and not bucket["bucket"].endswith("Z"), (
            f"桶不该带时区偏移：{bucket['bucket']}"
        )
        # 与 last_call_at 同一套钟：任何桶都不该晚于最近一次调用所在的小时。
        assert datetime.fromisoformat(bucket["bucket"]) <= now

    last_call = datetime.fromisoformat(stats["last_call_at"])
    latest_bucket = datetime.fromisoformat(stats["timeline"][-1]["bucket"])
    assert latest_bucket <= last_call


def test_window_start_uses_the_same_clock_as_the_rows(client):
    """窗口起点也曾经用 UTC：aware 值传给 timestamp without time zone 列会被丢掉偏移，
    「最近 24 小时」在 UTC+8 上实际取了 32 小时。"""
    start = introspection._window_start(60)
    assert start.tzinfo is None
    drift = abs((datetime.now() - timedelta(minutes=60) - start).total_seconds())
    assert drift < 5
