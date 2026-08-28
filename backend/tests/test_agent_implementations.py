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
from app.agents.executors.metric import MetricExecutor, _build_table
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
        ids = {"ontology_id": onto.id, "customer_id": customer.id, "domain_code": f"m6_{tag}"}

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
    assert out["execute_mode"] == "handoff"
    assert out["compute_engine"] == "doris"


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


def test_doris_metric_execution_uses_ready_physical_projection(formal_logic, monkeypatch):
    from app.models import (
        DataSource, ObjectType, Ontology, OntologyWarehouseDeployment,
        WarehouseObjectProjection, WarehouseLogicProjection,
    )
    from app.services import doris_job_runner

    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        ontology = db.get(Ontology, formal_logic["ontology_id"])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id, ObjectType.name == "order"
        ).one()
        ds = DataSource(
            name="Doris metric", kind="doris", purpose="warehouse",
            is_default_warehouse=True, dsn_secret_ref="mysql://doris",
        )
        db.add(ds); db.flush()
        deployment = OntologyWarehouseDeployment(
            ontology_id=ontology.id, ontology_version=ontology.version,
            doris_datasource_id=ds.id, status="ready",
        )
        db.add(deployment); db.flush()
        db.add(WarehouseObjectProjection(
            deployment_id=deployment.id, object_type_id=obj.id,
            serving_layer="dwd", serving_database="dwd", serving_table="sales_order",
            schema_status="ready", sync_status="ready", transform_status="ready",
            queryable=True,
        ))
        db.commit()
        ds_id = ds.id

    captured: dict = {}
    monkeypatch.setattr(
        doris_job_runner,
        "run_doris_sql",
        lambda db, **kwargs: captured.update(kwargs) or {
            "ok": True, "execute_mode": "orchestrated", "compute_engine": "doris",
        },
    )
    spec = MetricDrafter().draft(
        "订单总额",
        {
            "ontology_id": formal_logic["ontology_id"],
            "business_logic_id": formal_logic["logic_id"],
            "target_datasource_id": ds_id,
        },
    )
    receipt = MetricExecutor().execute(spec, {"artifact_id": "metric-artifact"})
    assert captured["kind"] == "metric"
    assert captured["source_tables"] == ["dwd.sales_order"]
    assert "FROM dwd.sales_order" in captured["execute_sql"][0]
    assert "flink" not in captured["execute_sql"][0].lower()
    assert receipt["logic_projection_id"]
    with SessionLocal() as db:
        projection = db.get(WarehouseLogicProjection, receipt["logic_projection_id"])
        assert projection.status == "running"
        assert projection.queryable is False
        ds = db.get(DataSource, ds_id)
        ds.is_default_warehouse = False
        db.commit()


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
    assert "INSERT OVERWRITE TABLE `dim_erp`.`customer`" in out["sql"]
    # Transform 只读 Doris ODS，不再直连业务源表。库名前缀只作用于服务层（dim_erp），
    # ODS 侧恒为同步写入的那一个库——两边各按前缀拼一个库名，读的就不是搬进来的数据。
    assert f"FROM `ods`.`ods_{seeded['domain_code']}_tab_customer`" in out["sql"]
    # 单源不带表别名、不带 JOIN：多源改造给 _projection 加的列表达式映射，缺省必须
    # 等价于「原样引用同名列」，否则每一份存量清洗作业的 SQL 都会变样。
    assert " t0" not in out["sql"] and "JOIN" not in out["sql"]
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


def test_transform_column_rules_rewrite_string_projection(seeded):
    """列级算子必须真的进 SQL：曾经 trim/大小写只在词表里，选了也一个字符不改。"""
    spec = TransformDrafter().draft(
        "客户表去空格并统一转大写",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer"},
    )
    assert {r["rule"] for r in spec["cleansing_rules"]} == {"trim", "uppercase"}
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == ["trim", "uppercase"]
    # trim 先于大小写，且必须回写同名别名——否则外层（去重那层）找不到列。
    assert "UPPER(TRIM(`region`)) AS `region`" in out["sql"]
    assert out["unapplied_rules"] == []


