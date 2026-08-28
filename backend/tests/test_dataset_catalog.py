"""数据集目录：数仓里的物理表如何成为**可被选中**的东西。

钉住的核心约定：目录不新增本体、不新增表，只把既有落点包装成稳定引用。特别钉死
**按槽位判状态**——下游要选的是「ODS 那张表好了没」，不能被这个对象自己的清洗失败染红。
"""

from __future__ import annotations

import uuid

import pytest

from app.models import (
    BusinessLogic,
    DataSource,
    DomainContext,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)
from app.services.dataset_catalog import (
    KIND_LOGIC,
    KIND_OBJECT,
    SLOT_ADS,
    SLOT_ODS,
    SLOT_SERVING,
    dataset_ref,
    list_datasets,
    parse_dataset_ref,
    resolve_dataset_ref,
)
from app.services.object_landing import FAILED, LANDED, REGISTERED, SCHEMA_READY

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)"


@pytest.fixture
def catalog_seed(db):
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:catalog-{token}", name=f"catalog-{token}"
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=2)
    db.add(ontology)
    db.flush()
    obj = ObjectType(
        ontology_id=ontology.id,
        name=f"customer_{token}",
        display_name="客户",
        source_ref=_URN,
        table_role="business_object",
    )
    doris = DataSource(
        name=f"Doris-{token}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add_all([obj, doris])
    db.commit()
    return ontology, obj, doris


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


def _projection(db, deployment, obj, **overrides):
    fields = {
        "schema_status": "ready",
        "sync_status": "empty",
        "transform_status": "not_required",
        "ods_database": "ods",
        "ods_table": "ods_erp_customer",
        "serving_layer": "dwd",
        "serving_database": "dwd",
        "serving_table": "dwd_customer",
        "queryable": False,
    }
    fields.update(overrides)
    projection = WarehouseObjectProjection(
        deployment_id=deployment.id, object_type_id=obj.id, **fields
    )
    db.add(projection)
    db.flush()
    return projection


def _contract(db, ontology, obj, doris, **overrides):
    fields = {
        "source_datasource_id": doris.id,
        "source_physical_table": "erp.customer",
        "target_ods_database": "ods",
        "target_ods_table": "ods_erp_customer",
        "mode": "full",
        "status": "draft",
        "ontology_version": ontology.version,
    }
    fields.update(overrides)
    contract = IngestionContract(
        ontology_id=ontology.id,
        object_type_id=obj.id,
        doris_datasource_id=doris.id,
        **fields,
    )
    db.add(contract)
    db.flush()
    return contract


def test_object_without_landing_is_not_in_catalog(db, catalog_seed):
    """没落点就没条目：目录列的是数仓里真有登记的表，不是本体的对象清单。"""
    ontology, _obj, _doris = catalog_seed
    assert list_datasets(db, ontology.id) == []


def test_landed_object_yields_ods_and_serving_entries(db, catalog_seed):
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", queryable=True)
    db.commit()

    entries = list_datasets(db, ontology.id)
    assert [e.slot for e in entries] == [SLOT_ODS, SLOT_SERVING]
    ods, serving = entries
    assert ods.physical == "ods.ods_erp_customer"
    assert ods.layer == "ods"
    assert ods.ref == dataset_ref(KIND_OBJECT, obj.id, SLOT_ODS)
    assert serving.physical == "dwd.dwd_customer"
    assert serving.layer == "dwd"
    # ODS 不对外服务；能查的是服务层那张。
    assert (ods.queryable, serving.queryable) == (False, True)


def test_slot_states_do_not_bleed_into_each_other(db, catalog_seed):
    """**目录的存在理由**：ODS 就绪、清洗失败时，ODS 那张表仍然是可选的源。

    对象级汇总（object_landing）此时是 failed —— 那是对的，人要看见红灯；但下游清洗
    要选的是 ODS，把它一起判成失败就等于说「源表坏了」，而源表好好的。
    """
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", transform_status="failed")
    _contract(db, ontology, obj, doris, status="ready")
    db.commit()

    by_slot = {e.slot: e for e in list_datasets(db, ontology.id)}
    assert by_slot[SLOT_ODS].state == LANDED
    assert by_slot[SLOT_ODS].source_ready is True
    assert by_slot[SLOT_SERVING].state == FAILED
    assert by_slot[SLOT_SERVING].source_ready is False


def test_drafted_contract_shows_as_registered_not_selectable(db, catalog_seed):
    """只起草了同步契约：表还没建，目录要列出来（人得知道它在路上），但不算可选源。"""
    ontology, obj, doris = catalog_seed
    _contract(db, ontology, obj, doris, status="active")
    db.commit()

    entries = list_datasets(db, ontology.id)
    assert [e.state for e in entries] == [REGISTERED]
    assert entries[0].source_ready is False
    assert list_datasets(db, ontology.id, source_ready_only=True) == []


def test_materialized_empty_table_is_still_selectable(db, catalog_seed):
    """物化建了空表：可以当源（有没有数由任务自检按本次 Spec 判，不在目录拦）。"""
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj)
    db.commit()

    serving = {e.slot: e for e in list_datasets(db, ontology.id)}[SLOT_SERVING]
    assert serving.state == SCHEMA_READY
    assert serving.source_ready is True


