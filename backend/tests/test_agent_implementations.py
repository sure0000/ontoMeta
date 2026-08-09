"""M6 三类 Drafter/Executor：④指标 → ③ETL → ①同步（风险由低到高）。

贯穿全部三类的两条约束：
- **凭据不进 Spec**：只允许主机/数据源别名。
- **ontoMeta 只生成、不执行**：执行器产出可部署产物并交接，不直接改集群。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.agents.drafters.metric import MetricDrafter
from app.agents.drafters.sync import SyncDrafter, decide_preservation
from app.agents.drafters.transform import TransformDrafter
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


@pytest.fixture
def formal_logic() -> dict:
    """一条**已形式化但没有任何绑定**的口径——真实库里就是这个形状。

    ``expression_summary`` 是给人看的中文摘要，``expression_json`` 才是权威 AST。
    """
    from app.models import EntityStatus

    tag = uuid.uuid4().hex[:8]
    pub = EntityStatus.PUBLISHED.value
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:fx-{tag}", name=f"fx-{tag}")
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()
        order = ObjectType(
            ontology_id=onto.id, name="order", display_name="订单",
            table_role="business_object", status=pub,
            source_ref=_URN.format(t="tab_order"),
        )
        db.add(order)
        db.flush()
        amount = Property(object_type_id=order.id, name="amount", display_name="金额",
                          data_type="decimal", semantic_type="measure", status=pub)
        status_prop = Property(object_type_id=order.id, name="status", display_name="状态",
                               data_type="varchar", semantic_type="categorical", status=pub)
        db.add_all([amount, status_prop])
        db.flush()
        logic = BusinessLogic(
            ontology_id=onto.id, name="order_total", display_name="订单总额",
            logic_type="metric", status=pub,
            expression_summary="SUM(订单.金额)",
            expression_json=json.dumps({
                "type": "metric",
                "refs": [{
                    "ref_id": "r1", "object_type_id": order.id, "object_name": "order",
                    "object_display_name": "订单", "property_id": amount.id,
                    "property_name": "amount", "property_display_name": "金额",
                }],
                "body": {"operation": "sum", "args": [{"ref": "r1"}],
                         "filter": None, "group_by": [], "window": None},
            }, ensure_ascii=False),
        )
        db.add(logic)
        db.commit()
        return {"ontology_id": onto.id, "logic_id": logic.id}


def test_metric_drafter_falls_back_to_formal_expression(formal_logic):
    """口径已形式化就不该被判「未绑定主对象」——绑定表为空时从 AST 反推。

    真实库里的 order_total 正是这样：AST 里写明了 SUM(order.amount)，绑定表却是空的，
    于是四条指标任务全部卡在校验，一次都没执行过。
    """
    spec = MetricDrafter().draft(
        "订单总额",
        {"ontology_id": formal_logic["ontology_id"],
         "business_logic_id": formal_logic["logic_id"]},
    )
    assert spec["subject_objects"] == ["order"]
    assert spec["inputs"] == ["amount"]
    assert spec["object_types"] == ["order"]


def test_metric_executor_compiles_formal_caliber(formal_logic):
    """聚合 SQL 由 metric_compiler 从 AST 编译，不再把中文口径摘要拼进 SQL。"""
    spec = MetricDrafter().draft(
        "订单总额",
        {"ontology_id": formal_logic["ontology_id"],
         "business_logic_id": formal_logic["logic_id"]},
    )
    out = MetricExecutor().execute(spec, {})
    sql = out["sql"]
    assert "SUM(订单.金额)" not in sql, "中文显示名不该出现在 SQL 里"
    assert "SUM(" in sql and "amount" in sql
    # 保留字 order 必须被引起来，否则任何引擎都解析不了
    assert "`order`" in sql
    assert "AS metric_value" in sql
    # 写库场景不能带 LIMIT：那会把结果悄悄截断
    assert "LIMIT" not in sql.upper()


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
    # 源表名逐段引用（真实源里带空格的表名不引用就是废 SQL）
    assert "FROM `erp_ods`.`tab_customer`" in out["sql"]
    assert out["applied_rules"] == ["deduplicate"]
    # 该本体没声明主键 → 退回整行去重，且**明说**是整行，不冒充按主键
    assert "SELECT DISTINCT" in out["sql"]
    assert any(
        n["rule"] == "deduplicate" and "整行去重" in n["detail"]
        for n in out["rule_notes"]
    )


def test_transform_deduplicates_by_primary_key(seeded):
    """有主键就按主键去重——整行 DISTINCT 对带审计列的源表一行都去不掉。"""
    with SessionLocal() as db:
        obj = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == seeded["ontology_id"],
                    ObjectType.name == "customer")
            .one()
        )
        db.add(Property(object_type_id=obj.id, name="customer_id", display_name="客户ID",
                        data_type="bigint", semantic_type="identifier", required=True))
        db.commit()
    spec = TransformDrafter().draft(
        "客户表去重", {"ontology_id": seeded["ontology_id"], "target_table": "customer"}
    )
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == ["deduplicate"]
    assert "ROW_NUMBER() OVER (PARTITION BY `customer_id`" in out["sql"]
    assert "`__rn` = 1" in out["sql"]
    assert "SELECT DISTINCT" not in out["sql"]


def test_transform_drop_null_emits_predicate(seeded):
    """drop_null 曾只挂在可应用集合里而没有任何实现：回执说已应用、SQL 里没有 WHERE。"""
    with SessionLocal() as db:
        obj = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == seeded["ontology_id"],
                    ObjectType.name == "customer")
            .one()
        )
        db.add(Property(object_type_id=obj.id, name="customer_id", display_name="客户ID",
                        data_type="bigint", semantic_type="identifier", required=True))
        db.commit()
    spec = TransformDrafter().draft(
        "客户表过滤空值", {"ontology_id": seeded["ontology_id"], "target_table": "customer"}
    )
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == ["drop_null"]
    assert "WHERE `customer_id` IS NOT NULL" in out["sql"]


def test_transform_drop_null_unapplied_without_keys(seeded):
    """说不出滤哪几列就不假装应用——归 unapplied 并给出原因。"""
    spec = TransformDrafter().draft(
        "客户表过滤空值", {"ontology_id": seeded["ontology_id"], "target_table": "customer"}
    )
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == []
    assert out["unapplied_rules"] == ["drop_null"]
    assert "IS NOT NULL" not in out["sql"]


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


def test_sync_incremental_appends_not_overwrites(seeded):
    """增量读 + 覆盖写 = 拿一个时间切片把整表换掉，历史数据当场没了。"""
    spec = SyncDrafter().draft(
        "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
    )
    assert spec["mode"] == "incremental"
    job = json.loads(SyncExecutor().execute(spec, {})["jobs"]["sync_customer"])
    assert job["sink"][0]["save_mode"] == "append"
    # 全量才允许覆盖
    full = dict(spec, mode="full")
    full_job = json.loads(SyncExecutor().execute(full, {})["jobs"]["sync_customer"])
    assert full_job["sink"][0]["save_mode"] == "overwrite"


def test_sync_sink_plugin_follows_target_engine(seeded):
    """目标是 postgres 却渲染 Hive sink，照着这份配置跑不起来。"""
    spec = SyncDrafter().draft(
        "同步客户",
        {"ontology_id": seeded["ontology_id"], "object_type": "customer",
         "engine": "postgres"},
    )
    job = json.loads(SyncExecutor().execute(spec, {})["jobs"]["sync_customer"])
    assert job["sink"][0]["plugin"] == "Jdbc"
    doris = dict(spec, engine="doris")
    doris_job = json.loads(SyncExecutor().execute(doris, {})["jobs"]["sync_customer"])
    assert doris_job["sink"][0]["plugin"] == "Doris"


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


# ---------- 与流水线的衔接 ----------


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


# ---------- P2：物化 executor 强制 preflight 闸门 ----------


def test_materialize_executor_blocks_on_preflight_failure(monkeypatch, seeded):
    """MaterializeExecutor.execute 提交前跑 preflight，有阻断项就抛异常拒绝执行。

    保护 Data Agent 提交的物化制品：手动弹窗有前端闸门，但 agents/draft+execute
    绕过弹窗，必须在 executor 侧兜底，否则产出连不上 runner/Airflow 的 DAG。
    """
    from unittest.mock import patch

    from app.agents.executors.materialize import MaterializeExecutor
    from app.services.materialize_preflight import PreflightItem, PreflightReport

    # 构造一份含阻断项的 preflight 报告
    bad_report = PreflightReport()
    bad_report.add(
        PreflightItem(
            key="sync_runner",
            label="sync-runner",
            status="fail",
            blocking=True,
            detail="通道为 runner，但未配置 sync-runner 地址。",
            next_step="设 SYNC_RUNNER_ENDPOINT 指向常驻 runner。",
        )
    )

    spec = {
        "ontology_id": seeded["ontology_id"],
        "target_datasource_id": "ds-x",
    }

    with patch(
        "app.services.materialization_runner.resolve_engine", return_value="hive"
    ), patch(
        "app.services.materialize_preflight.run_preflight", return_value=bad_report
    ), patch(
        "app.services.materialization_runner.run"
    ) as mock_run:
        executor = MaterializeExecutor()
        with pytest.raises(RuntimeError, match="提交前自检发现"):
            executor.execute(spec, {})
        # 有阻断项时绝不能真去提交 DAG
        mock_run.assert_not_called()


def test_materialize_executor_proceeds_when_preflight_ok(monkeypatch, seeded):
    """preflight 无阻断项时，executor 照常调 runner.run 提交。"""
    from unittest.mock import patch

    from app.agents.executors.materialize import MaterializeExecutor
    from app.services.materialize_preflight import PreflightReport

    ok_report = PreflightReport()  # 无 item → 无阻断失败 → ok

    spec = {
        "ontology_id": seeded["ontology_id"],
        "target_datasource_id": "ds-x",
    }

    with patch(
        "app.services.materialization_runner.resolve_engine", return_value="hive"
    ), patch(
        "app.services.materialize_preflight.run_preflight", return_value=ok_report
    ), patch(
        "app.services.materialization_runner.run", return_value={"ok": True}
    ) as mock_run:
        executor = MaterializeExecutor()
        receipt = executor.execute(spec, {"artifact_id": "art-1"})
        assert receipt == {"ok": True}
        mock_run.assert_called_once()


def test_engine_follows_target_datasource_not_contract(seeded):
    """选了 postgres 目标仓，产出的就该是 postgres 方言——此前一律取契约默认 hive，
    于是同步/清洗任务对着 postgres 连接产 Hive DDL 与 Hive sink，建表那步必挂。"""
    from app.models import DataSource

    with SessionLocal() as db:
        db.add(DataSource(id="ds-pg-eng", name="pg-eng", kind="postgres",
                          dsn_secret_ref="postgresql://x/y"))
        db.commit()
    try:
        ctx = {"ontology_id": seeded["ontology_id"], "object_type": "customer",
               "target_datasource_id": "ds-pg-eng"}
        assert SyncDrafter().draft("同步客户", ctx)["engine"] == "postgres"
        assert TransformDrafter().draft(
            "清洗客户", {**ctx, "target_table": "customer"}
        )["engine"] == "postgres"
        # 人显式选的仍然优先
        assert SyncDrafter().draft("同步客户", {**ctx, "engine": "doris"})["engine"] == "doris"
        # 没选目标数据源时行为不变（回退契约/缺省）
        assert SyncDrafter().draft(
            "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
        )["engine"] == "hive"
    finally:
        with SessionLocal() as db:
            db.query(DataSource).filter(DataSource.id == "ds-pg-eng").delete()
            db.commit()


def test_transform_flink_declares_source_and_target_separately(seeded, monkeypatch):
    """跨库由 Flink 承担：源表按**源库**声明、目标表按数仓声明，两个端点不能同一个别名。

    此前两端都写死 warehouse_conn_id，背后是「sync 已把源搬进数仓 ODS 层」的假设，
    而生成的 SQL 明明 FROM 原始源表——数仓根本看不见它，配上 Flink 也搬不动。
    """
    from app.models import DataSource

    with SessionLocal() as db:
        db.add(DataSource(id="ds-flink", name="pg-flink", kind="postgres",
                          dsn_secret_ref="postgresql://x/y"))
        db.commit()
    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return "-- flink sql --"

    import app.agents.executors.transform as mod

    monkeypatch.setattr(mod, "generate_flink_sql", _spy)
    # 执行器在函数体内 import run_flink_sql，故要打在它的**定义处**
    import app.services.flink_job_runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "run_flink_sql", lambda *a, **k: {"execute_mode": "handoff"}
    )
    try:
        spec = TransformDrafter().draft(
            "客户表去重",
            {"ontology_id": seeded["ontology_id"], "target_table": "customer",
             "target_datasource_id": "ds-flink"},
        )
        assert spec["source_ref_alias"] == "erp_readonly"
        TransformExecutor().execute(spec, {})
    finally:
        with SessionLocal() as db:
            db.query(DataSource).filter(DataSource.id == "ds-flink").delete()
            db.commit()

    assert captured, "generate_flink_sql 未被调用"
    source, target = captured["source"], captured["target"]
    assert source.alias == "erp_readonly"          # 源库别名
    assert target.alias == "ontometa_ds_pg_flink"  # 数仓别名
    assert source.alias != target.alias
    # 两侧平台各归各的：源是源库的平台，目标是数仓引擎
    assert source.platform == "mysql"
    assert target.platform == "postgres"
