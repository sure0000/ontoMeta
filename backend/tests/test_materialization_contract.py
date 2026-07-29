"""M1 物化契约：默认推导规则、三方合并（人工钉住不被覆盖）、唯一约束。"""

from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    MaterializationContract,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)


def _seed(tag: str) -> dict:
    """一个本体：业务对象(带时间字段) / 技术表 / 事实关系 / 外键关系 / 业务逻辑。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mc-{tag}",
            name=f"mc-domain-{tag}",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()

        customer = ObjectType(
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            table_role="business_object",
        )
        order = ObjectType(
            ontology_id=ontology.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
        )
        sys_log = ObjectType(
            ontology_id=ontology.id,
            name="__auth_log",
            display_name="鉴权日志",
            table_role="technical",
        )
        db.add_all([customer, order, sys_log])
        db.flush()

        # customer 有时间语义字段 → 应推出增量 + 分区键；order 没有 → 全量
        db.add_all(
            [
                Property(
                    object_type_id=customer.id,
                    name="created_at",
                    display_name="创建时间",
                    semantic_type="datetime",
                ),
                Property(
                    object_type_id=customer.id,
                    name="cust_name",
                    display_name="客户名称",
                    semantic_type="attribute",
                ),
                Property(
                    object_type_id=order.id,
                    name="order_no",
                    display_name="订单号",
                    semantic_type="identifier",
                ),
            ]
        )

        fact = RelationType(
            ontology_id=ontology.id,
            name="places",
            display_name="下单",
            source_object_type_id=customer.id,
            target_object_type_id=order.id,
            structure_type="fact_table",
        )
        fk = RelationType(
            ontology_id=ontology.id,
            name="belongs_to",
            display_name="归属",
            source_object_type_id=order.id,
            target_object_type_id=customer.id,
            structure_type="foreign_key",
        )
        db.add_all([fact, fk])

        logic = BusinessLogic(
            ontology_id=ontology.id,
            name="gmv",
            display_name="成交额",
            logic_type="metric",
        )
        db.add(logic)
        db.commit()
        return {
            "ontology_id": ontology.id,
            "customer_id": customer.id,
            "order_id": order.id,
            "sys_log_id": sys_log.id,
            "fact_id": fact.id,
            "fk_id": fk.id,
            "logic_id": logic.id,
        }


def _contracts_by_target(ontology_id: str) -> dict[str, MaterializationContract]:
    with SessionLocal() as db:
        rows = (
            db.query(MaterializationContract)
            .filter(MaterializationContract.ontology_id == ontology_id)
            .all()
        )
        return {r.target_id: r for r in rows}


def test_sync_derives_defaults_by_role_and_structure(client, admin_headers):
    ids = _seed("derive")
    resp = client.post(
        f"/api/ontologies/{ids['ontology_id']}/materialization-contracts/sync",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 3 对象 + 2 关系 + 1 逻辑
    assert body["total"] == 6
    assert body["created"] == 6

    by_target = _contracts_by_target(ids["ontology_id"])

    # 业务对象 → dim 且物化
    customer = by_target[ids["customer_id"]]
    assert customer.target_layer == "dim"
    assert customer.materialized is True
    # 有 datetime 字段 → 增量 + 该字段做分区键
    assert customer.load_strategy == "incremental"
    assert customer.partition_key == "created_at"
    assert customer.engines == ["hive"]

    # 无时间语义字段 → 全量、无分区键
    order = by_target[ids["order_id"]]
    assert order.load_strategy == "full"
    assert order.partition_key is None

    # 技术表 → 不落物理表
    assert by_target[ids["sys_log_id"]].materialized is False

    # 事实关系 → dwd 且物化；外键关系 → 不落表（外键是列声明）
    assert by_target[ids["fact_id"]].target_layer == "dwd"
    assert by_target[ids["fact_id"]].materialized is True
    assert by_target[ids["fk_id"]].materialized is False

    # 业务逻辑 → ads
    assert by_target[ids["logic_id"]].target_layer == "ads"


def test_sync_is_idempotent(client, admin_headers):
    ids = _seed("idem")
    url = f"/api/ontologies/{ids['ontology_id']}/materialization-contracts/sync"
    client.post(url, headers=admin_headers)
    second = client.post(url, headers=admin_headers).json()
    assert second["created"] == 0
    assert second["updated"] == 0


def test_manual_edit_pins_field_against_resync(client, admin_headers):
    ids = _seed("pin")
    ontology_id = ids["ontology_id"]
    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    contract_id = _contracts_by_target(ontology_id)[ids["customer_id"]].id

    # 人工把客户维度改成 SCD2 + 双引擎
    resp = client.patch(
        f"/api/materialization-contracts/{contract_id}",
        headers=admin_headers,
        json={"scd_type": "scd2", "engines": ["hive", "doris"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scd_type"] == "scd2"
    assert body["engines"] == ["hive", "doris"]
    assert set(body["pinned_fields"]) == {"scd_type", "target_engines"}
    assert body["origin"] == "machine_edited"

    # 机器重推导：钉住的不动，未钉住的仍由机器管
    sync = client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    ).json()
    assert sync["skipped_pinned"] >= 2

    after = _contracts_by_target(ontology_id)[ids["customer_id"]]
    assert after.scd_type == "scd2"
    assert after.engines == ["hive", "doris"]
    assert after.partition_key == "created_at"  # 未钉住，仍是机器推导值


def test_machine_reclaims_unpinned_field_after_role_change(client, admin_headers):
    """未钉住的字段必须跟随本体变化——否则契约会与本体脱节。"""
    ids = _seed("reclaim")
    ontology_id = ids["ontology_id"]
    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    assert _contracts_by_target(ontology_id)[ids["customer_id"]].materialized is True

    with SessionLocal() as db:
        obj = db.get(ObjectType, ids["customer_id"])
        obj.table_role = "technical"
        db.commit()

    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    assert _contracts_by_target(ontology_id)[ids["customer_id"]].materialized is False


def test_upstream_removed_marked_not_deleted(client, admin_headers):
    """本体实体消失时标记而非删除，保留人工配置以便实体回来时复用。"""
    ids = _seed("removed")
    ontology_id = ids["ontology_id"]
    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    with SessionLocal() as db:
        db.query(RelationType).filter(RelationType.id == ids["fk_id"]).delete()
        db.commit()

    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    contract = _contracts_by_target(ontology_id)[ids["fk_id"]]
    assert contract.upstream_removed is True


def test_list_filters_and_target_names(client, admin_headers):
    ids = _seed("list")
    ontology_id = ids["ontology_id"]
    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )

    all_rows = client.get(
        f"/api/ontologies/{ontology_id}/materialization-contracts",
        headers=admin_headers,
    ).json()
    assert len(all_rows) == 6
    customer_row = next(r for r in all_rows if r["target_id"] == ids["customer_id"])
    assert customer_row["target_name"] == "customer"
    assert customer_row["target_display_name"] == "客户"

    only_objects = client.get(
        f"/api/ontologies/{ontology_id}/materialization-contracts",
        headers=admin_headers,
        params={"target_kind": "object_type"},
    ).json()
    assert {r["target_kind"] for r in only_objects} == {"object_type"}

    materialized = client.get(
        f"/api/ontologies/{ontology_id}/materialization-contracts",
        headers=admin_headers,
        params={"materialized_only": True},
    ).json()
    # 技术表与外键关系被排除
    assert all(r["materialized"] for r in materialized)
    assert len(materialized) == 4


def test_unique_constraint_per_target(client, admin_headers):
    ids = _seed("unique")
    ontology_id = ids["ontology_id"]
    client.post(
        f"/api/ontologies/{ontology_id}/materialization-contracts/sync",
        headers=admin_headers,
    )
    with SessionLocal() as db:
        db.add(
            MaterializationContract(
                ontology_id=ontology_id,
                target_kind="object_type",
                target_id=ids["customer_id"],
            )
        )
        raised = False
        try:
            db.commit()
        except Exception:  # noqa: BLE001 — 期望唯一约束拒绝
            raised = True
            db.rollback()
        assert raised


def test_missing_ontology_returns_404(client, admin_headers):
    resp = client.get(
        "/api/ontologies/does-not-exist/materialization-contracts",
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_machine_baseline_recorded(client, admin_headers):
    ids = _seed("baseline")
    client.post(
        f"/api/ontologies/{ids['ontology_id']}/materialization-contracts/sync",
        headers=admin_headers,
    )
    contract = _contracts_by_target(ids["ontology_id"])[ids["customer_id"]]
    baseline = json.loads(contract.machine_baseline)
    assert baseline["target_layer"] == "dim"
    assert baseline["load_strategy"] == "incremental"
