"""M5 智能体流水线骨架：状态机、Gate 拦截、幂等重放、未确认不得执行。

四条不变量优先级最高：
1. 未确认不得执行
2. Gate 有阻断项时不得进入 validated
3. 已成功的制品重复执行不产生第二次副作用
4. 凭据不得出现在 Spec 中
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.agents import register_builtin_agents, registry
from app.agents.drafters.base import Drafter
from app.agents.executors.base import Executor
from app.database import SessionLocal
from app.governance.standard import DEFAULT_STANDARD
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus


@pytest.fixture(autouse=True)
def restore_builtin_agents():
    """本模块拿假 Drafter 顶掉真实注册（还有几处故意 unregister）——用完必须还回去。

    ``registry`` 是**进程级全局**：只 unregister 不恢复，后面所有用到 metric 等的
    模块就都以为该类「尚未实现」。这类污染只在整套跑时暴露，单跑本文件永远是绿的。
    """
    yield
    register_builtin_agents()  # 幂等


class _FakeDrafter(Drafter):
    kind = "metric"

    def __init__(self, spec: dict[str, Any]):
        self._spec = spec

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        return dict(self._spec)

    def name_from_spec(self, spec: dict[str, Any]) -> str:
        return f"指标 · {spec.get('metric_name')}"


class _CountingExecutor(Executor):
    """记录调用次数，用于验证幂等——幂等靠断言副作用次数，不能只看返回值。"""

    kind = "metric"

    def __init__(self, *, fail: bool = False):
        self.dry_runs = 0
        self.executions = 0
        self._fail = fail

    def dry_run(self, spec, context):
        self.dry_runs += 1
        return {"will_create": spec.get("metric_name"), "rows_affected": 0}

    def execute(self, spec, context):
        self.executions += 1
        if self._fail:
            raise RuntimeError("模拟执行失败")
        return {"created": spec.get("metric_name")}


@pytest.fixture
def metric_agent():
    """注册一对假 Drafter/Executor；真实实现属于 M6。

    ``subject_objects`` 是必填：指标缺主对象会被 Gate 阻断（遗留1）——这里给个绑定
    让制品能走完状态机。它不参与幻觉校验（那只查 object_types/properties），故无需播种本体。
    """
    drafter = _FakeDrafter(
        {"metric_name": "gmv", "engine": "hive", "subject_objects": ["order"]}
    )
    executor = _CountingExecutor()
    registry.register("metric", drafter, executor)
    yield drafter, executor
    registry.unregister("metric")


@pytest.fixture
def ontology_id() -> str:
    with SessionLocal() as db:
        # datahub_domain_id 唯一：用 uuid 避免多个测试共用本 fixture 时插入撞约束。
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:m5-{uuid4().hex[:8]}", name="m5-domain"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(onto)
        db.flush()
        db.add(
            ObjectType(
                ontology_id=onto.id, name="customer", display_name="客户",
                table_role="business_object",
            )
        )
        db.commit()
        return onto.id


def _draft(client, headers, **overrides) -> dict:
    body = {"kind": "metric", "intent": "统计成交额", "context": {}}
    body.update(overrides)
    resp = client.post("/api/agents/draft", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 注册表 ----------


def test_unregistered_kind_returns_501(client, admin_headers):
    """未注册的 kind 必须 501，不能静默放行。

    四类实现均已注册，故此处临时反注册一个来验证该分支仍然生效——
    registry 允许部分填充，这条路径不能因为「现在都实现了」就失去覆盖。
    """
    from app.agents import register_builtin_agents

    registry.unregister("materialize")
    try:
        resp = client.post(
            "/api/agents/draft",
            headers=admin_headers,
            json={"kind": "materialize", "intent": "物化本体"},
        )
        assert resp.status_code == 501
        assert "M6" in resp.json()["detail"]
    finally:
        register_builtin_agents()


def test_kinds_endpoint_shows_progress(client, admin_headers):
    body = client.get("/api/agents/kinds", headers=admin_headers).json()
    assert set(body["all_kinds"]) == {
        "sync",
        "transform",
        "metric",
        "materialize",
    }
    # 四类制品均已注册（含 M3+ 本体一键物化）
    assert set(body["registered"]) == {
        "sync",
        "transform",
        "metric",
        "materialize",
    }
    # 移除 cluster 后无高危制品（HIGH_RISK_KINDS 置空）
    assert body["high_risk"] == []


def test_unknown_kind_rejected(client, admin_headers):
    resp = client.post(
        "/api/agents/draft", headers=admin_headers, json={"kind": "nope", "intent": "x"}
    )
    assert resp.status_code in (409, 501)


# ---------- 状态机 ----------


def test_happy_path_state_machine(client, admin_headers, metric_agent):
    _, executor = metric_agent
    a = _draft(client, admin_headers)
    assert a["status"] == "drafted"

    a = client.post(
        f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}
    ).json()
    assert a["status"] == "validated"
    assert a["validation_report"]["blocking_count"] == 0
    assert a["validation_report"]["dry_run"]["will_create"] == "gmv"
    # G3 版本戳：校验报告记下过闸时的规约版本（审计 / 规约升级后判是否需 re-lint）。
    assert a["validation_report"]["standard_version"] == DEFAULT_STANDARD.version
    assert executor.dry_runs == 1

    a = client.post(
        f"/api/agents/artifacts/{a['id']}/confirm",
        headers=admin_headers, json={"operator": "张三"},
    ).json()
    assert a["status"] == "confirmed"
    assert a["confirmed_by"] == "张三"

    a = client.post(
        f"/api/agents/artifacts/{a['id']}/execute", headers=admin_headers, json={}
    ).json()
    assert a["status"] == "succeeded"
    assert a["execution_receipt"]["created"] == "gmv"
    assert executor.executions == 1


def test_cannot_execute_without_confirm(client, admin_headers, metric_agent):
    """最重要的一条：未经人工确认的制品不得执行。"""
    _, executor = metric_agent
    a = _draft(client, admin_headers)
    resp = client.post(
        f"/api/agents/artifacts/{a['id']}/execute", headers=admin_headers, json={}
    )
    assert resp.status_code == 409
    assert "未经人工确认" in resp.json()["detail"]
    assert executor.executions == 0

    # 校验过但未确认，仍然不许执行
    client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={})
    resp = client.post(
        f"/api/agents/artifacts/{a['id']}/execute", headers=admin_headers, json={}
    )
    assert resp.status_code == 409
    assert executor.executions == 0


def test_cannot_confirm_without_validate(client, admin_headers, metric_agent):
    a = _draft(client, admin_headers)
    resp = client.post(
        f"/api/agents/artifacts/{a['id']}/confirm", headers=admin_headers, json={}
    )
    assert resp.status_code == 409
    assert "请先执行校验" in resp.json()["detail"]


def test_execution_failure_marks_failed(client, admin_headers):
    registry.register("metric", _FakeDrafter({"metric_name": "x", "subject_objects": ["order"]}), _CountingExecutor(fail=True))
    try:
        a = _draft(client, admin_headers)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        a = client.post(f"/api/agents/artifacts/{a['id']}/confirm", headers=admin_headers, json={}).json()
        a = client.post(f"/api/agents/artifacts/{a['id']}/execute", headers=admin_headers, json={}).json()
        assert a["status"] == "failed"
        assert "模拟执行失败" in a["execution_receipt"]["error"]
    finally:
        registry.unregister("metric")


# ---------- 幂等 ----------


def test_execute_is_idempotent(client, admin_headers, metric_agent):
    """重复执行不得产生第二次副作用——断言执行器调用次数，不能只看返回值。"""
    _, executor = metric_agent
    a = _draft(client, admin_headers)
    aid = a["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})

    first = client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}).json()
    second = client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}).json()

    assert executor.executions == 1, "已成功的制品被重复执行了"
    assert first["execution_receipt"] == second["execution_receipt"]
    assert second["status"] == "succeeded"


def test_cannot_revalidate_succeeded(client, admin_headers, metric_agent):
    a = _draft(client, admin_headers)
    aid = a["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={})
    resp = client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    assert resp.status_code == 409


# ---------- 编辑（PATCH） ----------


def test_edit_drafted_updates_spec_stays_drafted(client, admin_headers, metric_agent):
    """drafted 态编辑 context → drafter 重派生 spec，状态仍 drafted。"""
    drafter, _ = metric_agent
    a = _draft(client, admin_headers)
    aid = a["id"]
    # 改 drafter 会派生出的 spec（假 drafter 直接回传注入的 spec，这里改注入内容）
    drafter._spec = {"metric_name": "dau", "engine": "hive", "subject_objects": ["user"]}
    resp = client.patch(
        f"/api/agents/artifacts/{aid}",
        headers=admin_headers,
        json={"context": {"business_logic_id": "x"}, "name": "日活"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "drafted"
    assert body["spec"]["metric_name"] == "dau"
    assert body["name"] == "日活"


def test_edit_validated_resets_to_drafted_and_clears_report(
    client, admin_headers, metric_agent
):
    """validated 态编辑 → 状态打回 drafted，旧校验报告清空。"""
    drafter, _ = metric_agent
    a = _draft(client, admin_headers)
    aid = a["id"]
    a = client.post(
        f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={}
    ).json()
    assert a["status"] == "validated"
    assert a["validation_report"] is not None

    resp = client.patch(
        f"/api/agents/artifacts/{aid}",
        headers=admin_headers,
        json={"spec": {"metric_name": "gmv2", "subject_objects": ["order"]}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "drafted"
    assert body["validation_report"] is None  # 旧校验对新 spec 已失效
    assert body["spec"]["metric_name"] == "gmv2"


def test_edit_failed_is_allowed(client, admin_headers):
    """failed 态（执行失败）可编辑，回到 drafted 重试。"""
    registry.register(
        "metric",
        _FakeDrafter({"metric_name": "x", "subject_objects": ["order"]}),
        _CountingExecutor(fail=True),
    )
    try:
        a = _draft(client, admin_headers)
        aid = a["id"]
        client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
        client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})
        a = client.post(
            f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}
        ).json()
        assert a["status"] == "failed"

        resp = client.patch(
            f"/api/agents/artifacts/{aid}",
            headers=admin_headers,
            json={"spec": {"metric_name": "y", "subject_objects": ["order"]}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "drafted"
    finally:
        registry.unregister("metric")


def test_cannot_edit_confirmed(client, admin_headers, metric_agent):
    """confirmed 态不可编辑（人已背书某版 spec，改动须新建）→ 409。"""
    a = _draft(client, admin_headers)
    aid = a["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})
    resp = client.patch(
        f"/api/agents/artifacts/{aid}",
        headers=admin_headers,
        json={"spec": {"metric_name": "z", "subject_objects": ["order"]}},
    )
    assert resp.status_code == 409
    assert "不可编辑" in resp.json()["detail"]


def test_cannot_edit_succeeded(client, admin_headers, metric_agent):
    """succeeded 态不可编辑 → 409（已产生副作用，改动须新建）。"""
    a = _draft(client, admin_headers)
    aid = a["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={})
    resp = client.patch(
        f"/api/agents/artifacts/{aid}",
        headers=admin_headers,
        json={"spec": {"metric_name": "z", "subject_objects": ["order"]}},
    )
    assert resp.status_code == 409


def test_edit_empty_payload_rejected(client, admin_headers, metric_agent):
    """既不给 spec 也不给 intent/context → 400（没东西可改）。"""
    a = _draft(client, admin_headers)
    resp = client.patch(
        f"/api/agents/artifacts/{a['id']}", headers=admin_headers, json={}
    )
    assert resp.status_code == 400


# ---------- Validation Gate ----------


def test_gate_blocks_missing_required_field(client, admin_headers):
    registry.register("metric", _FakeDrafter({"engine": "hive"}), _CountingExecutor())
    try:
        a = _draft(client, admin_headers)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        assert a["status"] == "drafted", "有阻断项却进入了 validated"
        codes = {i["code"] for i in a["validation_report"]["issues"]}
        assert "missing_required_field" in codes
    finally:
        registry.unregister("metric")


def test_gate_blocks_credentials_in_spec(client, admin_headers):
    """凭据绝不允许进入 Spec——LLM 上下文与制品存储都不得承载密钥。"""
    registry.register(
        "metric",
        _FakeDrafter({"metric_name": "gmv", "ssh_password": "hunter2"}),
        _CountingExecutor(),
    )
    try:
        a = _draft(client, admin_headers)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        assert a["status"] == "drafted"
        codes = {i["code"] for i in a["validation_report"]["issues"]}
        assert "credential_in_spec" in codes
    finally:
        registry.unregister("metric")


def test_gate_blocks_hallucinated_object(client, admin_headers, ontology_id):
    """防幻觉：Spec 引用的对象必须在本体中真实存在。"""
    registry.register(
        "metric",
        _FakeDrafter({"metric_name": "gmv", "object_types": ["customer", "不存在的表"]}),
        _CountingExecutor(),
    )
    try:
        a = _draft(client, admin_headers, ontology_id=ontology_id)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        assert a["status"] == "drafted"
        issues = a["validation_report"]["issues"]
        hallucinated = [i for i in issues if i["code"] == "unknown_object"]
        assert len(hallucinated) == 1
        assert hallucinated[0]["entity_name"] == "不存在的表"
    finally:
        registry.unregister("metric")


def test_gate_blocks_unknown_engine(client, admin_headers):
    registry.register(
        "metric", _FakeDrafter({"metric_name": "gmv", "engine": "teradata"}), _CountingExecutor()
    )
    try:
        a = _draft(client, admin_headers)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        assert a["status"] == "drafted"
        codes = {i["code"] for i in a["validation_report"]["issues"]}
        assert "engine_unknown" in codes
    finally:
        registry.unregister("metric")


def test_verified_engine_has_no_unverified_warning(client, admin_headers):
    """M8 后四引擎能力矩阵均已核实——不再产生 engine_unverified 警告，正常进入 validated。"""
    registry.register(
        "metric", _FakeDrafter({"metric_name": "gmv", "engine": "doris", "subject_objects": ["order"]}), _CountingExecutor()
    )
    try:
        a = _draft(client, admin_headers)
        a = client.post(f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}).json()
        assert a["status"] == "validated"
        codes = {i["code"] for i in a["validation_report"]["issues"]}
        assert "engine_unverified" not in codes
    finally:
        registry.unregister("metric")


# ---------- 权限 ----------


def test_agents_namespace_requires_publisher(client, admin_headers):
    created = client.post(
        "/api/principals", headers=admin_headers, json={"name": "写侧编辑", "role": "editor"}
    ).json()
    editor = {"X-Admin-Token": created["token"]}
    for method, path in [
        ("GET", "/api/agents/artifacts"),
        ("POST", "/api/agents/draft"),
    ]:
        resp = client.request(method, path, headers=editor, json={})
        assert resp.status_code == 403, f"{method} {path} 应拒绝 editor"
    client.delete(f"/api/principals/{created['id']}", headers=admin_headers)


# ---------- 列表与详情 ----------


def test_list_and_filter(client, admin_headers, metric_agent):
    a = _draft(client, admin_headers)
    rows = client.get(
        "/api/agents/artifacts", headers=admin_headers, params={"kind": "metric"}
    ).json()
    assert any(r["id"] == a["id"] for r in rows)

    detail = client.get(f"/api/agents/artifacts/{a['id']}", headers=admin_headers).json()
    assert detail["spec"]["metric_name"] == "gmv"
    assert detail["is_high_risk"] is False


def test_missing_artifact_404(client, admin_headers):
    assert client.get(
        "/api/agents/artifacts/nope", headers=admin_headers
    ).status_code == 404


# ---------- 手动结构化 spec 起草路径（跳过 drafter，仍进闸门） ----------


def test_spec_path_bypasses_drafter(client, admin_headers, metric_agent):
    """给了 spec 就直接落库，不调 drafter。

    _FakeDrafter 恒返回 {metric_name: gmv,...}；这里显式传一份不同的 spec，
    落库若是我传的那份，即证明 drafter 未被调用。"""
    drafter, _ = metric_agent
    my_spec = {"metric_name": "dau", "engine": "hive", "subject_objects": ["user"]}
    a = _draft(client, admin_headers, intent=None, spec=my_spec, name="日活")
    assert a["status"] == "drafted"
    assert a["spec"]["metric_name"] == "dau"  # 不是 drafter 的 gmv
    assert a["name"] == "日活"
    assert a["origin"] == "user"


def test_spec_path_name_derived_when_absent(client, admin_headers, metric_agent):
    """spec 路径不给 name 时，用 drafter.name_from_spec 派生。"""
    a = _draft(
        client, admin_headers, intent=None,
        spec={"metric_name": "gmv", "subject_objects": ["order"]}, name=None,
    )
    assert a["name"] == "指标 · gmv"


def test_intent_required_without_spec(client, admin_headers, metric_agent):
    """既没 spec、也没 intent、context 还空 → 400（三者全缺无从起草）。"""
    resp = client.post(
        "/api/agents/draft", headers=admin_headers,
        json={"kind": "metric", "context": {}},
    )
    assert resp.status_code == 400


def test_context_path_without_intent_succeeds(client, admin_headers, metric_agent):
    """表单起草路径：只给结构化 context、无自然语言 intent 也能起草。

    表单收的是 drafter 输入（下拉选择器），没有 intent 文本；此前 guard 强制 intent
    非空会把整条表单路径挡死。给了非空 context 即放行，drafter 用显式选择器派生 spec。
    并断言溯源标 user（user_created=True 由表单显式声明），而非 machine。"""
    resp = client.post(
        "/api/agents/draft", headers=admin_headers,
        json={
            "kind": "metric",
            "context": {"ontology_id": "x", "business_logic_id": "bl-1"},
            "user_created": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "drafted"
    assert body["origin"] == "user"


def test_spec_path_still_runs_gate(client, admin_headers, metric_agent, ontology_id):
    """手填 spec 引用不存在的对象 → 校验闸门仍报 unknown_object（安全边界未被绕过）。"""
    a = _draft(
        client, admin_headers, intent=None, ontology_id=ontology_id,
        spec={"metric_name": "x", "subject_objects": ["ghost"],
              "object_types": ["ghost"]},
    )
    report = client.post(
        f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}
    ).json()["validation_report"]
    assert report["blocking_count"] >= 1
    assert any(i["code"] == "unknown_object" for i in report["issues"])


def test_spec_path_produces_dry_run(client, admin_headers, metric_agent):
    """手填 spec 无阻断项时，校验照常产 dry-run（执行方案预览）。"""
    _, executor = metric_agent
    a = _draft(
        client, admin_headers, intent=None,
        spec={"metric_name": "gmv", "subject_objects": ["order"]},
    )
    a = client.post(
        f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}
    ).json()
    assert a["status"] == "validated"
    assert a["validation_report"]["dry_run"]["will_create"] == "gmv"
    assert executor.dry_runs == 1


# ---------- P0b：读时对账 orchestrated 制品状态 ----------


def test_reconcile_failed_dagrun_writes_back_status(monkeypatch):
    """orchestrated 制品 status=succeeded 但 Airflow 报 DagRun failed → 读时回写 FAILED。"""
    from unittest.mock import MagicMock, patch

    from app.models.agent import ArtifactStatus, GovernanceArtifact
    from app.services.agent_pipeline import AgentPipelineService

    # 构造一个 orchestrated 回执的 SUCCEEDED 制品
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="materialize",
            name="test-reconcile",
            status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json='{"execute_mode": "orchestrated", "dag_id": "dag1", '
            '"dag_run_id": "run1", "batches": [{"dag_id": "dag1", "dag_run_id": "run1"}]}',
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    # Mock Airflow 返回 failed
    mock_client = MagicMock()
    mock_client.get_dag_run.return_value = {"state": "failed"}
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    mock_rt = MagicMock()
    mock_rt.available = True

    # Patch 在 _reconcile_orchestrated_status 内部动态导入的位置
    with patch("app.services.settings_service.SettingsService") as mock_settings_cls:
        mock_settings_cls.return_value.get_airflow_runtime.return_value = mock_rt
        with patch("app.connectors.airflow.AirflowClient", return_value=mock_client):
            # 读取制品 → 触发对账
            svc = AgentPipelineService()
            with SessionLocal() as db:
                artifact = svc.get(db, artifact_id)
                assert artifact is not None
                # 对账后 status 应被回写为 FAILED
                assert artifact.status == ArtifactStatus.FAILED.value


def test_reconcile_running_dagrun_keeps_succeeded(monkeypatch):
    """orchestrated 制品 Airflow 报 DagRun running（非终态）→ status 保持 SUCCEEDED，不回写。"""
    from unittest.mock import MagicMock, patch

    from app.models.agent import ArtifactStatus, GovernanceArtifact
    from app.services.agent_pipeline import AgentPipelineService

    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="materialize",
            name="test-reconcile-running",
            status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json='{"execute_mode": "orchestrated", "dag_id": "dag2", '
            '"dag_run_id": "run2", "batches": [{"dag_id": "dag2", "dag_run_id": "run2"}]}',
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    mock_client = MagicMock()
    mock_client.get_dag_run.return_value = {"state": "running"}
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    mock_rt = MagicMock()
    mock_rt.available = True

    with patch("app.services.settings_service.SettingsService") as mock_settings_cls:
        mock_settings_cls.return_value.get_airflow_runtime.return_value = mock_rt
        with patch("app.connectors.airflow.AirflowClient", return_value=mock_client):
            svc = AgentPipelineService()
            with SessionLocal() as db:
                artifact = svc.get(db, artifact_id)
                assert artifact is not None
                # running 非终态 → status 保持 SUCCEEDED（live_state 会展示 running）
                assert artifact.status == ArtifactStatus.SUCCEEDED.value


def test_reconcile_skips_non_orchestrated(monkeypatch):
    """非 orchestrated 制品（如 metric）不触发对账，不调 Airflow。"""
    from unittest.mock import MagicMock, patch

    from app.models.agent import ArtifactStatus, GovernanceArtifact
    from app.services.agent_pipeline import AgentPipelineService

    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="metric",
            name="test-no-reconcile",
            status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json='{"created": "gmv"}',  # 无 execute_mode
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    mock_client = MagicMock()
    with patch("app.connectors.airflow.AirflowClient", return_value=mock_client):
        svc = AgentPipelineService()
        with SessionLocal() as db:
            artifact = svc.get(db, artifact_id)
            assert artifact is not None
            # 不应调 Airflow
            mock_client.get_dag_run.assert_not_called()
            assert artifact.status == ArtifactStatus.SUCCEEDED.value



# ---------- 回执自陈失败 → 制品必须显示失败 ----------


class _ReceiptExecutor(Executor):
    """按注入的回执返回，且**不抛异常**——模拟 runner「投递失败如实记进回执」的约定。"""

    kind = "metric"

    def __init__(self, receipt: dict[str, Any]):
        self._receipt = receipt

    def dry_run(self, spec, context):
        return {"will_create": spec.get("metric_name"), "rows_affected": 0}

    def execute(self, spec, context):
        return dict(self._receipt)


def _run_to_execute(client, headers, **draft_overrides) -> dict:
    a = _draft(client, headers, **draft_overrides)
    aid = a["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=headers, json={})
    resp = client.post(f"/api/agents/artifacts/{aid}/execute", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    "receipt",
    [
        # materialize 投递期失败的真实形状（Airflow 没解析到 DAG）
        {
            "ok": False,
            "state": "failed",
            "execute_mode": "orchestrated",
            "error": "Airflow 尚未解析到 DAG",
        },
        # 顶层没自陈、但某一批失败（按 cron 分组的多批物化）
        {
            "execute_mode": "orchestrated",
            "batches": [
                {"dag_id": "d1", "dag_run_id": "r1", "state": "queued"},
                {"dag_id": "d2", "state": "failed", "error": "触发失败"},
            ],
        },
    ],
    ids=["top_level_failed", "one_batch_failed"],
)
def test_execute_marks_failed_when_receipt_says_failed(
    client, admin_headers, receipt
):
    """执行器不抛异常 ≠ 成功。

    runner 的约定是投递失败写进回执的 ok/state/error 而不抛；此前 execute() 一律置
    succeeded，于是「Airflow 根本没解析到 DAG」的物化任务在列表里显示成功——用户据此
    以为数据已经落库。制品状态必须跟着回执走。
    """
    registry.register("metric", _FakeDrafter(
        {"metric_name": "gmv", "engine": "hive", "subject_objects": ["order"]}
    ), _ReceiptExecutor(receipt))
    try:
        out = _run_to_execute(client, admin_headers)
    finally:
        registry.unregister("metric")
    assert out["status"] == "failed"


def test_execute_keeps_succeeded_for_produce_only_receipt(client, admin_headers):
    """「仅产出」回执（无 ok/无 state）不算失败——不能把正常产出误判成失败。"""
    registry.register("metric", _FakeDrafter(
        {"metric_name": "gmv", "engine": "hive", "subject_objects": ["order"]}
    ), _ReceiptExecutor({"artifacts": {"ddl": "CREATE TABLE ..."}, "handoff": "DolphinScheduler"}))
    try:
        out = _run_to_execute(client, admin_headers)
    finally:
        registry.unregister("metric")
    assert out["status"] == "succeeded"


# ---------- 派生名唯一 ----------


def test_derived_names_are_deduped(client, admin_headers, metric_agent):
    """不给 name 时派生名只由 spec/intent 决定，同配置必然同名——须自动加序号区分。"""
    intent = f"统计{uuid4().hex[:8]}"  # 与本模块其它用例的派生名隔离
    names = [
        _draft(client, admin_headers, name=None, intent=intent)["name"]
        for _ in range(3)
    ]
    assert names == [intent, f"{intent}（2）", f"{intent}（3）"]


def test_explicit_name_is_never_rewritten(client, admin_headers, metric_agent):
    """人自己起的名一律原样保留，重名也不改——改用户输入比重名更糟。"""
    a = _draft(client, admin_headers, name="我的指标任务")
    b = _draft(client, admin_headers, name="我的指标任务")
    assert a["name"] == b["name"] == "我的指标任务"


# ---------- 问题清单排序 ----------


def test_preflight_issues_rank_above_ontology_noise():
    """自检项必须排在本体存量噪声前面。

    ERP 本体一次校验产出 188 条 issue，185 条是与本次执行无关的 ontology_issue；
    「Airflow 没解析到 DAG」这条恰是执行必失败的原因，排在后面等于没提示。
    """
    from app.services.agent_pipeline import _rank_issues
    from app.services.draft_consistency import ValidationIssue

    ranked = _rank_issues([
        ValidationIssue(code="ontology_issue", message="噪声1", entity_type="object_type"),
        ValidationIssue(code="preflight_warning", message="DAG 目录", entity_type="artifact"),
        ValidationIssue(code="ontology_issue", message="噪声2", entity_type="object_type"),
        ValidationIssue(code="preflight_blocked", message="建表连接", entity_type="artifact"),
        ValidationIssue(code="missing_required_field", message="缺字段", entity_type="artifact"),
    ])
    assert [i.code for i in ranked] == [
        "missing_required_field",
        "preflight_blocked",
        "preflight_warning",
        "ontology_issue",
        "ontology_issue",
    ]


def test_execute_injects_artifact_id_into_context():
    """制品 id 由流水线注入执行上下文，不指望调用方回传。

    执行器拿它作 dag_run_id。前端调 execute 不传 context，此前 id 一路为空、run_id
    退化成 ontometa__manual__manual：同一本体的第二个任务撞同一个 run_id，读时对账
    还会拿别人的 DagRun 状态回写自己的制品。
    """
    from app.models.agent import ArtifactStatus, GovernanceArtifact
    from app.services.agent_pipeline import AgentPipelineService

    seen: dict = {}

    class _Spy(Executor):
        kind = "sync"

        def dry_run(self, spec, context):
            return {"action": "noop", "side_effects": "无"}

        def execute(self, spec, context):
            seen.update(context)
            return {"ok": True}

    svc = AgentPipelineService()
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="sync", name="run-id", status=ArtifactStatus.CONFIRMED.value,
            spec_json='{"source": "a.b", "target": "dim.b"}',
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

        original = registry.get_executor("sync")
        registry.register("sync", registry.get_drafter("sync"), _Spy())
        try:
            svc.execute(db, artifact_id)
        finally:
            registry.register("sync", registry.get_drafter("sync"), original)

    assert seen.get("artifact_id") == artifact_id
