"""物理落点读模型：任务产出的表如何回到本体工作区。

钉住的核心约定：**任务建的表不重新建模，只登记为已有对象的落点。** 这些用例覆盖
读侧的汇总口径，以及同步对账把状态镜像给 Projection 的那条回写路径。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.models import (
    DataSource,
    DomainContext,
    IngestionContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    WarehouseObjectProjection,
)
from app.services.ingestion_contract import mirror_contract_to_projection
from app.services.object_landing import (
    FAILED,
    LANDED,
    REGISTERED,
    SCHEMA_READY,
    STALE,
    SYNCING,
    bulk_object_landings,
    object_landing,
)

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.customer,PROD)"


@pytest.fixture
def landing_seed(db):
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:landing-{token}", name=f"landing-{token}"
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=3)
    db.add(ontology)
    db.flush()
    obj = ObjectType(
        ontology_id=ontology.id,
        name=f"customer_{token}",
        display_name="客户",
        source_ref=_URN,
        table_role="business_object",
    )
    db.add(obj)
    doris = DataSource(
        name=f"Doris-{token}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.commit()
    return ontology, obj, doris


def _deployment(db, ontology, doris, **overrides):
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
        **overrides,
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


def test_unregistered_object_has_no_landing(db, landing_seed):
    """没有任何登记 → 返回 None，而不是一个看起来煞有介事的「未落地」对象。

    「查过了但没有」和「压根没查」必须分得开：读模型给 None，调用方才有机会区分。
    """
    _ontology, obj, _doris = landing_seed
    assert object_landing(db, obj.id) is None
    assert bulk_object_landings(db, [obj.id]) == {}


def test_materialized_but_not_synced_is_schema_ready(db, landing_seed):
    """物化只建表不搬数：落点看得见表名，但状态不是「已落地」。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj)
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing is not None
    assert landing.state == SCHEMA_READY
    assert landing.ods_table == "ods.ods_erp_customer"
    assert landing.serving_table == "dwd.dwd_customer"
    assert landing.queryable is False


def test_ready_contract_makes_object_landed(db, landing_seed):
    """同步跑完 → 对象在本体里直接显示已落地，无需任何人工建模。"""
    ontology, obj, doris = landing_seed
    now = datetime(2026, 8, 27, 10, 0, 0)
    _contract(db, ontology, obj, doris, status="ready", last_success_at=now)
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing.state == LANDED
    assert landing.ods_table == "ods.ods_erp_customer"
    assert landing.ods_mode == "full"
    assert landing.last_success_at == now


def test_registered_contract_is_not_claimed_as_built(db, landing_seed):
    """光起草了契约不等于表建好了：没物化过就只能说「待搬数」。"""
    ontology, obj, doris = landing_seed
    _contract(db, ontology, obj, doris, status="active")
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing.state == REGISTERED
    assert landing.schema_status is None


def test_running_contract_is_syncing(db, landing_seed):
    ontology, obj, doris = landing_seed
    _contract(db, ontology, obj, doris, status="running", mode="cdc")
    db.commit()

    assert object_landing(db, obj.id).state == SYNCING


def test_failed_transform_beats_ready_ods(db, landing_seed):
    """坏消息压过好消息：ODS 就绪但清洗失败，落点必须显示失败。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", transform_status="failed")
    db.commit()

    assert object_landing(db, obj.id).state == FAILED


def test_stale_ranks_below_running_and_above_ready(db, landing_seed):
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", transform_status="stale")
    db.commit()

    assert object_landing(db, obj.id).state == STALE


def test_not_required_transform_does_not_block_landed(db, landing_seed):
    """``not_required`` 是「这个对象不需要清洗」，不是一个未就绪的落点状态。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", transform_status="not_required")
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing.state == LANDED
    assert landing.serving_status is None


def test_contract_wins_over_projection_for_ods(db, landing_seed):
    """两套登记并存时以契约为准：只有它带 mode 与真实的最近成功时间。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, ods_table="stale_table", sync_status="empty")
    _contract(
        db, ontology, obj, doris, status="ready", mode="incremental",
        target_ods_table="ods_erp_customer",
    )
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing.ods_table == "ods.ods_erp_customer"
    assert landing.ods_mode == "incremental"


def test_latest_ontology_version_wins(db, landing_seed):
    """跨版本留下的历史契约不该盖住当前版本的落点。"""
    ontology, obj, doris = landing_seed
    _contract(db, ontology, obj, doris, status="failed")
    _contract(
        db, ontology, obj, doris, status="ready",
        ontology_version=ontology.version - 1,
        target_ods_table="ods_erp_customer_old",
    )
    db.commit()

    landing = object_landing(db, obj.id)
    assert landing.state == FAILED
    assert landing.ods_table == "ods.ods_erp_customer"


def test_bulk_lookup_is_two_queries_regardless_of_count(db, landing_seed):
    """列表页按页批量取：查询数不随对象数增长。"""
    ontology, obj, doris = landing_seed
    extra = ObjectType(
        ontology_id=ontology.id,
        name=f"{obj.name}_b",
        display_name="订单",
        table_role="business_object",
    )
    db.add(extra)
    db.flush()
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready")
    _projection(db, deployment, extra, sync_status="ready")
    db.commit()

    landings = bulk_object_landings(db, [obj.id, extra.id])
    assert set(landings) == {obj.id, extra.id}
    assert all(item.state == LANDED for item in landings.values())


# --- 回写：同步对账把状态镜像给 Projection -----------------------------------


def test_mirror_propagates_ready_to_projection(db, landing_seed):
    """契约就绪 → Projection 就绪。下游 transform 的 ODS 准入靠这一列。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    projection = _projection(db, deployment, obj, sync_status="empty")
    contract = _contract(
        db, ontology, obj, doris, status="ready",
        last_success_at=datetime(2026, 8, 27, 9, 0, 0),
    )
    db.commit()

    mirror_contract_to_projection(db, contract)
    db.commit()
    db.refresh(projection)
    assert projection.sync_status == "ready"
    assert projection.last_sync_at == contract.last_success_at


