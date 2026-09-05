"""dsh 验收报告 P1/P2 的回归。

P0 修的是「会给出错答案」，这一批修的是「答得对但用起来很贵」——每条都对应一个
实测数字：

- 找 id 要开 6 次 query_objects（72 次调用里最多的一种形态）→ resolve_subject
- list_tasks 平均 11.8 秒（逐条对账 Airflow）→ reconcile 开关，默认关
- query_objects(limit=5) 回 9.2 KB（复核工作台字段随列表放大）→ fields 投影
- server_info 回 8.1 KB，把工具清单里已有的描述又抄一遍 → 默认只回角色表
- get_ops_record 成功率 64%，失败多为条件必填没写进 schema（描述里写着"等"）
- SQL 错误双前缀 + SQLAlchemy 官网链接，既进模型上下文也进审计页
- 同一落点连建 4 条同构任务、前 3 条全失败，校验报告一句没提
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import mcp.types as types
import pytest

from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import TOOL_REGISTRY, AuthContext
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
)
from app.models.agent import ArtifactStatus, GovernanceArtifact

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


def _ontology(db, label: str, uniq: str) -> str:
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:{label}-{uniq}", name=f"{label}-{uniq}"
    )
    db.add(domain)
    db.flush()
    onto = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
    )
    db.add(onto)
    db.flush()
    return onto.id


@pytest.fixture
def subjects(client):
    """两个域各有一个精确同名的「公司」，外加一批只是子串命中的噪音对象。

    噪音是关键：候选排序必须在**截断之前**做，否则第二个精确命中根本进不了页面。
    """
    uniq = uuid4().hex[:8]
    name = f"company_{uniq}"
    display = f"公司-{uniq}"
    made = {}
    with SessionLocal() as db:
        for label, status in (("odoo", PUB), ("erpnext", DRAFT)):
            ontology_id = _ontology(db, label, uniq)
            obj = ObjectType(
                ontology_id=ontology_id,
                name=name,
                display_name=display,
                table_role="business_object",
                status=status,
            )
            db.add(obj)
            db.flush()
            made[label] = {"object_id": obj.id, "ontology_id": ontology_id}
            # 同域塞一堆只是子串命中的对象，把两个精确命中挤出首页。
            for i in range(12):
                db.add(
                    ObjectType(
                        ontology_id=ontology_id,
                        name=f"{name}_noise_{i}",
                        display_name=f"{display}的附属表{i}",
                        table_role="technical",
                        status=PUB,
                    )
                )
        db.commit()
    return {"name": name, "display": display, **made}


# ------------------------------------------------------- P1-6 resolve_subject


def test_resolve_subject_puts_exact_matches_first_across_domains(
    call_via_server, subjects
):
    """精确命中必须在截断之前排出来。

    「找 id」是真机上被调用最多的动作：一次会话里 6 次 query_objects + 3 次
    query_object_detail，13 步里 9 步只是在把两个中文词变成 id。
    """
    body = _body(
        call_via_server("resolve_subject", {"keyword": subjects["name"], "limit": 3})
    )
    assert body["success"] is True
    assert body["metadata"]["exact_count"] == 2, "两个域各一个精确同名，一个都不能漏"
    assert body["metadata"]["exact_count_complete"] is True

    top_two = body["data"]["matches"][:2]
    assert {m["id"] for m in top_two} == {
        subjects["odoo"]["object_id"],
        subjects["erpnext"]["object_id"],
    }, "精确命中要顶在最前，不能被子串命中挤掉"
    # 有歧义时不给 exact_match，逼调用方自己挑。
    assert body["data"]["exact_match"] is None
    assert "domain_name" in body["data"]["note"] or "数据域" in body["data"]["note"]


def test_resolve_subject_returns_what_the_next_call_needs(call_via_server, subjects):
    """id 之外还得给全：本体、数据域、角色、发布状态、有没有落点。

    ontology_id 不在读模型 ObjectTypeSummary 上——直接 getattr 会静默给 None，
    而下一步几乎每个工具都要它。
    """
    body = _body(
        call_via_server(
            "resolve_subject",
            {"keyword": subjects["name"], "ontology_id": subjects["erpnext"]["ontology_id"]},
        )
    )
    match = body["data"]["exact_match"]
    assert match is not None, "限定到单个本体后应唯一"
    assert match["id"] == subjects["erpnext"]["object_id"]
    assert match["ontology_id"] == subjects["erpnext"]["ontology_id"]
    assert match["domain_name"]
    assert match["table_role"] == "business_object"
    assert match["status"] == DRAFT
    # 没有落点登记就是 None——调用方据此知道"这个主体还查不了数"。
    assert "landing" in match


def test_resolve_subject_does_not_hide_unpublished_subjects(call_via_server, subjects):
    """默认不按发布状态过滤：一过滤，跨域同名就看起来"唯一"了（get_landing 的旧坑）。"""
    body = _body(call_via_server("resolve_subject", {"keyword": subjects["name"]}))
    ids = {m["id"] for m in body["data"]["matches"]}
    assert subjects["erpnext"]["object_id"] in ids


def test_resolve_subject_says_so_when_nothing_matches(call_via_server):
    body = _body(call_via_server("resolve_subject", {"keyword": "绝不存在的主体名"}))
    assert body["success"] is True
    assert body["data"]["matches"] == []
    assert body["metadata"]["exact_count"] == 0
    assert "没有匹配" in body["data"]["note"]


# --------------------------------------------------- P1-7 list_tasks reconcile


def test_list_tasks_does_not_reconcile_by_default(call_via_server, monkeypatch):
    """默认不逐条回读 Airflow：那是 11.8 秒均耗时的来源，而列目录并不需要终态。"""
    calls: list[str] = []
    from app.services.agent_pipeline import AgentPipelineService

    original = AgentPipelineService._reconcile_orchestrated_status

    def _spy(self, db, artifact):
        calls.append(artifact.id)
        return original(self, db, artifact)

    monkeypatch.setattr(AgentPipelineService, "_reconcile_orchestrated_status", _spy)

    with SessionLocal() as db:
        db.add(
            GovernanceArtifact(
                kind="metric",
                name=f"p1-list-{uuid4().hex[:8]}",
                status=ArtifactStatus.DRAFTED.value,
                spec_json="{}",
            )
        )
        db.commit()

    body = _body(call_via_server("list_tasks", {"limit": 5}))
    assert body["success"] is True
    assert calls == [], "默认不该对账任何一条"
    assert body["metadata"]["reconciled"] is False
    # 说破，免得把制品自陈状态当成远端终态报出去。
    assert "get_task_status" in body["metadata"]["status_note"]

    _body(call_via_server("list_tasks", {"limit": 5, "reconcile": True}))
    assert calls, "显式要求时才对账"


def test_list_tasks_limit_bounds_the_query_not_just_the_slice(call_via_server):
    """limit 要在查询里截断。此前只在调用方切片，对账仍跑满全表。"""
    with SessionLocal() as db:
        for _ in range(4):
            db.add(
                GovernanceArtifact(
                    kind="metric",
                    name=f"p1-page-{uuid4().hex[:8]}",
                    status=ArtifactStatus.DRAFTED.value,
                    spec_json="{}",
                )
            )
        db.commit()
    body = _body(call_via_server("list_tasks", {"limit": 2}))
    assert len(body["data"]["tasks"]) == 2
    assert body["metadata"]["truncated"] is True


# ------------------------------------------------------ P1-8 字段投影


def test_query_objects_defaults_to_a_lean_field_face(call_via_server, subjects):
    """默认不回复核工作台字段——它们随列表成倍放大，对定位实体毫无用处。"""
    lean = _body(
        call_via_server(
            "query_objects",
            {"ontology_id": subjects["odoo"]["ontology_id"], "limit": 5},
        )
    )
    full = _body(
        call_via_server(
            "query_objects",
            {
                "ontology_id": subjects["odoo"]["ontology_id"],
                "limit": 5,
                "fields": ["*"],
            },
        )
    )
    assert lean["metadata"]["fields"] == "lean"
    assert full["metadata"]["fields"] == "all"

    lean_keys = set(lean["data"]["objects"][0])
    full_keys = set(full["data"]["objects"][0])
    assert {"id", "name", "display_name", "table_role", "status"} <= lean_keys
    assert {"conflicts", "pinned_fields", "role_reason"} & full_keys
    assert not ({"conflicts", "pinned_fields", "role_reason"} & lean_keys)
    assert len(json.dumps(lean["data"], ensure_ascii=False)) < len(
        json.dumps(full["data"], ensure_ascii=False)
    )


def test_query_objects_named_fields_keep_identity_and_flag_typos(
    call_via_server, subjects
):
    """点名字段时 id/name/display_name 恒在；点错的字段要说破，不能静默忽略——
    静默忽略会让调用方以为"那个字段是空的"。"""
    body = _body(
        call_via_server(
            "query_objects",
            {
                "ontology_id": subjects["odoo"]["ontology_id"],
                "limit": 2,
                "fields": ["table_role", "没有这个字段"],
            },
        )
    )
    keys = set(body["data"]["objects"][0])
    assert {"id", "name", "display_name", "table_role"} <= keys
    assert "status" not in keys
    assert body["metadata"]["unknown_fields"] == ["没有这个字段"]


def test_overview_item_fields_drop_the_redundant_segment_id(call_via_server, subjects):
    """板块的 id→名映射在同一份回包的 object_distribution 里已经有了。"""
    lean = _body(
        call_via_server(
            "get_ontology_overview", {"ontology_id": subjects["odoo"]["ontology_id"]}
        )
    )
    full = _body(
        call_via_server(
            "get_ontology_overview",
            {"ontology_id": subjects["odoo"]["ontology_id"], "fields": ["*"]},
        )
    )
    assert lean["metadata"]["item_fields"] == "lean"
    items = lean["data"]["business_objects"]["items"]
    if items:
        assert "segment_id" not in items[0]
        assert "segment_name" in items[0]
        assert "segment_id" in full["data"]["business_objects"]["items"][0]


# ------------------------------------------------- P1-9 server_info 瘦身


def test_server_info_default_is_a_role_table_not_a_second_manifest(call_via_server):
    lean = _body(call_via_server("server_info", {}))["data"]
    verbose = _body(call_via_server("server_info", {"verbose": True}))["data"]
    assert "tools" not in lean and "tool_roles" in lean
    assert "tools" in verbose
    assert len(json.dumps(lean, ensure_ascii=False)) < len(
        json.dumps(verbose, ensure_ascii=False)
    ) / 2, "默认应显著小于全文"


# ------------------------------------------- P1-10 条件必填写进 schema


def test_get_ops_record_names_the_families_that_need_ontology_id(call_via_server):
    """描述里原本写的是「task_run/pipeline/draft_run **等**」——那个「等」让调用方
    只能撞一次才知道。两组都要摆明，且从 REGISTRY 现算，加族时自动跟上。"""
    tool = TOOL_REGISTRY["get_ops_record"]
    hint = tool.input_schema["properties"]["ontology_id"]["description"]
    for family in ("task_run", "pipeline", "ontology_version", "data_app", "migration"):
        assert family in hint
    for family in ("standard", "datasource", "component"):
        assert family in hint
    assert "等" not in hint

    # 运行期报错也要给出这两组，而不是只说"需要 ontology_id"。
    result = call_via_server("get_ops_record", {"family": "task_run"})
    assert result.is_error is True
    data = _body(result)["data"]
    assert "task_run" in data["ontology_scoped_families"]
    assert "standard" in data["global_families"]


# ------------------------------------------------- P1-11 SQL 错误噪音


def test_sql_error_is_one_clean_line():
    """服务层已包过一层「查询执行失败：」，SQLAlchemy 还会附上整段 SQL 和文档链接。
    这段文本既进模型上下文，也原样落进审计页给人看。"""
    from app.mcp.tools.sql import _clean_db_error

    raw = Exception(
        '查询执行失败：(pymysql.err.OperationalError) (1051, "Unknown table \'x\'")\n'
        "[SQL: SELECT * FROM x LIMIT 100]\n"
        "(Background on this error at: https://sqlalche.me/e/20/e3q8)"
    )
    cleaned = _clean_db_error(raw)
    assert cleaned.startswith("(pymysql.err.OperationalError)")
    assert "查询执行失败" not in cleaned
    assert "[SQL:" not in cleaned
    assert "sqlalche.me" not in cleaned


# ------------------------------------------ P2-15 同落点重复失败护栏


def test_validate_warns_when_the_same_target_recently_failed(client):
    """真机上同一目标连建 4 条同构任务、前 3 条全失败，校验报告一句没提——
    是模型在正文里自己想起来提醒的。护栏得在闸门里。"""
    from app.agents.validation import is_blocking, validate_spec

    uniq = uuid4().hex[:8]
    spec = {
        "target_datasource_id": f"ds-{uniq}",
        "target_ods_database": "ods",
        "target_ods_table": f"ods_demo_{uniq}",
    }
    with SessionLocal() as db:
        ontology_id = _ontology(db, "dup", uniq)
        for i in range(3):
            db.add(
                GovernanceArtifact(
                    kind="sync",
                    name=f"同步 · demo（{i}）",
                    ontology_id=ontology_id,
                    status=ArtifactStatus.FAILED.value,
                    spec_json=json.dumps(spec),
                )
            )
        db.commit()

        issues = validate_spec(
            db, kind="sync", spec=spec, ontology_id=ontology_id, artifact_id=None
        )
        hits = [i for i in issues if i.code == "duplicate_recent_failure"]
        assert len(hits) == 1
        assert "3 条" in hits[0].message
        # 提醒而非阻断：修好远端问题后重跑本来就是正当动作。
        assert is_blocking(hits[0]) is False


def test_duplicate_guard_excludes_the_artifact_itself(client):
    """不传自己的 id，每条任务都会跟自己"重复"。"""
    from app.agents.validation import validate_spec

    uniq = uuid4().hex[:8]
    spec = {"target_ods_database": "ods", "target_ods_table": f"ods_self_{uniq}"}
    with SessionLocal() as db:
        ontology_id = _ontology(db, "self", uniq)
        artifact = GovernanceArtifact(
            kind="sync",
            name="同步 · self",
            ontology_id=ontology_id,
            status=ArtifactStatus.FAILED.value,
            spec_json=json.dumps(spec),
        )
        db.add(artifact)
        db.commit()

        issues = validate_spec(
            db, kind="sync", spec=spec, ontology_id=ontology_id, artifact_id=artifact.id
        )
        assert not [i for i in issues if i.code == "duplicate_recent_failure"]


def test_duplicate_guard_is_silent_without_a_target(client):
    """取不到落点就无从判重——不猜，也不报。"""
    from app.agents.validation import validate_spec

    with SessionLocal() as db:
        issues = validate_spec(
            db, kind="metric", spec={"foo": "bar"}, ontology_id=None, artifact_id=None
        )
        assert not [i for i in issues if i.code == "duplicate_recent_failure"]