def test_transform_column_rules_only_touch_string_columns(seeded):
    """数值/日期列不该被 TRIM：判据是目标列真实落成的 Doris 类型。"""
    spec = TransformDrafter().draft(
        "客户表去空格", {"ontology_id": seeded["ontology_id"], "target_table": "customer"}
    )
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == ["trim"]
    assert "TRIM(`region`) AS `region`" in out["sql"]
    # amount 落 DECIMAL、created_at 落 DATETIME：两者都不该被套上字符串算子。
    assert "TRIM(`amount`)" not in out["sql"]
    assert "TRIM(`created_at`)" not in out["sql"]


def test_transform_case_rules_are_mutually_exclusive(seeded):
    """大写 + 小写同时选 = 后一条静默覆盖前一条；两条都不应用并说清冲突。"""
    spec = TransformDrafter().draft(
        "客户表", {"ontology_id": seeded["ontology_id"], "target_table": "customer",
                  "cleansing_rules": ["uppercase", "lowercase"]},
    )
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == []
    assert set(out["unapplied_rules"]) == {"uppercase", "lowercase"}
    assert any("互斥" in n["detail"] for n in out["rule_notes"])
    assert "UPPER(" not in out["sql"] and "LOWER(" not in out["sql"]


def test_transform_reports_unapplied_rules(seeded):
    """闭集之外的规则显式列出，不静默丢弃（如已下线的 normalize_code 存量 Spec）。"""
    spec = TransformDrafter().draft(
        "客户表",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer",
         "cleansing_rules": [{"rule": "normalize_code", "description": "编码标准化"}]},
    )
    out = TransformExecutor().execute(spec, {})
    assert out["unapplied_rules"] == ["normalize_code"]
    assert out["applied_rules"] == []


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
         # 落点不是配置项：库名前缀/自定义 ODS 库传进来也不生效。
         "database_prefix": "erp", "target_ods_database": "dwd_erp"},
    )
    # 源由 source_ref 定位，目标结构由本体决定
    assert spec["source"] == "erp_ods.tab_customer"
    assert spec["target"] == f"ods.ods_{seeded['domain_code']}_tab_customer"
    assert spec["target_ods_database"] == "ods"
    assert "database_prefix" not in spec
    assert spec["mode"] == "incremental"  # 有 datetime 字段
    assert spec["partition_key"] == "created_at"
    assert spec["preservation"]["preserve"] is False


def test_sync_task_name_uses_the_business_object_not_physical_coordinates(seeded):
    """任务名写业务名。

    此前是 ``同步 · {源库.源表} → {ods 库.ods 表}``，真实 ERP 上长成
    ``同步 · _d71df877e93eac81.tabCustomer Group → ods.ods_erpnext_tab_customer_group``
    ——源库名是哈希、源表名是 doctype 原样，任务列表里一屏全是这种串，看不出在同步什么。
    """
    drafter = SyncDrafter()
    spec = drafter.draft(
        "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
    )
    assert spec["object_display_name"] == "客户"
    name = drafter.name_from_spec(spec)
    assert name == "同步 · 客户 → 数仓 ODS"
    assert "tab_customer" not in name
    assert drafter.suggested_name("同步客户", spec) == name


def test_sync_task_name_falls_back_for_legacy_specs():
    """老 Spec 没有对象名（只有物理坐标）时不硬编名字，退回原口径。"""
    legacy = {"source": "erp_ods.tab_customer", "target": "ods.ods_erp_tab_customer"}
    assert SyncDrafter().name_from_spec(legacy) == (
        "同步 · erp_ods.tab_customer → ods.ods_erp_tab_customer"
    )


def test_sync_preservation_surfaces_in_plan(seeded):
    """关键源保全判定仍在，执行器如实带出（Flink 路径的 STG 保全为后续工作）。

    旧行为渲染一份 SeaTunnel preserve_ 作业；统一执行后 execute 无 target_datasource
    退回「仅产出」，保全决定经 _plan 带出 preserved=True，不静默丢弃。
    """
    spec = SyncDrafter().draft(
        "同步订单审计日志",
        {"ontology_id": seeded["ontology_id"], "object_type": "order_audit_log"},
    )
    assert spec["preservation"]["preserve"] is True
    out = SyncExecutor().execute(spec, {})  # 无 target_datasource → 仅产出
    assert out["preserved"] is True
    assert out["preservation_reason"]
    assert out["handoff"] == "flink_sql"


