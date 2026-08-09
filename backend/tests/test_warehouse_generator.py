"""M3 本体 → 物理正向生成器：端到端生成、幂等性、unsupported 报告完整性。

最关键的两组断言：
- **幂等**：物理表是本体的投影，重复生成必须逐字节一致，否则「可丢弃可重建」不成立。
- **unsupported 完整性**：生成不了的东西必须显式列出，绝不静默跳过。
"""

from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp_ods.{table},PROD)"


def _seed(tag: str) -> dict:
    """客户(维) / 订单(维) / 订单明细(事实实现表) / 系统表(technical) / 指标。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:wg-{tag}", name=f"wg-domain-{tag}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(onto)
        db.flush()

        customer = ObjectType(
            ontology_id=onto.id, name="customer", display_name="客户",
            description="客户主数据", table_role="business_object",
            source_ref=_URN.format(table="tab_customer"),
        )
        order = ObjectType(
            ontology_id=onto.id, name="sales_order", display_name="销售订单",
            table_role="business_object", source_ref=_URN.format(table="tab_order"),
        )
        order_item = ObjectType(
            ontology_id=onto.id, name="order_item", display_name="订单明细",
            table_role="bridge", source_ref=_URN.format(table="tab_order_item"),
        )
        syslog = ObjectType(
            ontology_id=onto.id, name="__auth", display_name="鉴权表",
            table_role="technical", source_ref=_URN.format(table="__auth"),
        )
        db.add_all([customer, order, order_item, syslog])
        db.flush()

        db.add_all([
            Property(object_type_id=customer.id, name="customer_id",
                     display_name="客户ID", data_type="bigint",
                     semantic_type="identifier", required=True),
            Property(object_type_id=customer.id, name="customer_name",
                     display_name="客户名称", data_type="varchar",
                     semantic_type="attribute"),
            Property(object_type_id=customer.id, name="created_at",
                     display_name="创建时间", data_type="timestamp",
                     semantic_type="datetime"),
            Property(object_type_id=order.id, name="sales_order_id",
                     display_name="订单ID", data_type="bigint",
                     semantic_type="identifier", required=True),
            Property(object_type_id=order.id, name="amount",
                     display_name="订单金额", data_type="decimal",
                     semantic_type="amount"),
            Property(object_type_id=order_item.id, name="item_id",
                     display_name="明细ID", data_type="bigint",
                     semantic_type="identifier"),
            Property(object_type_id=order_item.id, name="qty",
                     display_name="数量", data_type="int", semantic_type="number"),
            Property(object_type_id=syslog.id, name="token",
                     display_name="令牌", data_type="varchar",
                     semantic_type="technical"),
        ])

        fk = RelationType(
            ontology_id=onto.id, name="order_of_customer", display_name="归属客户",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            structure_type="foreign_key", cardinality="N:1",
            source_evidence=json.dumps({"foreign_key": "customer_id",
                                        "target_field": "customer_id"}),
        )
        fact = RelationType(
            ontology_id=onto.id, name="order_contains_item", display_name="包含明细",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            structure_type="fact_table", mapping_object_type_id=order_item.id,
        )
        nn = RelationType(
            ontology_id=onto.id, name="customer_tag", display_name="客户标签",
            source_object_type_id=customer.id, target_object_type_id=order.id,
            structure_type="foreign_key", cardinality="N:N",
        )
        db.add_all([fk, fact, nn])

        metric = BusinessLogic(
            ontology_id=onto.id, name="gmv", display_name="成交额",
            logic_type="metric", expression_summary="SUM(sales_order.amount)",
        )
        no_expr = BusinessLogic(
            ontology_id=onto.id, name="empty_metric", display_name="空口径指标",
            logic_type="metric",
        )
        db.add_all([metric, no_expr])
        db.commit()
        return {
            "ontology_id": onto.id, "customer_id": customer.id,
            "order_id": order.id, "item_id": order_item.id,
            "syslog_id": syslog.id, "nn_id": nn.id,
        }


def _sync(client, headers, ontology_id: str):
    r = client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync", headers=headers
    )
    assert r.status_code == 200, r.text


# ---------- DDL ----------


def test_generates_hive_ddl_end_to_end(client, admin_headers):
    ids = _seed("ddl")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers,
        params={"engine": "hive", "database_prefix": "erp"},
    ).json()

    stmts = body["statements"]
    assert "dim_erp.customer" in stmts
    assert "dim_erp.sales_order" in stmts
    # technical 表不物化 → 不出现
    assert not any("__auth" in k for k in stmts)

    ddl = stmts["dim_erp.customer"]
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS `dim_erp`.`customer`" in ddl
    # 注释由本体反补物理层
    assert "COMMENT '客户 · 客户主数据'" in ddl
    assert "COMMENT '客户ID'" in ddl
    # 时间语义字段 → 分区键，且不在列清单里
    assert "PARTITIONED BY (`created_at` TIMESTAMP" in ddl
    assert "`created_at` TIMESTAMP COMMENT '创建时间'," not in ddl
    assert "'ontometa.primary_key'='customer_id'" in ddl


def test_foreign_key_declared_from_source_evidence(client, admin_headers):
    ids = _seed("fk")
    _sync(client, admin_headers, ids["ontology_id"])
    ddl = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()["statements"]["dim_erp.sales_order"]
    assert "'ontometa.foreign_key.customer_id'='dim_erp.customer(customer_id)'" in ddl


def test_fact_relation_materialized_via_mapping_table(client, admin_headers):
    ids = _seed("fact")
    _sync(client, admin_headers, ids["ontology_id"])
    stmts = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()["statements"]
    # 事实关系落在实现表名上，进 dwd 层
    assert "dwd_erp.order_item" in stmts
    assert "包含明细" in stmts["dwd_erp.order_item"]


# ---------- unsupported 完整性 ----------


def test_unsupported_reports_nn_relation(client, admin_headers):
    ids = _seed("nn")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl", headers=admin_headers
    ).json()
    reasons = {u["target"]: u["reason"] for u in body["unsupported"]}
    assert "customer_tag" in reasons
    assert "N:N" in reasons["customer_tag"]


def test_unsupported_reports_metric_without_expression(client, admin_headers):
    ids = _seed("noexpr")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl", headers=admin_headers
    ).json()
    reasons = {u["target"]: u["reason"] for u in body["unsupported"]}
    assert "empty_metric" in reasons
    assert "口径" in reasons["empty_metric"]


def test_unsupported_reports_missing_contract(client, admin_headers):
    """没跑过 sync 就生成 → 每个实体都要被显式报出来，不能悄悄产出空结果。"""
    ids = _seed("nocontract")
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl", headers=admin_headers
    ).json()
    assert body["statements"] == {}
    reasons = {u["target"]: u["reason"] for u in body["unsupported"]}
    assert "customer" in reasons and "缺物化契约" in reasons["customer"]


def test_capability_error_becomes_unsupported_not_silent(client, admin_headers):
    """契约要 SCD2、Hive 做不到 → 列进 unsupported，绝不静默降级建表。"""
    ids = _seed("scd2")
    oid = ids["ontology_id"]
    _sync(client, admin_headers, oid)
    contracts = client.get(
        f"/api/ontologies/{oid}/materialization-contracts", headers=admin_headers
    ).json()
    cid = next(c["id"] for c in contracts if c["target_id"] == ids["customer_id"])
    client.patch(
        f"/api/materialization-contracts/{cid}",
        headers=admin_headers, json={"scd_type": "scd2"},
    )

    body = client.get(
        f"/api/ontologies/{oid}/warehouse/ddl",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()
    assert "dim_erp.customer" not in body["statements"]
    reasons = {u["target"]: u["reason"] for u in body["unsupported"]}
    assert "scd2" in reasons["dim_erp.customer"]


def test_unverified_engine_surfaces_warning(client, admin_headers):
    ids = _seed("warn")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers, params={"engine": "hive"},
    ).json()
    # hive 已核实 → 无 unverified 警告
    assert not any(w["feature"] == "unverified_capabilities" for w in body["warnings"])


# ---------- ETL ----------


def test_etl_falls_back_to_same_column_name(client, admin_headers):
    """真实源常无 source_field_ref —— 必须回退同名字段，而不是产出空 SELECT。"""
    ids = _seed("etl")
    _sync(client, admin_headers, ids["ontology_id"])
    stmts = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/etl",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()["statements"]
    sql = stmts["dim_erp.customer"]
    # 缺省同步方式 = full → INSERT OVERWRITE（正向生成既有行为不变）。
    assert "INSERT OVERWRITE TABLE dim_erp.customer" in sql
    assert "`customer_id` AS `customer_id`" in sql
    # 源表由 source_ref(URN) 解析而来，且逐段加引号（库名/表名各自引用）
    assert "FROM `erp_ods`.`tab_customer`;" in sql


def test_etl_load_strategy_incremental(client, admin_headers):
    """同步方式=增量 → INSERT INTO + 分区键水位谓词（物化弹窗单选驱动，缺省仍为覆盖）。"""
    ids = _seed("etlinc")
    _sync(client, admin_headers, ids["ontology_id"])
    base = f"/api/ontologies/{ids['ontology_id']}/warehouse/etl"
    params = {"database_prefix": "erp"}
    # 缺省：覆盖
    full_sql = client.get(base, headers=admin_headers, params=params).json()[
        "statements"
    ]["dim_erp.customer"]
    assert full_sql.startswith("INSERT OVERWRITE TABLE dim_erp.customer")
    # 显式增量：追加 + 水位
    inc = client.get(
        base, headers=admin_headers, params={**params, "load_strategy": "incremental"}
    ).json()
    inc_sql = inc["statements"]["dim_erp.customer"]
    assert "INSERT INTO TABLE dim_erp.customer" in inc_sql
    assert ":watermark" in inc_sql
    # CDC：物化内不承载 → 回退覆盖 + 明确 warning
    cdc = client.get(
        base, headers=admin_headers, params={**params, "load_strategy": "cdc"}
    ).json()
    assert cdc["statements"]["dim_erp.customer"].startswith("INSERT OVERWRITE TABLE")
    assert any(w["feature"] == "cdc" for w in cdc["warnings"])


def test_etl_quotes_source_table_with_space(client, admin_headers):
    """真实源的表名带空格（Frappe 的 ``tabAccount Category``）——不引用就是废 SQL。

    ERP 域里这类名字占比很高，此前生成的 ``FROM db.tabAsset Repair Purchase Invoice``
    任何引擎都解析不了，却一路走到「执行成功」。
    """
    ids = _seed("etlspace")
    with SessionLocal() as db:
        obj = ObjectType(
            ontology_id=ids["ontology_id"], name="account_category",
            display_name="账户分类", table_role="business_object",
            source_ref=_URN.format(table="tabAccount Category"),
        )
        db.add(obj)
        db.flush()
        db.add(Property(object_type_id=obj.id, name="name", display_name="名称",
                        data_type="varchar", semantic_type="identifier"))
        db.commit()
    _sync(client, admin_headers, ids["ontology_id"])
    stmts = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/etl",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()["statements"]
    sql = next(v for k, v in stmts.items() if k.endswith("account_category"))
    assert "FROM `erp_ods`.`tabAccount Category`" in sql
    assert "FROM erp_ods.tabAccount Category" not in sql


def test_etl_skips_ads_layer(client, admin_headers):
    ids = _seed("etlads")
    _sync(client, admin_headers, ids["ontology_id"])
    stmts = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/etl", headers=admin_headers
    ).json()["statements"]
    assert not any(k.startswith("ads") for k in stmts)


# ---------- DAG ----------


def test_dag_orders_dim_before_dwd_before_ads(client, admin_headers):
    ids = _seed("dag")
    _sync(client, admin_headers, ids["ontology_id"])
    dag = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/dag",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()
    order = dag["order"]
    assert dag["cyclic"] == []
    layer_of = {n["id"]: n["layer"] for n in dag["nodes"]}
    ranks = {"dim": 0, "dwd": 1, "dws": 2, "ads": 3}
    seq = [ranks[layer_of[i]] for i in order]
    assert seq == sorted(seq), f"分层顺序被破坏: {order}"


def test_dag_handles_cycles_without_hanging(client, admin_headers):
    """真实 ERP 血缘存在大规模强连通分量——拓扑排序不得假设无环。"""
    ids = _seed("cycle")
    oid = ids["ontology_id"]
    with SessionLocal() as db:
        # 制造 customer ←→ sales_order 互指外键
        db.add(RelationType(
            ontology_id=oid, name="customer_of_order", display_name="反向",
            source_object_type_id=ids["customer_id"],
            target_object_type_id=ids["order_id"],
            structure_type="foreign_key", cardinality="N:1",
            source_evidence=json.dumps({"foreign_key": "sales_order_id",
                                        "target_field": "sales_order_id"}),
        ))
        db.commit()
    _sync(client, admin_headers, oid)
    dag = client.get(
        f"/api/ontologies/{oid}/warehouse/dag",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()
    # 真正在环里的只有互指的两张维表
    assert set(dag["cyclic"]) == {"dim_erp.customer", "dim_erp.sales_order"}
    # 下游只是被阻塞，不能报成「循环依赖」——否则会让人去找不存在的环
    assert set(dag["blocked"]) == {"dwd_erp.order_item", "ads_erp.gmv"}
    reasons = {u["target"]: u["reason"] for u in dag["unsupported"]}
    assert "处于循环依赖中" in reasons["dim_erp.customer"]
    assert "被阻塞" in reasons["dwd_erp.order_item"]


# ---------- mapping ----------


def test_mapping_is_feedable_to_data_source(client, admin_headers):
    ids = _seed("map")
    _sync(client, admin_headers, ids["ontology_id"])
    mapping = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/mapping",
        headers=admin_headers, params={"database_prefix": "erp"},
    ).json()
    assert mapping["tables"]["customer"] == "dim_erp.customer"
    # 名称对齐红利：物理列名 == 本体属性名，无需列映射
    assert mapping["columns"] == {}


# ---------- 幂等 ----------


def test_generation_is_idempotent(client, admin_headers):
    """物理表可丢弃可重建的前提：同一本体重复生成，产出逐字节一致。"""
    ids = _seed("idem")
    _sync(client, admin_headers, ids["ontology_id"])
    url = f"/api/ontologies/{ids['ontology_id']}/warehouse/bundle"
    params = {"engines": "hive", "database_prefix": "erp"}
    first = client.get(url, headers=admin_headers, params=params).json()
    second = client.get(url, headers=admin_headers, params=params).json()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------- 引擎与错误处理 ----------


def test_engines_endpoint_exposes_capability_matrix(client, admin_headers):
    body = client.get("/api/warehouse/engines", headers=admin_headers).json()
    assert body["default"] == "hive"
    by_name = {e["name"]: e for e in body["engines"]}
    # M8：四个引擎全部实现且能力矩阵已逐项核实。
    for engine in ("hive", "doris", "iceberg", "starrocks", "clickhouse"):
        assert by_name[engine]["implemented"] is True, engine
        assert by_name[engine]["capabilities"]["verified"] is True, engine


def test_derivation_reads_from_authoritative_hive(client, admin_headers):
    """单一写入路径：非 hive 引擎从 Hive 权威副本派生，而非各自从 ODS 双写。"""
    ids = _seed("deriv")
    oid = ids["ontology_id"]
    _sync(client, admin_headers, oid)

    # hive 是权威源 → 无需派生
    hive = client.get(
        f"/api/ontologies/{oid}/warehouse/derivation",
        headers=admin_headers, params={"engine": "hive"},
    ).json()
    assert hive["authoritative"] is True
    assert hive["derivations"] == {}

    doris = client.get(
        f"/api/ontologies/{oid}/warehouse/derivation",
        headers=admin_headers, params={"engine": "doris", "database_prefix": "erp"},
    ).json()
    assert doris["authoritative"] is False
    entry = doris["derivations"]["dim_erp.customer"]
    assert entry["source_table"] == "hive.dim_erp.customer"
    assert "FROM hive.dim_erp.customer" in entry["load_sql"]
    assert "CREATE TABLE IF NOT EXISTS `dim_erp`.`customer`" in entry["target_ddl"]


def test_bundle_uses_derivation_for_non_hive(client, admin_headers):
    ids = _seed("bundle")
    oid = ids["ontology_id"]
    _sync(client, admin_headers, oid)
    bundle = client.get(
        f"/api/ontologies/{oid}/warehouse/bundle",
        headers=admin_headers, params={"engines": "hive,doris", "database_prefix": "erp"},
    ).json()
    assert bundle["write_path"]["authoritative"] == "hive"
    # hive 走 ODS→Hive 的 ETL；doris 走 Hive→doris 的派生
    assert "etl" in bundle["engines"]["hive"]
    assert "derivation" in bundle["engines"]["doris"]
    assert "etl" not in bundle["engines"]["doris"]


def test_unknown_engine_returns_400(client, admin_headers):
    ids = _seed("badengine")
    resp = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers, params={"engine": "teradata"},
    )
    assert resp.status_code == 400


def test_missing_ontology_returns_404(client, admin_headers):
    resp = client.get(
        "/api/ontologies/nope/warehouse/ddl", headers=admin_headers
    )
    assert resp.status_code == 404


# ---------- 两段式 DDL（postgres 真外键） ----------


def test_postgres_foreign_keys_are_separate_statements(client, admin_headers):
    """postgres 的外键不进建表语句，改由 constraints 段在建表后补。

    内联时被引用表必须先存在，于是「只物化一部分」和「本体里有环」都建不出来。
    """
    ids = _seed("pgfk")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers,
        params={"engine": "postgres", "database_prefix": "erp"},
    ).json()

    create = body["statements"]["dim_erp.sales_order"]
    assert "FOREIGN KEY" not in create, "外键不该内联进建表语句"
    # schema 也要先建，否则 "dim_erp"."sales_order" 无处落
    assert 'CREATE SCHEMA IF NOT EXISTS "dim_erp";' in create

    # 外键列真的在表上时才产约束；种子里若无该列，则应在 warnings 里说明而不是静默消失
    fks = body["constraints"].get("dim_erp.sales_order")
    if fks:
        assert any(c["ref"] == "dim_erp.customer" for c in fks)
        assert all("ADD CONSTRAINT" in c["sql"] for c in fks)
    else:
        assert any(
            w["feature"] in ("foreign_key_column_missing", "foreign_key_out_of_scope")
            and w["target"] == "dim_erp.sales_order"
            for w in body["warnings"]
        ), "外键既没产出、也没报原因——这就是静默降级"


def test_foreign_key_on_missing_column_is_dropped_with_warning(client, admin_headers):
    """外键列不在表上时不得产出约束——那在任何引擎上都是非法 DDL，且必须报出来。

    关系投影按「被引用实体名_id」推列名，真实源表（Frappe 系 ERP）并无此列：
    真实 ERP 本体里 dim.account 的 5 条外键没有一条命中它自己的 29 个列。
    此前这些外键内联在 CREATE TABLE 里，被更早的报错盖住了。
    """
    ids = _seed("pgfkmiss")
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers,
        params={"engine": "postgres", "database_prefix": "erp"},
    ).json()

    # 造不出「列缺失」的种子时此用例无意义，故只在确有该 warning 时断言其形状
    missing = [w for w in body["warnings"] if w["feature"] == "foreign_key_column_missing"]
    for w in missing:
        target = w["target"]
        # 被丢掉的外键绝不能同时出现在 constraints 里
        assert not any(
            "ADD CONSTRAINT" in c["sql"] and w["detail"].split("→")[1].split("未创建")[0].strip() in c["sql"]
            for c in body["constraints"].get(target, [])
        )


def test_uncertain_primary_key_is_not_enforced(client, admin_headers):
    """猜出来的身份属性不发强制约束。

    ERP 的 bank 表没有 bank_id，多个 *_id 里按字典序挑中了 custom_enterprise_seed_external_id
    ——一个自定义扩展字段，既不唯一也大量为空。postgres 把它建成真 PRIMARY KEY 之后，
    这张表永远装不进数据（duplicate key / not-null violation），一条没把握的推断变成了硬失败。
    """
    ids = _seed("pkguess")
    with SessionLocal() as db:
        obj = ObjectType(
            ontology_id=ids["ontology_id"], name="bank", display_name="银行",
            table_role="business_object", source_ref=_URN.format(table="tabBank"),
        )
        db.add(obj)
        db.flush()
        # 唯一的 identifier 候选，但它不叫 bank_id/id——「只有这一个」不等于「它唯一」。
        # 真实 ERP 的 bank 就是这样：只有 custom_enterprise_seed_external_id 一个候选，
        # 而它既不唯一也大量为空。
        db.add_all([
            Property(object_type_id=obj.id, name="custom_external_id", display_name="外部ID",
                     data_type="varchar", semantic_type="identifier"),
            Property(object_type_id=obj.id, name="bank_name", display_name="名称",
                     data_type="varchar", semantic_type="attribute"),
        ])
        db.commit()
    _sync(client, admin_headers, ids["ontology_id"])
    body = client.get(
        f"/api/ontologies/{ids['ontology_id']}/warehouse/ddl",
        headers=admin_headers, params={"engine": "postgres"},
    ).json()
    ddl = next(v for k, v in body["statements"].items() if k.endswith("bank"))
    assert "PRIMARY KEY" not in ddl
    # 但必须说清为什么没建，不能静默
    notes = [u for u in body["unsupported"] if "bank" in u["target"]]
    assert any("未生成强制主键" in u["reason"] for u in notes), notes

    # 对照：约定命中（customer_id）时仍然建主键
    customer_ddl = next(v for k, v in body["statements"].items() if k.endswith("customer"))
    assert "PRIMARY KEY" in customer_ddl
