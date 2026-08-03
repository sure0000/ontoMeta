"""JobPlanner：本体 + 物化契约 → 搬运作业计划。

最关键的两组断言：

- **与 M3 同源**：列映射、装载方式、目标库表必须与 ``warehouse_generator`` 生成的
  DDL/ETL 完全一致——两套逻辑一旦分叉，「建的表」和「搬的数据」就对不上了。
- **不可搬运项显式列出**：缺 source_ref、CDC 无连接器等一律进 ``unsupported``，绝不静默跳过。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.services.job_planner import job_planner
from app.services.materialization_contract import MaterializationContractService
from app.services.warehouse_generator import WarehouseGenerator

_URN = "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.{table},PROD)"
_contracts = MaterializationContractService()
_generator = WarehouseGenerator()


@pytest.fixture(autouse=True)
def _init_db(client):
    """拉起 session 级 client 以建表（本测试直接用 SessionLocal，不走 API）。"""
    return client


def _seed(tag: str, *, with_source: bool = True, platform: str = "mariadb") -> str:
    """客户（有列映射）+ 订单（无列映射，走同名回退）。"""
    urn = _URN.replace("mariadb", platform)
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:jp-{tag}", name=f"jp-{tag}"
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

        customer = ObjectType(
            ontology_id=onto.id,
            name="customer",
            display_name="客户",
            table_role="business_object",
            source_ref=urn.format(table="tab_customer") if with_source else None,
        )
        order = ObjectType(
            ontology_id=onto.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
            source_ref=urn.format(table="tab_order"),
        )
        db.add_all([customer, order])
        db.flush()
        db.add_all(
            [
                # 有 source_field_ref：物理列名与本体属性名不同
                Property(
                    object_type_id=customer.id,
                    name="customer_id",
                    display_name="客户ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                    # 真实回采写入的是 schemaField 标识（``<datasetUrn>#字段名``），
                    # 不是裸列名——这里刻意用真实形态，别用简化值把 bug 测没了。
                    source_field_ref=urn.format(table="tab_customer") + "#cust_id",
                ),
                Property(
                    object_type_id=customer.id,
                    name="created_at",
                    display_name="创建时间",
                    data_type="timestamp",
                    semantic_type="datetime",
                ),
                # 无 source_field_ref：必须回退同名
                Property(
                    object_type_id=order.id,
                    name="sales_order_id",
                    display_name="订单ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
            ]
        )
        db.commit()
        ontology_id = onto.id

    with SessionLocal() as db:
        _contracts.sync(db, ontology_id)
    return ontology_id


def _build(ontology_id: str, **kwargs):
    with SessionLocal() as db:
        return job_planner.build(db, ontology_id, engine="hive", **kwargs)


def test_builds_one_job_per_materialized_table():
    plan = _build(_seed("basic"))
    names = {j.name for j in plan.jobs}
    assert "sync_dim_customer" in names
    assert "sync_dim_sales_order" in names


def test_column_mapping_matches_m3_etl():
    """同源约束：作业的列映射必须与 M3 生成的 SELECT 完全一致。"""
    ontology_id = _seed("samesource")
    plan = _build(ontology_id)
    job = next(j for j in plan.jobs if j.target.table == "customer")

    with SessionLocal() as db:
        etl = _generator.generate_etl_sql(db, ontology_id, "hive")
    sql = etl["statements"]["dim.customer"]

    for col in job.columns:
        # M3 渲染成 `src` AS `prop`，作业渲染成 ColumnMapping(source, target)
        assert f"`{col.source}` AS `{col.target}`" in sql
    # 有 source_field_ref 的用物理名，没有的回退同名
    mapping = {c.target: c.source for c in job.columns}
    assert mapping["customer_id"] == "cust_id"
    assert mapping["created_at"] == "created_at"


def test_target_table_matches_ddl_target():
    """作业写入的库表必须与 DDL 建的库表一致（否则搬到一张不存在的表里）。"""
    ontology_id = _seed("target")
    plan = _build(ontology_id, database_overrides={"dim": "warehouse_prod"})
    job = next(j for j in plan.jobs if j.target.table == "customer")
    assert job.target.qualified == "warehouse_prod.customer"

    with SessionLocal() as db:
        ddl = _generator.generate_ddl(
            db, ontology_id, "hive", database_overrides={"dim": "warehouse_prod"}
        )
    assert "warehouse_prod.customer" in ddl["statements"]


def test_source_endpoint_parsed_from_urn():
    plan = _build(_seed("urn"))
    job = next(j for j in plan.jobs if j.target.table == "customer")
    assert job.source.platform == "mariadb"  # 决定连接器
    assert job.source.database == "erp_ods"
    assert job.source.table == "tab_customer"
    assert job.source_urn.startswith("urn:li:dataset:")  # 原样留给血缘上报


def test_missing_source_ref_goes_to_unsupported():
    plan = _build(_seed("nosource", with_source=False))
    assert not any(j.target.table == "customer" for j in plan.jobs)
    reasons = {u["target"]: u["reason"] for u in plan.unsupported}
    assert any("source_ref" in r for r in reasons.values())


def test_schema_notes_are_not_mixed_into_unsupported():
    """M3 的表结构提示（如缺主键）不妨碍搬运，混进 unsupported 会让人误判几百张表搬不了。

    真实 ERP 本体上这个区分很要命：734 张表全部产出了作业，却有 643 条「未声明身份属性」
    的提示——混在一起会读成「643 张表搬不了」。
    """
    ontology_id = _seed("notes")
    # 去掉身份语义**并改名**（``_primary_key_of`` 还会按 ``<表名>_id`` 的命名约定兜底认主键），
    # 两者都断掉，M3 编译目标表时才会记「未声明可识别的身份属性，主键未生成」。
    with SessionLocal() as db:
        for prop in db.query(Property).all():
            if prop.semantic_type == "identifier":
                prop.semantic_type = "attribute"
                prop.name = f"col_{prop.name}"
        db.commit()

    plan = _build(ontology_id)
    assert plan.jobs  # 照常产出搬运作业
    assert plan.unsupported == []  # 没有任何「搬不了」
    assert any("主键" in n["reason"] for n in plan.schema_notes)  # 提示仍带出，不吞


def test_contract_load_strategy_drives_job_mode():
    """同步方式逐实体来自各自契约（与 M10 的 per_contract_strategy 同一事实源）。"""
    ontology_id = _seed("modes")
    with SessionLocal() as db:
        cs = _contracts.list_contracts(db, ontology_id)
        names = _contracts.resolve_target_names(db, cs)
        by_name = {names.get(c.target_id, (None,))[0]: c for c in cs}
        _contracts.update(
            db,
            by_name["customer"].id,
            {"load_strategy": "incremental", "partition_key": "created_at"},
        )
        _contracts.update(db, by_name["sales_order"].id, {"load_strategy": "full"})

    plan = _build(ontology_id)
    by_table = {j.target.table: j for j in plan.jobs}
    assert by_table["customer"].mode == "incremental"
    assert by_table["customer"].partition_key == "created_at"
    assert by_table["sales_order"].mode == "full"


def _set_cdc(ontology_id: str, entity: str) -> None:
    with SessionLocal() as db:
        cs = _contracts.list_contracts(db, ontology_id)
        names = _contracts.resolve_target_names(db, cs)
        target = next(c for c in cs if names.get(c.target_id, (None,))[0] == entity)
        _contracts.update(db, target.id, {"load_strategy": "cdc"})


def test_cdc_with_connector_produces_cdc_job():
    ontology_id = _seed("cdcok")
    _set_cdc(ontology_id, "customer")
    plan = _build(ontology_id)
    assert next(j for j in plan.jobs if j.target.table == "customer").mode == "cdc"


def test_cdc_without_connector_is_reported_not_downgraded():
    """CDC 退成全量会改变数据语义——必须显式记 unsupported，不静默降级。"""
    ontology_id = _seed("cdcbad", platform="oracle")
    _set_cdc(ontology_id, "customer")
    plan = _build(ontology_id)

    assert not any(j.target.table == "customer" for j in plan.jobs)
    reasons = [u["reason"] for u in plan.unsupported if u["target"].endswith(".customer")]
    assert any("CDC" in r and "oracle" in r for r in reasons), reasons
    # 同本体里非 CDC 的表不受影响，照常产出
    assert any(j.target.table == "sales_order" for j in plan.jobs)


def test_selected_targets_filters_by_entity_name():
    plan = _build(_seed("selected"), selected_targets=["customer"])
    assert {j.target.table for j in plan.jobs} == {"customer"}


def test_runner_capabilities_gate_rejects_unsupported_sink():
    """runner 通道按执行侧 capabilities 判可搬性：目标引擎不在 sinks 里 → 全列 unsupported。"""
    ontology_id = _seed("capsink")
    caps = {
        "modes": ["full", "incremental"],
        "sources": ["mysql", "mariadb"],
        "sinks": ["doris"],  # 没有 hive；_build 的 engine=hive
    }
    plan = _build(ontology_id, runner_capabilities=caps)
    assert plan.jobs == ()
    assert any("hive" in u["reason"] for u in plan.unsupported)


def test_runner_capabilities_gate_allows_supported_combo():
    ontology_id = _seed("capok")
    caps = {
        "modes": ["full", "incremental"],
        "sources": ["mariadb"],  # _seed 默认 URN 平台是 mariadb
        "sinks": ["hive"],
    }
    plan = _build(ontology_id, runner_capabilities=caps)
    assert {j.target.table for j in plan.jobs} == {"customer", "sales_order"}


def test_runner_capabilities_gate_rejects_cdc_mode():
    """native 不做 CDC：capabilities.modes 无 cdc → CDC 表进 unsupported，不静默降级。"""
    ontology_id = _seed("capcdc")
    _set_cdc(ontology_id, "customer")
    caps = {"modes": ["full", "incremental"], "sources": ["mariadb"], "sinks": ["hive"]}
    plan = _build(ontology_id, runner_capabilities=caps)
    assert not any(j.target.table == "customer" for j in plan.jobs)
    assert any(
        "customer" in u["target"] and "cdc" in u["reason"].lower()
        for u in plan.unsupported
    )


def test_plan_is_idempotent():
    """同一本体重复生成，作业与渲染结果逐字节一致。"""
    ontology_id = _seed("idem")
    first = job_planner.render(_build(ontology_id))
    second = job_planner.render(_build(ontology_id))
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
        second, sort_keys=True, ensure_ascii=False
    )


def test_rendered_jobs_carry_no_credentials():
    plan = _build(_seed("nocred"))
    blob = json.dumps(job_planner.render(plan), ensure_ascii=False)
    for leaked in ("password=", "jdbc:mysql://", "root", "secret"):
        assert leaked not in blob
    assert "${ERP_READONLY_URL}" in blob


def test_runner_gate_judges_sink_and_mode_as_a_combination():
    """门禁必须按「目标 × 装载方式」判，不能分别判两个扁平集合。

    runner 可能一个档能写 hive 但只做全量、另一个档能做增量但写不了 hive；分别判会让
    「hive + 增量」通过门禁、跑到执行侧才失败——正是「不静默降级」要避免的那种。
    """
    from app.services.job_planner import _runner_reject

    caps = {
        "sources": ["mariadb"],
        "sinks": ["hive", "doris"],
        "modes": ["full", "incremental"],  # 扁平集合合看是「都支持」
        "sink_modes": {"hive": ["full"], "doris": ["full", "incremental"]},
    }
    assert _runner_reject(caps, "full", "mariadb", "hive") is None
    assert _runner_reject(caps, "incremental", "mariadb", "doris") is None

    reason = _runner_reject(caps, "incremental", "mariadb", "hive")
    assert reason and "hive" in reason and "full" in reason

    # 旧 runner（无 sink_modes）只能退回扁平判断，不能因此报错
    old = {k: v for k, v in caps.items() if k != "sink_modes"}
    assert _runner_reject(old, "incremental", "mariadb", "hive") is None
    assert _runner_reject(old, "cdc", "mariadb", "hive") is not None