def test_sync_mode_carried_into_plan(seeded):
    """装载方式贯穿到搬运计划（增量 vs 全量的 sink 语义已下沉到 move_job_compiler，
    其 detached/CDC 行为在 test_move_job_compiler 覆盖；这里只钉 sync 把 mode 正确带出）。"""
    spec = SyncDrafter().draft(
        "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
    )
    assert spec["mode"] == "incremental"
    out = SyncExecutor().dry_run(spec, {})
    assert out["action"] == "flink_sql_move"
    assert out["mode"] == "incremental"
    full = dict(spec, mode="full")
    assert SyncExecutor().dry_run(full, {})["mode"] == "full"


def test_sync_engine_is_doris_only(seeded):
    """Phase 6 prevents a new sync spec from carrying a non-Doris target engine."""
    with pytest.raises(ValueError, match="只允许使用 Doris"):
        SyncDrafter().draft(
            "同步客户",
            {"ontology_id": seeded["ontology_id"], "object_type": "customer",
             "engine": "postgres"},
        )
    spec = SyncDrafter().draft(
        "同步客户",
        {"ontology_id": seeded["ontology_id"], "object_type": "customer", "engine": "doris"},
    )
    assert SyncExecutor().dry_run(spec, {})["engine"] == "doris"


def test_sync_plan_carries_alias_not_credentials(seeded):
    """搬运计划里只放源库连接别名，不得出现连接串或口令（真正的 Flink SQL 占位符
    凭据安全在 test_flink_sql_generator 覆盖；这里钉 sync 计划本身不泄漏）。"""
    spec = SyncDrafter().draft(
        "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
    )
    out = SyncExecutor().execute(spec, {})  # 无 target_datasource → 仅产出计划
    assert out["source_ref_alias"]  # 别名在
    blob = json.dumps(out, ensure_ascii=False).lower()
    for leak in ("password", "jdbc:", "secret"):
        assert leak not in blob, f"搬运计划中泄漏了 {leak}"


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
        "app.services.materialization_runner.run_materialize"
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
        "app.services.materialization_runner.run_materialize", return_value={"ok": True}
    ) as mock_run:
        executor = MaterializeExecutor()
        receipt = executor.execute(spec, {"artifact_id": "art-1"})
        assert receipt == {"ok": True}
        mock_run.assert_called_once()


def test_non_doris_target_cannot_create_executable_spec(seeded):
    """Phase 6 removes runtime Hive/Postgres/StarRocks target compatibility."""
    from app.models import DataSource
    from app.services.materialization_runner import MaterializationError

    with SessionLocal() as db:
        db.add(DataSource(id="ds-pg-eng", name="pg-eng", kind="postgres",
                          purpose="business_source", dsn_secret_ref="postgresql://x/y"))
        db.commit()
    try:
        ctx = {"ontology_id": seeded["ontology_id"], "object_type": "customer",
               "target_datasource_id": "ds-pg-eng"}
        with pytest.raises(MaterializationError, match="必须是 Doris"):
            SyncDrafter().draft("同步客户", ctx)
        with pytest.raises(ValueError, match="必须是 Doris"):
            TransformDrafter().draft("清洗客户", {**ctx, "target_table": "customer"})
        # Explicit engine cannot override an incompatible target datasource.
        with pytest.raises(MaterializationError, match="必须是 Doris"):
            SyncDrafter().draft("同步客户", {**ctx, "engine": "doris"})
        assert SyncDrafter().draft(
            "同步客户", {"ontology_id": seeded["ontology_id"], "object_type": "customer"}
        )["engine"] == "doris"
    finally:
        with SessionLocal() as db:
            db.query(DataSource).filter(DataSource.id == "ds-pg-eng").delete()
            db.commit()


def test_transform_executor_has_no_flink_dependency():
    """Architecture guard: transform cannot regress to the Flink execution path."""
    import inspect
    import app.agents.executors.transform as module

    source = inspect.getsource(module)
    assert "flink_job_runner" not in source
    assert "generate_flink_sql" not in source
    assert "FlinkSqlTask" not in source


def test_transform_spec_contains_no_flink_fields(seeded):
    spec = TransformDrafter().draft(
        "客户表去重",
        {"ontology_id": seeded["ontology_id"], "target_table": "customer"},
    )
    assert spec["engine"] == "doris"
    assert not any(key.startswith("flink_") for key in spec)
    assert "execution_mode" not in spec
    assert "source_ref_alias" not in spec