def test_serving_layer_ods_does_not_duplicate_the_table(db, catalog_seed):
    """服务层显式设成 ods 时两个槽指向同一张表；选择器里出现两次就是 bug。"""
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(
        db,
        deployment,
        obj,
        serving_layer="ods",
        serving_database="ods",
        serving_table="ods_erp_customer",
        sync_status="ready",
        queryable=True,
    )
    db.commit()

    entries = list_datasets(db, ontology.id)
    assert [e.physical for e in entries] == ["ods.ods_erp_customer"]
    assert entries[0].slot == SLOT_SERVING
    assert entries[0].queryable is True


def test_soft_deleted_object_leaves_the_catalog(db, catalog_seed):
    """人工删掉的对象不再出现在目录里：它的表已是无主表，该走认领而不是被继续选中。"""
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready")
    obj.deleted_by_user = True
    db.commit()

    assert list_datasets(db, ontology.id) == []


def test_resolve_agrees_with_the_listing_on_soft_deleted_entities(db, catalog_seed):
    """成员判定只能有一份：列表里没有的，解析也不能解析得出。

    两处不一致的后果很具体——一个引用「选不到却解析得出」，于是存量任务继续指着一张
    已经无主的表跑下去，而界面上那个对象早就不在了。
    """
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready")
    db.commit()
    ref = dataset_ref(KIND_OBJECT, obj.id, SLOT_ODS)
    assert resolve_dataset_ref(db, ref) is not None

    obj.deleted_by_user = True
    db.commit()

    assert list_datasets(db, ontology.id) == []
    assert resolve_dataset_ref(db, ref) is None


def test_logic_projection_lands_in_ads(db, catalog_seed):
    """指标的 ADS 表挂在口径上，不是业务对象——目录也照这个口径列。"""
    ontology, _obj, doris = catalog_seed
    logic = BusinessLogic(
        ontology_id=ontology.id,
        name=f"gmv_{uuid.uuid4().hex[:6]}",
        display_name="GMV",
        logic_type="metric",
    )
    db.add(logic)
    deployment = _deployment(db, ontology, doris)
    db.flush()
    db.add(
        WarehouseLogicProjection(
            deployment_id=deployment.id,
            business_logic_id=logic.id,
            serving_database="ads",
            serving_table="ads_gmv_daily",
            status="ready",
            queryable=True,
        )
    )
    db.commit()

    entries = list_datasets(db, ontology.id)
    assert [(e.entity_kind, e.layer, e.physical) for e in entries] == [
        (KIND_LOGIC, "ads", "ads.ads_gmv_daily")
    ]
    assert entries[0].ref == dataset_ref(KIND_LOGIC, logic.id, SLOT_ADS)


def test_filters_narrow_by_layer_query_and_queryability(db, catalog_seed):
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", queryable=True)
    db.commit()

    assert [e.slot for e in list_datasets(db, ontology.id, layer="ods")] == [SLOT_ODS]
    assert [e.slot for e in list_datasets(db, ontology.id, layer="dwd")] == [SLOT_SERVING]
    assert list_datasets(db, ontology.id, layer="dws") == []
    assert [e.slot for e in list_datasets(db, ontology.id, queryable_only=True)] == [
        SLOT_SERVING
    ]
    assert len(list_datasets(db, ontology.id, q="dwd_customer")) == 1
    assert len(list_datasets(db, ontology.id, q="客户")) == 2
    assert list_datasets(db, ontology.id, q="不存在的表") == []


