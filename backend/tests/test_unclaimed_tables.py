"""无主表：数仓里有、本体里没人认领的那些表怎么回到治理里。

钉住的两条：**只给认领，不给照着表建对象**（反推出来的对象正是重复对象的来源）；
**认领只登记归属**——不代表平台搬过数据，故不写最近成功时间、不放行查询网关。
"""

from __future__ import annotations

import uuid

import pytest

from app.models import (
    DataSource,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services import dataset_catalog, unclaimed_tables
from app.services.unclaimed_tables import (
    UnclaimedTableError,
    claim_table,
    layer_of_database,
    list_unclaimed_tables,
    managed_databases,
)

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.{t},PROD)"


@pytest.fixture
def warehouse(db, monkeypatch):
    """一个本体 + 一个默认 Doris；数仓里的表由替身返回，不连真库。"""
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:claim-{token}", name=f"claim-{token}"
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=2)
    db.add(ontology)
    db.flush()
    # 默认数仓在库里唯一（data_sources.is_default_warehouse 有唯一约束），
    # 用例之间共用同一行，不能每个用例各建一个。
    doris = (
        db.query(DataSource)
        .filter(DataSource.is_default_warehouse.is_(True))
        .first()
    )
    if doris is None:
        doris = DataSource(
            name=f"Doris-{token}",
            kind="doris",
            purpose="warehouse",
            enabled=True,
            is_default_warehouse=True,
            dsn_secret_ref="mysql+pymysql://reader@fe:9030",
        )
        db.add(doris)
        db.flush()
    obj = ObjectType(
        ontology_id=ontology.id,
        name=f"customer_{token}",
        display_name="客户",
        source_ref=_URN.format(t="customer"),
        table_role="business_object",
    )
    db.add(obj)
    db.commit()

    tables: dict[str, list[str]] = {
        "ods": [f"ods_erp_customer_{token}", f"legacy_dump_{token}"],
        "dwd": [f"dwd_manual_wide_{token}"],
    }

    def fake_list_tables(dsn: str, database: str | None = None) -> list[str]:
        if database not in tables:
            from app.services.data_app_executor import ExecutionError

            raise ExecutionError(f"库 {database} 不存在")
        return sorted(tables[database])

    monkeypatch.setattr(unclaimed_tables, "list_tables", fake_list_tables)
    return {
        "ontology": ontology,
        "doris": doris,
        "object": obj,
        "token": token,
        "tables": tables,
    }


def _deployment(db, ontology, doris):
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    return deployment


def test_lists_only_tables_nobody_claims(db, warehouse):
    ontology, obj, doris = warehouse["ontology"], warehouse["object"], warehouse["doris"]
    token = warehouse["token"]
    deployment = _deployment(db, ontology, doris)
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="ready",
            sync_status="ready",
            transform_status="not_required",
            ods_database="ods",
            ods_table=f"ods_erp_customer_{token}",
            serving_database="dwd",
            serving_table="dwd_customer",
            serving_layer="dwd",
        )
    )
    db.commit()

    items, scanned = list_unclaimed_tables(db, ontology.id)
    physical = [t.physical for t in items]
    # 已认领的那张不在清单里；库里另外两张在。
    assert f"ods.ods_erp_customer_{token}" not in physical
    assert f"ods.legacy_dump_{token}" in physical
    assert f"dwd.dwd_manual_wide_{token}" in physical
    assert scanned == ["dwd", "ods"] or scanned == ["ods", "dwd"]


def test_only_scans_databases_this_ontology_writes_to(db, warehouse):
    """默认不扫整个数仓：对别的域、别的系统的表宣称「无主」是越权。"""
    ontology, obj, doris = warehouse["ontology"], warehouse["object"], warehouse["doris"]
    assert managed_databases(db, ontology.id) == ["ods"]

    deployment = _deployment(db, ontology, doris)
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="ready",
            sync_status="empty",
            transform_status="not_required",
            serving_database="dwd",
            serving_table="dwd_customer",
            serving_layer="dwd",
        )
    )
    db.commit()
    assert managed_databases(db, ontology.id) == ["dwd", "ods"]


def test_missing_database_is_skipped_not_fatal(db, warehouse):
    """还没物化过的层库不存在很正常：跳过并如实说扫了哪些，不让整份清单失败。"""
    ontology = warehouse["ontology"]
    items, scanned = list_unclaimed_tables(db, ontology.id, database="dws")
    assert items == [] and scanned == []


def test_orphan_registration_leaves_the_table_unclaimed(db, warehouse):
    """对象被人工删掉后，它的表就是无主表——正该回到清单里等人重新认领。

    这是 ``check_landing_orphans --reattach`` 的人工版：登记行还在，但主人没了，
    「已认领」就不该再算数。
    """
    ontology, obj, doris = warehouse["ontology"], warehouse["object"], warehouse["doris"]
    token = warehouse["token"]
    deployment = _deployment(db, ontology, doris)
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="ready",
            sync_status="ready",
            transform_status="not_required",
            ods_database="ods",
            ods_table=f"ods_erp_customer_{token}",
        )
    )
    db.commit()
    assert f"ods.ods_erp_customer_{token}" not in [
        t.physical for t in list_unclaimed_tables(db, ontology.id)[0]
    ]

    obj.deleted_by_user = True
    db.commit()

    assert f"ods.ods_erp_customer_{token}" in [
        t.physical for t in list_unclaimed_tables(db, ontology.id)[0]
    ]


