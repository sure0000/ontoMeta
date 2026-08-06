"""M6 四类 Drafter/Executor：④指标 → ③ETL → ①同步 → ⓪部署（风险由低到高）。

贯穿全部四类的两条约束：
- **凭据不进 Spec**：只允许主机/数据源别名。
- **ontoMeta 只生成、不执行**：执行器产出可部署产物并交接，不直接改集群。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.agents.drafters.cluster import ClusterDrafter
from app.agents.drafters.metric import MetricDrafter
from app.agents.drafters.sync import SyncDrafter, decide_preservation
from app.agents.drafters.transform import TransformDrafter
from app.agents.executors.cluster import ClusterExecutor
from app.agents.executors.metric import MetricExecutor
from app.agents.executors.sync import SyncExecutor
from app.agents.executors.transform import TransformExecutor
from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp_ods.{t},PROD)"


@pytest.fixture
def seeded(client, admin_headers) -> dict:
    """一个可用于全部四类制品的本体：客户维表 + 成交额指标。

    每次用独立 domain：其一避开 ``datahub_domain_id`` 唯一约束，
    其二让个别会改数据的用例（如置空 source_ref）不污染其它用例。
    """
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:m6-{tag}", name=f"m6-{tag}")
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(onto)
        db.flush()

        customer = ObjectType(
            ontology_id=onto.id, name="customer", display_name="客户",
            table_role="business_object", source_ref=_URN.format(t="tab_customer"),
        )
        order_log = ObjectType(
            ontology_id=onto.id, name="order_audit_log", display_name="订单审计日志",
            table_role="business_object", source_ref=_URN.format(t="tab_order_log"),
        )
        db.add_all([customer, order_log])
        db.flush()

        amount = Property(
            object_type_id=customer.id, name="amount", display_name="金额",
            data_type="decimal", semantic_type="amount",
        )
        region = Property(
            object_type_id=customer.id, name="region", display_name="地区",
            data_type="varchar", semantic_type="category",
        )
        db.add_all([
            amount, region,
            Property(object_type_id=customer.id, name="created_at",
                     display_name="创建时间", data_type="timestamp",
                     semantic_type="datetime"),
            Property(object_type_id=order_log.id, name="log_id",
                     display_name="日志ID", data_type="bigint",
                     semantic_type="identifier"),
        ])
        db.flush()

        gmv = BusinessLogic(
            ontology_id=onto.id, name="gmv", display_name="成交额",
            logic_type="metric", expression_summary="SUM(amount)",
        )
        db.add(gmv)
        db.flush()
        db.add_all([
            BusinessLogicObjectBinding(
                business_logic_id=gmv.id, object_type_id=customer.id, role="subject"
            ),
            BusinessLogicPropertyBinding(
                business_logic_id=gmv.id, property_id=amount.id, role="input"
            ),
            BusinessLogicPropertyBinding(
                business_logic_id=gmv.id, property_id=region.id, role="group"
            ),
        ])
        db.commit()
        ids = {"ontology_id": onto.id, "customer_id": customer.id}

    client.post(
        f"/api/ontologies/{ids['ontology_id']}/materialization-contracts/sync",
        headers=admin_headers,
    )
    return ids


# ---------- ④ 指标 ----------


def test_metric_drafter_reads_existing_business_logic(seeded):
    """指标口径由人定，Drafter 只挑选与结构化，不编口径。"""
    spec = MetricDrafter().draft("统计成交额", {"ontology_id": seeded["ontology_id"]})
    assert spec["metric_name"] == "gmv"
    assert spec["expression"] == "SUM(amount)"
    assert spec["subject_objects"] == ["customer"]
    # 绑定角色决定 SQL 结构
    assert spec["group_by"] == ["region"]
    assert spec["inputs"] == ["amount"]
    assert spec["target_layer"] == "ads"


def test_metric_drafter_refuses_when_no_logic_matches(seeded):
    with pytest.raises(ValueError, match="未在本体中找到匹配的业务逻辑"):
        MetricDrafter().draft("统计某个不存在的东西", {"ontology_id": seeded["ontology_id"]})


def test_metric_executor_renders_ddl_and_sql(seeded):
    spec = MetricDrafter().draft("成交额", {"ontology_id": seeded["ontology_id"]})
    out = MetricExecutor().execute(spec, {})
    assert out["target_table"] == "ads.gmv"
    assert "`metric_value` DECIMAL(18,4)" in out["ddl"]
    # 维度进结果表，指标才可下钻
    assert "`region`" in out["ddl"]
    assert "SUM(amount) AS metric_value" in out["sql"]
    assert "GROUP BY region" in out["sql"]
    assert out["handoff"] == "DolphinScheduler"


def test_metric_dry_run_has_no_side_effects(seeded):
    spec = MetricDrafter().draft("成交额", {"ontology_id": seeded["ontology_id"]})
    diff = MetricExecutor().dry_run(spec, {})
    assert diff["action"] == "create_or_replace_metric_table"
    assert "无" in diff["side_effects"]


def test_metric_executor_is_idempotent(seeded):
    spec = MetricDrafter().draft("成交额", {"ontology_id": seeded["ontology_id"]})
    ex = MetricExecutor()
    assert ex.execute(spec, {}) == ex.execute(spec, {})


# ---------- ③ ETL ----------


def test_transform_drafter_structures_cleansing_rules(seeded):
    spec = TransformDrafter().draft(
        "把客户表去重并过滤空值",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer"},
    )
    assert spec["target_table"] == "customer"
    rules = {r["rule"] for r in spec["cleansing_rules"]}
    assert rules == {"deduplicate", "drop_null"}


def test_transform_drafter_keeps_unmatched_intent_as_notes(seeded):
    """识别不了的需求原文保留，不臆造规则。"""
    spec = TransformDrafter().draft(
        "按业务口径做点特殊处理",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer"},
    )
    assert spec["cleansing_rules"] == []
    assert "特殊处理" in spec["notes"]


def test_transform_executor_reuses_m3_generator(seeded):
    spec = TransformDrafter().draft(
        "客户表去重",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer",
         "database_prefix": "erp"},
    )
    out = TransformExecutor().execute(spec, {})
    assert out["target_table"] == "dim_erp.customer"
    assert "INSERT OVERWRITE TABLE dim_erp.customer" in out["sql"]
    assert "FROM erp_ods.tab_customer" in out["sql"]
    assert out["applied_rules"] == ["deduplicate"]
    assert "SELECT DISTINCT" in out["sql"]


def test_transform_reports_unapplied_rules(seeded):
    """无法确定性表达的规则显式列出，不静默丢弃。"""
    spec = TransformDrafter().draft(
        "客户表统一转大写",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer"},
    )
    out = TransformExecutor().execute(spec, {})
    assert out["unapplied_rules"] == ["uppercase"]


# ---------- ① 同步 ----------


@pytest.mark.parametrize(
    "intent,table,preserve",
    [
        ("同步订单审计日志", "tab_order_log", True),
        ("同步 binlog 变更流", "tab_x", True),
        ("同步客户主数据", "tab_customer", False),
        ("同步码表", "tab_code", False),
    ],
)
def test_preservation_decision_rules(intent, table, preserve):
    """关键源保全判定：可随时全量重拉的不保全，以省存储。"""
    assert decide_preservation(intent, table)["preserve"] is preserve


def test_sync_drafter_maps_via_ontology_not_raw_copy(seeded):
    spec = SyncDrafter().draft(
        "同步客户数据",
        {"ontology_id": seeded["ontology_id"], "object_type": "customer",
         "database_prefix": "erp"},
    )
    # 源由 source_ref 定位，目标结构由本体决定
    assert spec["source"] == "erp_ods.tab_customer"
    assert spec["target"] == "dim_erp.customer"
    assert spec["mode"] == "incremental"  # 有 datetime 字段
    assert spec["partition_key"] == "created_at"
    assert spec["preservation"]["preserve"] is False


def test_sync_executor_emits_stg_job_when_preserved(seeded):
    spec = SyncDrafter().draft(
        "同步订单审计日志",
        {"ontology_id": seeded["ontology_id"], "object_type": "order_audit_log"},
    )
    assert spec["preservation"]["preserve"] is True
    out = SyncExecutor().execute(spec, {})
    assert out["preserved"] is True
    assert any(k.startswith("preserve_") for k in out["jobs"])


def test_sync_job_contains_alias_not_credentials(seeded):
    """作业配置里只能有数据源别名，不得出现连接串或口令。"""
    spec = SyncDrafter().draft(
        "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
    )
    out = SyncExecutor().execute(spec, {})
    blob = json.dumps(out["jobs"], ensure_ascii=False).lower()
    assert "datasource_alias" in blob
    for leak in ("password", "jdbc:", "://", "secret"):
        assert leak not in blob, f"作业配置中泄漏了 {leak}"


def test_sync_refuses_object_without_source_ref(seeded):
    with SessionLocal() as db:
        obj = db.get(ObjectType, seeded["customer_id"])
        obj.source_ref = None
        db.commit()
    with pytest.raises(ValueError, match="无 source_ref"):
        SyncDrafter().draft(
            "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
        )


# ---------- ⓪ 部署 ----------


def test_cluster_drafter_produces_aliases_only():
    spec = ClusterDrafter().draft(
        "部署 hdfs yarn hive spark 三节点",
        {"hosts": ["node-1", "node-2", "node-3"]},
    )
    assert spec["hosts"] == ["node-1", "node-2", "node-3"]
    assert set(spec["services"]) >= {"hdfs", "yarn", "hive", "spark"}
    assert spec["credential_ref"] == "cluster_ssh_default"
    # Spec 里不得有任何明文凭据
    blob = json.dumps(spec).lower()
    for leak in ("password", "private_key", "secret"):
        assert leak not in blob


def test_cluster_drafter_rejects_credentials_in_context():
    """凭据一旦进入上下文就拒绝——安全边界不能只靠事后扫描。"""
    with pytest.raises(ValueError, match="不得包含凭据字段"):
        ClusterDrafter().draft(
            "部署 hdfs", {"hosts": ["n1"], "ssh_password": "hunter2"}
        )


def test_cluster_drafter_rejects_inline_credential_host():
    with pytest.raises(ValueError, match="疑似内联凭据"):
        ClusterDrafter().draft("部署 hdfs", {"hosts": ["root@n1:pass"]})


def test_cluster_drafter_rejects_services_bm_cannot_manage():
    """BM 管不了的组件在起草期就拒绝，好过跑到执行期失败。"""
    with pytest.raises(ValueError, match="Bigtop Manager 不纳管"):
        ClusterDrafter().draft(
            "部署 dolphinscheduler", {"hosts": ["n1"], "services": ["dolphinscheduler"]}
        )
    with pytest.raises(ValueError, match="Bigtop Manager 不纳管"):
        ClusterDrafter().draft("部署", {"hosts": ["n1"], "services": ["ranger"]})


def test_cluster_executor_does_not_dispatch_without_explicit_opt_in():
    """默认只产载荷并停在交接点，不发起真实调用——下发须显式 opt-in。"""
    spec = ClusterDrafter().draft("部署 hdfs", {"hosts": ["n1", "n2"]})
    out = ClusterExecutor().execute(spec, {})
    assert out["dispatched"] is False
    assert "allow_dispatch=true" in out["note"]
    assert "非生产集群核实" in out["note"]
    assert out["payload"]["credential_ref"] == "cluster_ssh_default"


def test_cluster_dry_run_flags_irreversibility():
    spec = ClusterDrafter().draft("部署 hdfs", {"hosts": ["n1"]})
    diff = ClusterExecutor().dry_run(spec, {})
    assert diff["irreversible"] is True
    assert diff["host_count"] == 1
    assert "不可自动回滚" in diff["side_effects"]


# ---------- 与流水线的衔接 ----------


def test_credential_ref_not_flagged_as_credential_leak(client, admin_headers):
    """``credential_ref`` 是指向密钥存储的引用，正是鼓励的写法，Gate 须放行。"""
    resp = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={"kind": "cluster", "intent": "部署 hdfs yarn", "context": {"hosts": ["n1"]}},
    )
    assert resp.status_code == 200, resp.text
    aid = resp.json()["id"]
    a = client.post(
        f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={}
    ).json()
    codes = {i["code"] for i in a["validation_report"]["issues"]}
    assert "credential_in_spec" not in codes
    assert a["status"] == "validated"


def test_full_pipeline_for_metric(client, admin_headers, seeded):
    """端到端：意图 → 草稿 → 校验 → 确认 → 执行。"""
    a = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={
            "kind": "metric",
            "intent": "统计成交额",
            "ontology_id": seeded["ontology_id"],
            "context": {"ontology_id": seeded["ontology_id"]},
        },
    ).json()
    aid = a["id"]
    assert a["spec"]["metric_name"] == "gmv"

    a = client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={}).json()
    assert a["status"] == "validated"
    a = client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={}).json()
    assert a["status"] == "confirmed"
    a = client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}).json()
    assert a["status"] == "succeeded"
    assert "SUM(amount)" in a["execution_receipt"]["sql"]


def test_high_risk_cluster_requires_dry_run_before_confirm(client, admin_headers):
    a = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={"kind": "cluster", "intent": "部署 hdfs", "context": {"hosts": ["n1"]}},
    ).json()
    assert a["is_high_risk"] is True
    a = client.post(
        f"/api/agents/artifacts/{a['id']}/validate", headers=admin_headers, json={}
    ).json()
    # 高危制品必须拿到 dry-run 差异才能 validated
    assert a["validation_report"]["dry_run"]["irreversible"] is True
    assert a["status"] == "validated"


def test_invalid_intent_returns_400_not_500(client, admin_headers, seeded):
    """Drafter 对无效意图抛 ValueError —— 属输入问题，必须是 4xx 而非 500。"""
    resp = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={
            "kind": "metric",
            "intent": "统计一个根本不存在的东西",
            "context": {"ontology_id": seeded["ontology_id"]},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "未在本体中找到匹配的业务逻辑" in resp.json()["detail"]


def test_missing_context_returns_400(client, admin_headers):
    resp = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={"kind": "cluster", "intent": "部署 hdfs", "context": {}},
    )
    assert resp.status_code == 400
    assert "hosts" in resp.json()["detail"]


# ---------- 任务链（多任务编排） ----------


def test_pipeline_endpoints_walk_a_chain_step_by_step(client, admin_headers, seeded):
    """端到端：建链 → 起草第 1 步 → 走完它的校验/确认/执行 → 才起草第 2 步。

    钉住「链不替谁确认」：第 2 步在第 1 步跑成功之前请求推进，必须 409 并说清卡在哪。
    """
    onto = seeded["ontology_id"]
    created = client.post(
        "/api/agents/pipelines",
        headers=admin_headers,
        json={
            "name": "指标链",
            "intent": "先算指标，再算一次",
            "ontology_id": onto,
            "steps": [
                {"kind": "metric", "intent": "统计成交额", "context": {"ontology_id": onto}},
                {"kind": "metric", "intent": "统计成交额（复算）", "context": {"ontology_id": onto}},
            ],
        },
    )
    assert created.status_code == 200, created.text
    pipeline = created.json()
    pid = pipeline["id"]
    # 建链不起草任何制品
    assert pipeline["status"] == "drafted"
    assert all(s["artifact_id"] is None for s in pipeline["steps"])

    advanced = client.post(f"/api/agents/pipelines/{pid}/advance", headers=admin_headers)
    assert advanced.status_code == 200, advanced.text
    first = advanced.json()["artifact"]
    assert first["kind"] == "metric" and first["status"] == "drafted"
    assert advanced.json()["pipeline"]["status"] == "running"

    # 第 1 步还没跑成功 → 拒绝推进，并指明卡在第 1 步
    blocked = client.post(f"/api/agents/pipelines/{pid}/advance", headers=admin_headers)
    assert blocked.status_code == 409
    assert "第 1 步" in blocked.json()["detail"]

    aid = first["id"]
    client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={})
    client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={})
    done = client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}).json()
    assert done["status"] == "succeeded"

    second = client.post(f"/api/agents/pipelines/{pid}/advance", headers=admin_headers)
    assert second.status_code == 200, second.text
    assert second.json()["pipeline"]["next_step_index"] is None  # 两步都起草了

    detail = client.get(f"/api/agents/pipelines/{pid}", headers=admin_headers).json()
    assert [s["artifact_status"] for s in detail["steps"]] == ["succeeded", "drafted"]


def test_pipeline_with_unregistered_kind_is_501(client, admin_headers, seeded):
    """未实现的任务类型在建链时就拦掉，而不是等推进到那一步才炸。"""
    resp = client.post(
        "/api/agents/pipelines",
        headers=admin_headers,
        json={
            "name": "坏链",
            "ontology_id": seeded["ontology_id"],
            "steps": [
                {"kind": "metric", "intent": "统计成交额"},
                {"kind": "nope", "intent": "不存在"},
            ],
        },
    )
    assert resp.status_code == 501