# ---------- ④ 标签 / 规则走同一条指标任务链 ----------


def _seed_logic(logic_type: str, ast_body: dict, *, summary: str) -> dict:
    """建一条已发布、已形式化的 tag/rule 口径（订单.金额 + 订单.状态）。"""
    from app.models import EntityStatus

    tag = uuid.uuid4().hex[:8]
    pub = EntityStatus.PUBLISHED.value
    with SessionLocal() as db:
        domain = DomainContext(datahub_domain_id=f"urn:li:domain:tg-{tag}", name=f"tg-{tag}")
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
        db.add(amount)
        db.flush()
        body = json.loads(
            json.dumps(ast_body).replace("__REF__", "r1")
        )
        logic = BusinessLogic(
            ontology_id=onto.id, name=f"{logic_type}_big_order", display_name="大额订单",
            logic_type=logic_type, status=pub,
            expression_summary=summary,
            expression_json=json.dumps({
                "type": logic_type,
                "refs": [{
                    "ref_id": "r1", "object_type_id": order.id, "object_name": "order",
                    "object_display_name": "订单", "property_id": amount.id,
                    "property_name": "amount", "property_display_name": "金额",
                }],
                "body": body,
            }, ensure_ascii=False),
        )
        db.add(logic)
        db.commit()
        return {"ontology_id": onto.id, "logic_id": logic.id, "name": logic.name}


@pytest.fixture
def tag_logic():
    """标签：金额 > 1000 记「大额」，否则「普通」。"""
    return _seed_logic(
        "tag",
        {
            "cases": [
                {"when": {"left": {"ref": "__REF__"}, "op": ">", "right": {"value": 1000}},
                 "then": {"value": "大额"}},
                {"when": None, "then": {"value": "普通"}},
            ]
        },
        summary="金额>1000 记为大额",
    )


@pytest.fixture
def rule_logic():
    """规则：金额应当 > 0（违规=不满足的行）。"""
    return _seed_logic(
        "rule",
        {"condition": {"left": {"ref": "__REF__"}, "op": ">", "right": {"value": 0}},
         "message": "订单金额必须为正"},
        summary="金额必须为正",
    )


def _draft(fixture: dict) -> dict:
    return MetricDrafter().draft(
        "大额订单",
        {"ontology_id": fixture["ontology_id"], "business_logic_id": fixture["logic_id"]},
    )


def test_drafter_carries_logic_type_from_ast(tag_logic, rule_logic):
    """Spec 必须带口径类型：结果表形状与取数列都按它分叉，缺了就一律当指标处理。"""
    assert _draft(tag_logic)["logic_type"] == "tag"
    assert _draft(rule_logic)["logic_type"] == "rule"


def test_tag_task_keeps_label_and_count_in_separate_columns(tag_logic):
    """标签任务：标签取值单独一列，metric_value 落的是**该取值下的实体数**。

    此前一律 `logic.name AS metric_value`——对 tag 而言那是 CASE 分桶列（字符串标签），
    于是标签被塞进 decimal 的 metric_value，真正要的 row_count 整列丢掉：跑得通、数是错的。
    """
    out = MetricExecutor().execute(_draft(tag_logic), {})
    ddl, sql = out["ddl"], out["sql"]
    # 结果表：标签取值是可分组的字符串列，值列是计数（不是金额类型）
    assert ("`tag_value` VARCHAR" in ddl or "`tag_value` STRING" in ddl)
    assert "`metric_value` INT" in ddl
    assert "DECIMAL" not in ddl.split("metric_value")[1].split("\n")[0]
    # SQL：标签列 → tag_value；row_count → metric_value
    assert "AS `tag_value`" in sql
    assert "`row_count` AS metric_value" in sql
    # 标签值不该再被当成度量值
    assert "`tag_big_order` AS metric_value" not in sql


def test_rule_task_selects_violations_column(rule_logic):
    """规则任务：metric_value 取 violations。

    编译器给规则的计数列叫 violations，**没有**以口径名命名的列；此前外层照样
    `SELECT \\`rule_big_order\\`` —— 引用了子查询里不存在的列，一执行就 column not found。
    """
    out = MetricExecutor().execute(_draft(rule_logic), {})
    sql = out["sql"]
    assert "`violations` AS metric_value" in sql
    assert "`rule_big_order` AS metric_value" not in sql
    assert "`metric_value` INT" in out["ddl"]
    # 规则没有标签列
    assert "tag_value" not in out["ddl"]