def test_claim_registers_the_landing_without_claiming_freshness(db, warehouse):
    """认领只登记归属：表在（schema_status=ready），但平台没搬过——不写最近成功时间，
    也不放行查询网关（那要由一次真实成功之后的对账决定）。"""
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]

    entry = claim_table(
        db,
        ontology.id,
        object_type_id=obj.id,
        database="dwd",
        table=f"dwd_manual_wide_{token}",
    )
    assert entry.physical == f"dwd.dwd_manual_wide_{token}"
    assert entry.layer == "dwd"
    assert entry.source_ready is True  # 可以拿来当下游加工的源
    assert entry.queryable is False  # 但不给查询网关放行
    assert entry.last_success_at is None

    # 认领后它就从无主表清单里消失，并出现在数据集目录里。
    assert f"dwd.dwd_manual_wide_{token}" not in [
        t.physical for t in list_unclaimed_tables(db, ontology.id)[0]
    ]
    assert entry.ref in {e.ref for e in dataset_catalog.list_datasets(db, ontology.id)}


def test_claiming_an_ods_table_fills_the_ods_slot(db, warehouse):
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]
    entry = claim_table(
        db,
        ontology.id,
        object_type_id=obj.id,
        database="ods",
        table=f"legacy_dump_{token}",
    )
    assert entry.slot == dataset_catalog.SLOT_ODS
    assert entry.state == "landed"
    assert entry.queryable is False


def test_claiming_a_taken_table_is_refused(db, warehouse):
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]
    claim_table(
        db, ontology.id, object_type_id=obj.id, database="dwd", table=f"dwd_manual_wide_{token}"
    )
    other = ObjectType(
        ontology_id=ontology.id,
        name=f"other_{token}",
        display_name="别的对象",
        table_role="business_object",
    )
    db.add(other)
    db.commit()

    with pytest.raises(UnclaimedTableError) as err:
        claim_table(
            db,
            ontology.id,
            object_type_id=other.id,
            database="dwd",
            table=f"dwd_manual_wide_{token}",
        )
    assert "已经有主" in str(err.value)


def test_second_claim_on_the_same_slot_is_refused(db, warehouse):
    """一个对象在一层只能有一个落点：静默改指向会让存量任务读到另一张表。"""
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]
    claim_table(
        db, ontology.id, object_type_id=obj.id, database="dwd", table=f"dwd_manual_wide_{token}"
    )
    warehouse["tables"]["dwd"].append(f"dwd_another_{token}")

    with pytest.raises(UnclaimedTableError) as err:
        claim_table(
            db,
            ontology.id,
            object_type_id=obj.id,
            database="dwd",
            table=f"dwd_another_{token}",
        )
    assert "已经是" in str(err.value)


def test_deleted_object_cannot_be_the_owner(db, warehouse):
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]
    obj.deleted_by_user = True
    db.commit()
    with pytest.raises(UnclaimedTableError) as err:
        claim_table(
            db,
            ontology.id,
            object_type_id=obj.id,
            database="dwd",
            table=f"dwd_manual_wide_{token}",
        )
    assert "已被人工删除" in str(err.value)


def test_object_from_another_ontology_is_refused(db, warehouse):
    ontology = warehouse["ontology"]
    token = warehouse["token"]
    other_domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:other-{token}", name=f"other-{token}"
    )
    db.add(other_domain)
    db.flush()
    other_onto = Ontology(domain_context_id=other_domain.id, status="draft", version=1)
    db.add(other_onto)
    db.flush()
    stranger = ObjectType(
        ontology_id=other_onto.id,
        name=f"stranger_{token}",
        display_name="别的本体的对象",
        table_role="business_object",
    )
    db.add(stranger)
    db.commit()

    with pytest.raises(UnclaimedTableError):
        claim_table(
            db,
            ontology.id,
            object_type_id=stranger.id,
            database="dwd",
            table=f"dwd_manual_wide_{token}",
        )


@pytest.mark.parametrize(
    "database,expected",
    [("ods", "ods"), ("dwd", "dwd"), ("dwd_erp", "dwd"), ("dim_erp", "dim"), ("staging", None)],
)
def test_layer_is_derived_from_the_database_name_or_left_unknown(database, expected):
    """推不出层就给 None：层会写进落点登记，猜错了那张表会挂在错误的分层下。"""
    assert layer_of_database(database) == expected


def test_api_lists_and_claims(client, admin_headers, db, warehouse):
    ontology, obj = warehouse["ontology"], warehouse["object"]
    token = warehouse["token"]

    listed = client.get(
        f"/api/ontologies/{ontology.id}/unclaimed-tables", headers=admin_headers
    )
    assert listed.status_code == 200, listed.text
    assert f"ods.legacy_dump_{token}" in [i["physical"] for i in listed.json()["items"]]

    claimed = client.post(
        f"/api/ontologies/{ontology.id}/claim-table",
        json={"object_type_id": obj.id, "database": "ods", "table": f"legacy_dump_{token}"},
        headers=admin_headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["physical"] == f"ods.legacy_dump_{token}"

    again = client.post(
        f"/api/ontologies/{ontology.id}/claim-table",
        json={"object_type_id": obj.id, "database": "ods", "table": f"legacy_dump_{token}"},
        headers=admin_headers,
    )
    assert again.status_code == 400