def test_ref_round_trips_through_resolve(db, catalog_seed):
    """选的时候看到的，和跑的时候解析到的，必须是同一张表。"""
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready")
    db.commit()

    for entry in list_datasets(db, ontology.id):
        resolved = resolve_dataset_ref(db, entry.ref)
        assert resolved is not None
        assert resolved.physical == entry.physical
        assert resolved.state == entry.state


@pytest.mark.parametrize(
    "ref",
    ["", "ods.ods_erp_customer", "obj:missing", "obj:x@dwd", "logic:x@ods", "x:y@ods"],
)
def test_malformed_refs_resolve_to_none(db, ref):
    """引用会从 Spec / 表单 / 模型输出进来，形态不对时返回 None 而不是炸。

    ``obj:x@dwd`` 也在此列：引用指槽位不指层，写层名的引用一律不认——认了就等于
    默认「层不会变」，而层是契约里可改的。
    """
    assert parse_dataset_ref(ref) is None
    assert resolve_dataset_ref(db, ref) is None


def test_resolve_returns_none_for_unlanded_entity(db, catalog_seed):
    """引用语法对、实体也在，但没落点：给 None，别编一个表名出来。"""
    _ontology, obj, _doris = catalog_seed
    assert resolve_dataset_ref(db, dataset_ref(KIND_OBJECT, obj.id, SLOT_ODS)) is None


def test_api_lists_datasets(client, admin_headers, db, catalog_seed):
    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", queryable=True)
    db.commit()

    resp = client.get(
        f"/api/ontologies/{ontology.id}/datasets", headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [item["physical"] for item in body] == [
        "ods.ods_erp_customer",
        "dwd.dwd_customer",
    ]
    assert body[0]["ref"].endswith("@ods")
    assert body[1]["source_ready"] is True

    filtered = client.get(
        f"/api/ontologies/{ontology.id}/datasets",
        params={"layer": "dwd"},
        headers=admin_headers,
    ).json()
    assert [item["slot"] for item in filtered] == [SLOT_SERVING]


def test_api_404_for_unknown_ontology(client, admin_headers):
    resp = client.get("/api/ontologies/nope/datasets", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------- Data Agent


def test_agent_tool_lists_landings(client, db, catalog_seed):
    """Data Agent 拿得到落点表名。

    模型若靠命名规则自己拼 ``ods_xx_yy``，拼出来的表可能压根不存在——目录是唯一说法，
    所以这条工具必须真的返回登记里的物理表名。
    """
    from app.api.deps import chat_bi_service as svc

    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", queryable=True)
    db.commit()

    result, summary, is_error = svc._dispatch_list_datasets(
        db, ontology_id=ontology.id, args={"layer": "ods"}
    )
    assert is_error is False
    assert [item["physical"] for item in result["items"]] == ["ods.ods_erp_customer"]
    assert result["items"][0]["ref"].endswith("@ods")
    assert "1" in summary


def test_agent_tool_requires_anchored_ontology(client, db):
    """没锚定本体就明确报错，别返回一个空目录让模型以为数仓是空的。"""
    from app.api.deps import chat_bi_service as svc

    result, _summary, is_error = svc._dispatch_list_datasets(
        db, ontology_id="", args={}
    )
    assert is_error is True and "error" in result


def test_agent_tool_result_is_registered_as_fact(client, db, catalog_seed):
    """表名要进事实账本：答案说「已落到 ods.ods_erp_customer」不能被 F4 判成幻觉。

    见 ``propose_* 工具必须登记账本`` 那条同源教训——只读目录工具漏登记，症状一样。
    """
    from app.api.deps import chat_bi_service as svc
    from app.services.agent_grounding import FactLedger

    ontology, obj, doris = catalog_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready")
    db.commit()

    result, _summary, _is_error = svc._dispatch_list_datasets(
        db, ontology_id=ontology.id, args={}
    )
    ledger = FactLedger()
    svc._ledger_register(ledger, "list_datasets", result, False)
    assert ledger.has_entity_named("ods.ods_erp_customer")
    assert ledger.has_entity_named("客户")