def test_mirror_propagates_failure(db, landing_seed):
    """失败必须落到 Projection：否则上次成功留下的 ready 会让工作区谎报已落地。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    projection = _projection(db, deployment, obj, sync_status="ready")
    contract = _contract(db, ontology, obj, doris, status="failed")
    db.commit()

    mirror_contract_to_projection(db, contract)
    db.commit()
    db.refresh(projection)
    assert projection.sync_status == "failed"
    assert projection.queryable is False


def test_mirror_ignores_intermediate_status(db, landing_seed):
    """draft/submitted 是中间态：没跑完的搬运不改写上一次的落数结论。"""
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    projection = _projection(db, deployment, obj, sync_status="ready")
    contract = _contract(db, ontology, obj, doris, status="submitted")
    db.commit()

    mirror_contract_to_projection(db, contract)
    db.commit()
    db.refresh(projection)
    assert projection.sync_status == "ready"


def test_mirror_without_deployment_is_noop(db, landing_seed):
    """还没物化过（无部署）时同步对账不应炸——只是没有可镜像的目标。"""
    ontology, obj, doris = landing_seed
    contract = _contract(db, ontology, obj, doris, status="ready")
    db.commit()

    assert mirror_contract_to_projection(db, contract) is None


# --- 端到端：落点确实到得了前端 ------------------------------------------------


def test_landing_reaches_the_object_list_and_detail_api(
    client, admin_headers, db, landing_seed
):
    """列表与详情两个读接口都带上落点。

    落点是派生字段，读模型按关键字构造——漏传一处就恒为「未落地」而不会报错
    （``source_provenance`` 就是这么在界面上把整域对象置灰的）。故在 API 层钉住。
    """
    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    _projection(db, deployment, obj, sync_status="ready", transform_status="not_required")
    _contract(
        db, ontology, obj, doris, status="ready",
        last_success_at=datetime(2026, 8, 27, 8, 30, 0),
    )
    db.commit()

    listed = client.get(
        "/api/object-types",
        params={"ontology_id": ontology.id},
        headers=admin_headers,
    )
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["id"] == obj.id)
    assert row["landing"]["state"] == "landed"
    assert row["landing"]["ods_table"] == "ods.ods_erp_customer"

    detail = client.get(f"/api/object-types/{obj.id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["landing"]["state"] == "landed"


def test_object_without_landing_serializes_as_null(
    client, admin_headers, db, landing_seed
):
    """没有落点登记时字段是 null——前端据此区分「未落地」与「没查」。"""
    ontology, obj, _doris = landing_seed
    listed = client.get(
        "/api/object-types",
        params={"ontology_id": ontology.id},
        headers=admin_headers,
    )
    row = next(item for item in listed.json()["items"] if item["id"] == obj.id)
    assert row["landing"] is None


# --- 口径的 ADS 落点：挂在口径上，不给它建业务对象 -----------------------------


def test_metric_ads_table_lands_on_the_logic_not_a_new_object(db, landing_seed):
    """指标任务产出的 ADS 表登记到 BusinessLogic，对象数不变。

    这是「任务建的表不重新建模」在口径侧的同一条规矩：ADS 表是口径的物化，
    给它造一个 ObjectType 就等于在本体里凭空多出一个并不存在的业务概念。
    """
    from app.models import BusinessLogic, WarehouseLogicProjection
    from app.services.object_landing import bulk_logic_landings

    ontology, obj, doris = landing_seed
    deployment = _deployment(db, ontology, doris)
    logic = BusinessLogic(
        ontology_id=ontology.id,
        name=f"gmv_{uuid.uuid4().hex[:6]}",
        display_name="GMV",
        logic_type="metric",
    )
    db.add(logic)
    db.flush()
    db.add(
        WarehouseLogicProjection(
            deployment_id=deployment.id,
            business_logic_id=logic.id,
            serving_database="ads",
            serving_table="ads_gmv",
            status="ready",
            queryable=True,
            last_success_at=datetime(2026, 8, 27, 7, 0, 0),
        )
    )
    objects_before = db.query(ObjectType).filter_by(ontology_id=ontology.id).count()
    db.commit()

    landing = bulk_logic_landings(db, [logic.id])[logic.id]
    assert landing.state == LANDED
    assert landing.serving_table == "ads.ads_gmv"
    assert landing.queryable is True
    # ADS 表没有变成第二个业务对象
    assert db.query(ObjectType).filter_by(ontology_id=ontology.id).count() == objects_before
    assert bulk_object_landings(db, [obj.id]) == {}
