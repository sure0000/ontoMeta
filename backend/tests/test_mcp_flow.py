"""交互式建数流程（start_task_flow / advance_task_flow）。

盯的是四件会安静出错的事：**替用户选**（自动挑一个 id 就往下走）、**问该问的之外的**
（系统能定的参数还去逐个问，用户原话是"6 环确实太繁琐"）、**填错的值被悄悄换掉**
（用系统推荐值顶替），以及**确认过的方案与执行的方案不是同一份**（审查后又改了参数，
旧确认照样放行）。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.mcp.tools import TOOL_REGISTRY, AuthContext
from app.models import DataSource, DomainContext, ObjectType, Ontology, Property
from app.models.ontology import OntologyStatus

AUTH = AuthContext(client_type="mcp_local", role="publisher", principal_id="flow-test")
READER = AuthContext(client_type="mcp_local", role="reader", principal_id="flow-reader")


def call(name: str, arguments: dict, auth: AuthContext = AUTH):
    return asyncio.run(TOOL_REGISTRY[name].execute(arguments, auth))


@pytest.fixture
def default_warehouse(db):
    """一个「启用的默认 Doris 仓」。

    ``is_default_warehouse`` 上有唯一偏索引（全库只能有一行为真），整套用例又共用同一个
    SQLite 文件——能复用就复用，自己建的用完必须删掉。
    """
    existing = (
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).first()
    )
    if existing is not None:
        yield existing
        return
    warehouse = DataSource(
        name=f"flow-doris-{uuid4().hex[:6]}",
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
def sync_ready_ontology(db):
    """一个能真正走完同步流程的本体：对象带物理源表，配套一个启用的业务源库。"""
    suffix = uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"flow-domain-{suffix}", name=f"流程测试域 {suffix}")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id, version=1, status=OntologyStatus.PUBLISHED.value
    )
    db.add(ontology)
    db.flush()
    customer = ObjectType(
        ontology_id=ontology.id,
        name=f"customer_{suffix}",
        display_name="客户",
        table_role="business_object",
        status="published",
        source_ref=(
            f"urn:li:dataset:(urn:li:dataPlatform:mysql,erp.tabCustomer_{suffix},PROD)"
        ),
    )
    db.add(customer)
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
    source = DataSource(
        name=f"flow-erp-{suffix}",
        kind="mysql",
        purpose="business_source",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://u:p@h/erp",
    )
    db.add(source)
    db.commit()
    try:
        yield ontology.id, customer.name, source.id
    finally:
        db.delete(source)
        db.query(Property).filter(Property.object_type_id == customer.id).delete()
        db.delete(customer)
        db.delete(ontology)
        db.delete(domain)
        db.commit()


def test_start_task_flow_hands_back_a_form_not_a_single_question():
    result = call("start_task_flow", {"goal": "帮我把客户数据弄进数仓"})
    assert result.success, result.error
    data = result.data
    assert data["status"] == "ask"
    form = data["form"]
    assert [f["key"] for f in form["fields"]] == ["kind"]
    assert {o["value"] for o in form["fields"][0]["options"]} == {
        "sync",
        "transform",
        "materialize",
        "metric",
    }
    # 推荐可以有，但不能替用户定下来：这一轮不许出现已经选好的 flow。
    assert data["flow"] is None
    assert "kind" not in data["answers"]
    # 指引必须点名宿主的交互工具，否则模型只会退回一问一答
    assert "ask_user_question" in data["instruction"]


def test_start_task_flow_recommends_but_does_not_pick():
    data = call("start_task_flow", {"goal": "把订单表同步进 ODS"}).data
    options = data["form"]["fields"][0]["options"]
    assert [o["value"] for o in options if o.get("recommended")] == ["sync"]


def test_start_task_flow_carries_goal_in_cumulative_answers():
    """后续只回传表单答案时，原始需求仍能用于对象预选。"""
    goal = "将 erpnext 的库存结账（stock_closing_entry）同步进数仓"
    data = call("start_task_flow", {"goal": goal}).data

    assert data["answers"]["task_requirement"] == goal
    assert data["goal"] == goal


def test_advance_task_flow_asks_for_object_before_dependent_source(
    sync_ready_ontology, default_warehouse
):
    """未带 goal/对象时先问对象，不能把依赖字段的空 options 判成无源。"""
    ontology_id, object_name, source_id = sync_ready_ontology
    data = call(
        "advance_task_flow",
        {
            "kind": "sync",
            "answers": {"kind": "sync", "ontology_id": ontology_id},
        },
    ).data

    assert data["status"] == "ask", data
    fields = {field["key"]: field for field in data["form"]["fields"]}
    assert "object_type" in fields
    assert "source_datasource_id" not in fields
    assert any(option["value"] == object_name for option in fields["object_type"]["options"])

    # 对象定下来后，同一轮才展开真实来源候选。
    next_data = call(
        "advance_task_flow",
        {
            "kind": "sync",
            "answers": {
                "kind": "sync",
                "ontology_id": ontology_id,
                "object_type": object_name,
            },
        },
    ).data
    assert next_data["status"] == "ask", next_data
    source_field = next(
        field for field in next_data["form"]["fields"] if field["key"] == "source_datasource_id"
    )
    assert [option["value"] for option in source_field["options"]] == [source_id]


def test_derivable_parameters_are_not_asked_at_all(sync_ready_ontology, default_warehouse):
    """能从本体/契约/默认值推导的参数不该占用一次提问——直接进执行审查。"""
    ontology_id, object_name, source_id = sync_ready_ontology
    data = call(
        "advance_task_flow",
        {
            "kind": "sync",
            "answers": {"ontology_id": ontology_id},
            "goal": "把客户主数据同步进数仓",
        },
    ).data
    assert data["status"] == "review", data
    review = data["review"]
    # 审查摆的是 Drafter 派生的 Spec，不是把填的值念一遍：落点表名只有 Drafter 知道
    plan = {row["key"]: row["value"] for row in review["plan"]}
    assert plan["object_display_name"] == "客户"
    assert plan["target"].startswith("ods.")
    assert any("全量覆盖" in note for note in review["notes"])
    # 阻断项如实摆出来（测试环境没配 Airflow，提交前自检就会报）——审查的价值正在于
    # 让"这事现在跑不了"在人点确认之前就看得见。
    assert review["blocking_count"] == len(review["blocking_issues"])
    if review["blocking_count"]:
        assert any("阻断" in note for note in review["notes"])
    # 每个参数仍然可以就地改
    keys = {f["key"] for f in data["form"]["fields"]}
    assert {"source_datasource_id", "target_datasource_id", "mode"} <= keys
    assert any(f["auto"] for f in data["form"]["fields"])


def test_only_undecidable_parameters_are_asked(sync_ready_ontology, default_warehouse):
    """增量同步要的主键/增量字段/初始水位没有默认值——这三项才值得问。"""
    ontology_id, object_name, _ = sync_ready_ontology
    data = call(
        "advance_task_flow",
        {
            "kind": "sync",
            "answers": {"ontology_id": ontology_id, "mode": "incremental"},
            "goal": "增量同步客户主数据",
        },
    ).data
    assert data["status"] == "ask"
    assert data["form"]["submit_key"] is None  # 定参数不是"确认"，没有确认位
    assert set(f["key"] for f in data["form"]["fields"]) == {
        "primary_keys",
        "incremental_column",
        "initial_watermark",
    }


def test_confirmation_is_bound_to_the_reviewed_plan(sync_ready_ontology, default_warehouse):
    """一句 "yes" 不能放行：确认必须绑在被审查的那份方案上。"""
    ontology_id, *_ = sync_ready_ontology
    goal = "把客户主数据同步进数仓"
    first = call(
        "advance_task_flow",
        {"kind": "sync", "answers": {"ontology_id": ontology_id}, "goal": goal},
    ).data
    digest = first["form"]["submit_value"]
    assert digest and first["review"]["plan_digest"] == digest

    answers = dict(first["answers"])
    answers["__confirm_plan"] = "yes"
    said_yes = call("advance_task_flow", {"kind": "sync", "answers": answers, "goal": goal}).data
    assert said_yes["status"] == "review"
    assert said_yes["review"]["stale_confirmation"] is True

    answers["__confirm_plan"] = digest
    ready = call("advance_task_flow", {"kind": "sync", "answers": answers, "goal": goal}).data
    assert ready["status"] == "ready"
    assert ready["next_call"]["tool"] == "draft_task"
    payload = ready["next_call"]["arguments"]
    assert payload["kind"] == "sync" and payload["context"]["ontology_id"] == ontology_id


def test_changing_a_parameter_after_confirmation_voids_it(
    sync_ready_ontology, default_warehouse
):
    ontology_id, *_ = sync_ready_ontology
    goal = "把客户主数据同步进数仓"
    review = call(
        "advance_task_flow",
        {"kind": "sync", "answers": {"ontology_id": ontology_id}, "goal": goal},
    ).data
    answers = dict(review["answers"])
    answers["__confirm_plan"] = review["form"]["submit_value"]
    answers["mode"] = "cdc"  # 确认之后又改了装载方式
    after = call("advance_task_flow", {"kind": "sync", "answers": answers, "goal": goal}).data
    # CDC 要的参数还没定，先回到提问；无论如何都不能拿旧确认直接 ready
    assert after["status"] in {"ask", "review"}
    if after["status"] == "review":
        assert after["review"]["plan_digest"] != answers["__confirm_plan"]


def test_flow_output_feeds_propose_without_editing(sync_ready_ontology, default_warehouse):
    """审查里那份方案必须是真提案跑出来的——否则"审查"审的是一份不存在的东西。"""
    ontology_id, *_ = sync_ready_ontology
    review = call(
        "advance_task_flow",
        {"kind": "sync", "answers": {"ontology_id": ontology_id}, "goal": "同步客户"},
    ).data
    assert review["status"] == "review"
    answers = dict(review["answers"])
    answers["__confirm_plan"] = review["form"]["submit_value"]
    ready = call("advance_task_flow", {"kind": "sync", "answers": answers, "goal": "同步客户"}).data
    context = ready["next_call"]["arguments"]["context"]
    proposal = call("propose_sync", {"intent": "同步客户", "context": context})
    assert proposal.success, proposal.error
    assert proposal.data["proposal"]["spec"]["target"] == {
        row["key"]: row["value"] for row in review["review"]["plan"]
    }["target"]


def test_a_rejected_value_comes_back_on_the_field_not_silently_replaced(
    sync_ready_ontology, default_warehouse
):
    ontology_id, object_name, *_ = sync_ready_ontology
    answers = {
        "ontology_id": ontology_id,
        "task_requirement": "把客户主数据同步进数仓",
        "object_type": object_name,
        "mode": "根本没有这种装载方式",
    }
    data = call("advance_task_flow", {"kind": "sync", "answers": answers}).data
    assert data["status"] == "ask"
    mode = next(f for f in data["form"]["fields"] if f["key"] == "mode")
    assert "对不上候选" in mode["error"]
    # 错值不许被系统推荐值顶替
    assert data["answers"]["mode"] == "根本没有这种装载方式"


def test_label_answers_are_matched_back_to_real_values(
    sync_ready_ontology, default_warehouse
):
    """宿主表单回给模型的是选项 label，不是 value——两者都要认。"""
    ontology_id, object_name, *_ = sync_ready_ontology
    answers = {
        "ontology_id": ontology_id,
        "task_requirement": "同步客户",
        "object_type": "客户",
        "mode": "全量覆盖",
    }
    data = call("advance_task_flow", {"kind": "sync", "answers": answers}).data
    assert data["answers"]["object_type"] == object_name
    assert data["answers"]["mode"] == "full"


def test_multiselect_answer_matches_each_item(sync_ready_ontology, default_warehouse):
    """多选值要逐项对候选。曾经这里会无限递归——materialize 的物化范围一进来就炸。"""
    ontology_id, *_ = sync_ready_ontology
    answers = {
        "ontology_id": ontology_id,
        "task_requirement": "增量同步客户",
        "mode": "incremental",
        "primary_keys": ["客户编号"],
    }
    data = call(
        "advance_task_flow", {"kind": "sync", "answers": answers, "goal": "增量同步客户"}
    ).data
    assert data["answers"]["primary_keys"] == ["customer_id"]


def test_flow_requires_editor_role():
    from app.mcp.tools import tool_required_role

    for name in ("start_task_flow", "advance_task_flow", "open_task_form", "wait_task_form"):
        assert tool_required_role(TOOL_REGISTRY[name]) == "editor"
    assert not READER.has_role("editor")


def test_advance_rejects_unknown_kind():
    result = call("advance_task_flow", {"kind": "whatever", "answers": {}})
    assert not result.success
    assert "start_task_flow" in result.error


# --------------------------------------------------------------------------
# 网页表单兜底（客户端没有原生问答工具时）
# --------------------------------------------------------------------------


def _open_form(ontology_id: str, **extra) -> dict:
    answers = {"ontology_id": ontology_id, **extra}
    result = call(
        "open_task_form",
        {"kind": "sync", "answers": answers, "goal": "把客户主数据同步进数仓"},
    )
    assert result.success, result.error
    return result.data


def test_web_form_carries_the_same_review_and_needs_the_same_digest(
    client, admin_headers, sync_ready_ontology, default_warehouse
):
    """链接发出去、页面确认、Agent 取回继续——三段用的是同一份方案和同一次确认。"""
    ontology_id, object_name, _ = sync_ready_ontology
    issued = _open_form(ontology_id)
    assert issued["stage"] == "review"
    form_id = issued["form_id"]

    page = client.get(f"/api/mcp/flow-forms/{form_id}", headers=admin_headers)
    assert page.status_code == 200, page.text
    state = page.json()
    assert state["status"] == "pending" and state["stage"] == "review"
    assert state["review"]["plan"] and state["review"]["notes"]
    digest = state["form"]["submit_value"]

    # 没点确认 = 不受理（页面上只改了值）
    just_values = client.post(
        f"/api/mcp/flow-forms/{form_id}/submit",
        headers=admin_headers,
        json={"values": {}},
    ).json()
    assert just_values["accepted"] is False

    # 指纹对不上 = 提交前方案变过，退回重看
    stale = client.post(
        f"/api/mcp/flow-forms/{form_id}/submit",
        headers=admin_headers,
        json={"values": {}, "confirm": True, "plan_digest": "old-digest"},
    ).json()
    assert stale["accepted"] is False and "变化" in (stale.get("reason") or "")

    done = client.post(
        f"/api/mcp/flow-forms/{form_id}/submit",
        headers=admin_headers,
        json={"values": {}, "confirm": True, "plan_digest": digest},
    ).json()
    assert done["accepted"] is True and done["status"] == "submitted"

    picked = call("wait_task_form", {"form_id": form_id, "timeout_seconds": 1})
    assert picked.success, picked.error
    assert picked.data["status"] == "submitted"
    assert picked.data["next"]["status"] == "ready"
    assert picked.data["next"]["next_call"]["tool"] == "draft_task"


def test_web_form_page_is_readable_but_submitting_needs_editor(
    client, db, sync_ready_ontology, default_warehouse
):
    from app.services.principal_service import PrincipalService

    ontology_id, *_ = sync_ready_ontology
    form_id = _open_form(ontology_id)["form_id"]
    principal, token = PrincipalService().create(db, name="form-reader", role="reader")
    try:
        headers = {"X-Admin-Token": token}
        assert client.get(f"/api/mcp/flow-forms/{form_id}", headers=headers).status_code == 200
        blocked = client.post(
            f"/api/mcp/flow-forms/{form_id}/submit", headers=headers, json={"values": {}}
        )
        assert blocked.status_code == 403
    finally:
        PrincipalService().delete(db, principal.id)


def test_expired_form_is_refused_not_silently_accepted(
    client, admin_headers, db, sync_ready_ontology, default_warehouse
):
    from datetime import datetime, timedelta

    from app.models.mcp_flow_form import McpFlowForm

    ontology_id, *_ = sync_ready_ontology
    form_id = _open_form(ontology_id)["form_id"]
    row = db.get(McpFlowForm, form_id)
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()

    state = client.get(f"/api/mcp/flow-forms/{form_id}", headers=admin_headers).json()
    assert state["status"] == "expired" and state["form"] is None
    result = client.post(
        f"/api/mcp/flow-forms/{form_id}/submit",
        headers=admin_headers,
        json={"values": {}, "confirm": True},
    ).json()
    assert result["accepted"] is False
    waited = call("wait_task_form", {"form_id": form_id, "timeout_seconds": 1})
    assert not waited.success and "过期" in waited.error


def test_open_task_form_does_not_issue_a_link_for_one_off_questions():
    """任务类型/本体这两步只有一个问题，发个网页表单纯属绕远路。"""
    result = call("open_task_form", {"kind": "sync", "answers": {}})
    if not result.success:
        assert "直接问用户" in result.error
    else:
        assert result.metadata.get("form_issued") is False