def test_metric_task_shape_unchanged(formal_logic):
    """回归护栏：指标那一类的形状一个字节都没变（度量值仍是 decimal、无标签列）。"""
    out = MetricExecutor().execute(
        MetricDrafter().draft(
            "订单总额",
            {"ontology_id": formal_logic["ontology_id"],
             "business_logic_id": formal_logic["logic_id"]},
        ),
        {},
    )
    assert "`metric_value` DECIMAL(18,4)" in out["ddl"]
    assert "tag_value" not in out["ddl"]
    assert "`order_total` AS metric_value" in out["sql"]


def test_unformalized_tag_refuses_instead_of_faking_a_metric():
    """没形式化的标签不能建任务：兜底路只会拼出一条与该标签无关的聚合。"""
    import pytest as _pytest

    from app.agents.executors.metric import _build_sql

    spec = {
        "metric_name": "vip_customer", "display_name": "高价值客户",
        "logic_type": "tag", "target_layer": "ads",
        "subject_objects": ["customer"], "group_by": [], "expression": "COUNT(1)",
    }
    with _pytest.raises(ValueError) as err:
        _build_sql(spec, "hive")
    assert "形式化" in str(err.value) and "标签" in str(err.value)


def test_tag_walks_the_same_governance_pipeline(client, admin_headers, tag_logic):
    """端到端：标签口径穿完整治理流水线（起草 → 校验 → 确认 → 执行）。

    这条链此前只有指标走过。标签走它有两处会踩空：Spec 要带 logic_type（否则结果表
    按指标形状建），校验闸门要认标签的主对象（它来自 AST 而非绑定表）。
    """
    a = client.post(
        "/api/agents/draft",
        headers=admin_headers,
        json={
            "kind": "metric",
            "intent": "大额订单",
            "ontology_id": tag_logic["ontology_id"],
            "context": {
                "ontology_id": tag_logic["ontology_id"],
                "business_logic_id": tag_logic["logic_id"],
            },
        },
    ).json()
    aid = a["id"]
    assert a["spec"]["logic_type"] == "tag"
    assert a["name"].startswith("标签 · "), a["name"]  # 任务列表里认得出这是标签

    a = client.post(f"/api/agents/artifacts/{aid}/validate", headers=admin_headers, json={}).json()
    assert a["status"] == "validated", a
    a = client.post(f"/api/agents/artifacts/{aid}/confirm", headers=admin_headers, json={}).json()
    assert a["status"] == "confirmed"
    a = client.post(f"/api/agents/artifacts/{aid}/execute", headers=admin_headers, json={}).json()
    assert a["status"] == "succeeded", a
    receipt = a["execution_receipt"]
    assert "`row_count` AS metric_value" in receipt["sql"]
    assert ("`tag_value` VARCHAR" in receipt["ddl"] or "`tag_value` STRING" in receipt["ddl"])


def test_materialize_and_metric_task_agree_on_tag_table_shape(tag_logic):
    """同一条标签口径，**物化**建的表与**指标任务**写的表必须同形。

    两处曾各自写死指标形状：物化建 stat_date+metric_value(DECIMAL)，指标任务按 tag 形状
    INSERT tag_value+row_count——列对不上，直到执行才炸。现在共读
    metric_compiler.result_column_specs 这一份权威。
    """
    from app.models import MaterializationContract
    from app.models.warehouse import TargetKind
    from app.services.warehouse_generator import WarehouseGenerator

    with SessionLocal() as db:
        db.add(
            MaterializationContract(
                ontology_id=tag_logic["ontology_id"],
                target_kind=TargetKind.BUSINESS_LOGIC.value,
                target_id=tag_logic["logic_id"],
                target_layer="ads",
                materialized=True,
            )
        )
        db.commit()
        plan = WarehouseGenerator().build_logical_schema(db, tag_logic["ontology_id"])

    mat = next(t for t in plan.schema.tables if t.source_name == tag_logic["name"])
    task = _build_table(_draft(tag_logic))
    assert [c.name for c in mat.columns] == [c.name for c in task.columns]
    assert [c.data_type for c in mat.columns] == [c.data_type for c in task.columns]
    assert "tag_value" in [c.name for c in mat.columns]
