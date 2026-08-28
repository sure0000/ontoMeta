"""派生建模：什么时候才该在本体里多出一个实体。

钉住的核心判据：**换了粒度才换实体，换了层只换落点。** 所以 grain 必填、上游必须来自
数仓落点目录、连接条件必须把每个上游都接进来（少一条就是一次静默的笛卡尔积）；而派生
对象**不能同步**（它的上游在数仓不在源库），也不能被当成单源清洗的目标。
"""

from __future__ import annotations

import uuid

import pytest

from app.models import (
    DataSource,
    DerivedDefinition,
    DomainContext,
    MaterializationContract,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    Property,
    WarehouseObjectProjection,
)
from app.models.warehouse import TargetKind
from app.services import dataset_catalog, derived_object
from app.services.derived_object import (
    DerivedObjectError,
    DerivedObjectInput,
    FieldSource,
    JoinCondition,
    UpstreamJoin,
    create_derived_object,
    get_definition,
)
from app.services.source_ref import has_physical_source, provenance_of

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.{name},PROD)"


def _object(db, ontology, name, display, props=("id", "amount")):
    obj = ObjectType(
        ontology_id=ontology.id,
        name=name,
        display_name=display,
        source_ref=_URN.format(name=name),
        table_role="business_object",
    )
    db.add(obj)
    db.flush()
    for prop in props:
        db.add(
            Property(
                object_type_id=obj.id,
                name=prop,
                display_name=prop.upper(),
                semantic_type="identifier" if prop.endswith("id") else "amount",
                data_type="BIGINT" if prop.endswith("id") else "DECIMAL(18,2)",
            )
        )
    db.flush()
    return obj


def _land(db, ontology, doris, obj, table):
    """给对象一个已落地的 ODS 落点，让它进得了数据集目录。"""
    deployment = (
        db.query(OntologyWarehouseDeployment)
        .filter(OntologyWarehouseDeployment.ontology_id == ontology.id)
        .first()
    )
    if deployment is None:
        deployment = OntologyWarehouseDeployment(
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            doris_datasource_id=doris.id,
            status="schema_ready",
        )
        db.add(deployment)
        db.flush()
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="ready",
            sync_status="ready",
            transform_status="not_required",
            ods_database="ods",
            ods_table=table,
            serving_layer=None,
            serving_database=None,
            serving_table=None,
            queryable=False,
        )
    )
    db.flush()
    return dataset_catalog.dataset_ref(
        dataset_catalog.KIND_OBJECT, obj.id, dataset_catalog.SLOT_ODS
    )


@pytest.fixture
def derived_seed(db):
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:derived-{token}", name=f"derived-{token}"
    )
    db.add(domain)
    db.flush()
    ontology = Ontology(domain_context_id=domain.id, status="published", version=1)
    db.add(ontology)
    db.flush()
    doris = DataSource(
        name=f"Doris-{token}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.flush()
    order = _object(db, ontology, f"sales_order_{token}", "销售订单", ("id", "amount"))
    item = _object(db, ontology, f"order_item_{token}", "订单明细", ("id", "order_id"))
    order_ref = _land(db, ontology, doris, order, f"ods_erp_order_{token}")
    item_ref = _land(db, ontology, doris, item, f"ods_erp_item_{token}")
    db.commit()
    return ontology, order, item, order_ref, item_ref


def _payload(order_ref, item_ref, token, **overrides):
    data = dict(
        name=f"order_wide_{token}",
        display_name="订单商品宽表",
        grain="一行 = 一张订单的一个商品行",
        upstream_refs=[order_ref, item_ref],
        joins=[
            UpstreamJoin(
                left_ref=order_ref,
                right_ref=item_ref,
                on=[JoinCondition(left="id", right="order_id")],
            )
        ],
        fields=[
            FieldSource(property="order_id", from_ref=order_ref, from_column="id"),
            FieldSource(property="amount", from_ref=order_ref, from_column="amount"),
        ],
    )
    data.update(overrides)
    return DerivedObjectInput(**data)


def test_derived_object_joins_the_same_ontology(db, derived_seed):
    """派生对象是**同一个本体**里的新实体——不会因为加工出一张表就多出一个本体。"""
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]

    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    obj = db.get(ObjectType, result.object_type_id)
    assert obj.ontology_id == ontology.id
    assert obj.table_role == "business_object"
    # 人显式建的：机器全量重扫不得删改它，也不该再要人复核一遍。
    assert obj.user_created is True and obj.needs_review is False
    assert provenance_of(obj.source_ref) == "derived"
    # 属性从上游照抄语义类型，不重新猜。
    props = {p.name: p for p in db.query(Property).filter(Property.object_type_id == obj.id)}
    assert set(props) == {"order_id", "amount"}
    assert props["amount"].semantic_type == "amount"
    assert props["amount"].data_type == "DECIMAL(18,2)"
    assert db.query(Ontology).filter(Ontology.domain_context_id == ontology.domain_context_id).count() == 1


def test_derived_object_cannot_be_synced(db, derived_seed):
    """派生对象没有源库表：同步 Drafter 必须拒，且要说清它的上游在数仓里。"""
    from app.agents.drafters.sync import SyncDrafter

    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))
    obj = db.get(ObjectType, result.object_type_id)
    assert has_physical_source(obj.source_ref) is False

    with pytest.raises(ValueError) as err:
        SyncDrafter().draft(
            "同步这个对象",
            {"ontology_id": ontology.id, "object_type": obj.name},
        )
    assert "派生对象" in str(err.value)


def test_derived_object_has_no_ods_landing_name(db, derived_seed):
    """不给派生对象编 ODS 表名：编出来下游就会去读一张谁都没建过的表。"""
    from app.services.ods_naming import OdsNamingError, target_ods_table_name

    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))
    obj = db.get(ObjectType, result.object_type_id)

    with pytest.raises(OdsNamingError) as err:
        target_ods_table_name(db, ontology.id, obj)
    assert "派生对象" in str(err.value)


def test_grain_is_required(db, derived_seed):
    """粒度就是判据本身。允许留空 = 允许把一个 1:1 落点包装成新对象。"""
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(DerivedObjectError) as err:
        create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token, grain="  "))
    assert "粒度" in str(err.value)


def test_missing_join_is_rejected_as_cartesian_product(db, derived_seed):
    """少一条连接条件不会报错，只会安静地把行数乘起来——所以在这里拦。"""
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(DerivedObjectError) as err:
        create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token, joins=[]))
    assert "笛卡尔积" in str(err.value)


def test_empty_join_condition_is_rejected(db, derived_seed):
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(DerivedObjectError):
        create_derived_object(
            db,
            ontology.id,
            _payload(
                order_ref,
                item_ref,
                token,
                joins=[UpstreamJoin(left_ref=order_ref, right_ref=item_ref, on=[])],
            ),
        )


def test_upstream_must_come_from_the_catalog(db, derived_seed):
    """上游只能从数仓落点里选：能选的和能用的必须是同一份清单。"""
    ontology, _order, _item, order_ref, _item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    bogus = "obj:not-a-real-object@ods"
    with pytest.raises(DerivedObjectError) as err:
        create_derived_object(
            db,
            ontology.id,
            _payload(
                order_ref,
                bogus,
                token,
                upstream_refs=[order_ref, bogus],
                joins=[],
                fields=[FieldSource(property="order_id", from_ref=order_ref, from_column="id")],
            ),
        )
    assert bogus in str(err.value)


def test_single_upstream_needs_no_join(db, derived_seed):
    """单上游（如按天汇总）合法：粒度变了就够了，不必非要 join。"""
    ontology, _order, _item, order_ref, _item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(
        db,
        ontology.id,
        _payload(
            order_ref,
            _item_ref,
            token,
            upstream_refs=[order_ref],
            joins=[],
            grain="一行 = 一天的订单汇总",
            fields=[FieldSource(property="amount", from_ref=order_ref, from_column="amount")],
        ),
    )
    assert result.upstream_refs == [order_ref]


def test_field_source_must_be_a_declared_upstream(db, derived_seed):
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(DerivedObjectError) as err:
        create_derived_object(
            db,
            ontology.id,
            _payload(
                order_ref,
                item_ref,
                token,
                fields=[
                    FieldSource(property="x", from_ref="obj:elsewhere@ods", from_column="id")
                ],
            ),
        )
    assert "不在上游列表" in str(err.value)


def test_layer_is_pinned_so_machine_derivation_cannot_reset_it(db, derived_seed):
    """选的层要钉住：契约的机器推导按 table_role 给层（business_object → dim），
    不钉住的话下一次推导就把 dwd 改回 dim，表建到错的层里去。"""
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    contract = (
        db.query(MaterializationContract)
        .filter(
            MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
            MaterializationContract.target_id == result.object_type_id,
        )
        .one()
    )
    assert contract.target_layer == "dwd"
    assert "target_layer" in (contract.overridden_fields or "")


def test_pinned_layer_survives_a_contract_resync(db, derived_seed):
    """真正的验收不是「overridden_fields 里有这个名字」，而是**再推导一次层还在**。

    契约同步会按 table_role 重新推导（business_object → dim）；派生对象选的 dwd 若被
    改回去，表就建到错的层里，而界面上没有任何地方会提示这件事发生过。
    """
    from app.services.materialization_contract import MaterializationContractService

    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    MaterializationContractService().sync(db, ontology.id)

    contract = (
        db.query(MaterializationContract)
        .filter(
            MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
            MaterializationContract.target_id == result.object_type_id,
        )
        .one()
    )
    assert contract.target_layer == "dwd"
    # 物化本身要留着：派生对象的表就是靠物化建出来的（数据由清洗任务落）。
    assert contract.materialized is True


def test_ads_layer_is_rejected(db, derived_seed):
    """ADS 是口径的物化，归 BusinessLogic；给它建对象就是又造一份重复概念。"""
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(DerivedObjectError):
        create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token, layer="ads"))


def test_definition_reads_back_with_upstream_state(db, derived_seed):
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    view = get_definition(db, result.object_type_id)
    assert view is not None
    assert view.grain.startswith("一行 = ")
    assert [e.ref for e in view.upstreams] == [order_ref, item_ref]
    assert view.dangling_refs == []
    assert view.joins[0]["on"] == [{"left": "id", "right": "order_id"}]
    assert view.layer == "dwd"
    assert get_definition(db, _order.id) is None


def test_dangling_upstream_is_surfaced_not_hidden(db, derived_seed):
    """上游被删后定义仍在库里。少列一个上游会让定义看起来照样成立——必须显式报出来。"""
    ontology, _order, item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    item.deleted_by_user = True
    db.commit()

    view = get_definition(db, result.object_type_id)
    assert [e.ref for e in view.upstreams] == [order_ref]
    assert view.dangling_refs == [item_ref]


def test_derived_object_is_selectable_as_transform_target_only_with_multi_source(
    db, derived_seed
):
    """派生对象暂不能建单源清洗任务，但错误要说实话——而不是去读 ods.<对象名>。"""
    from app.agents.executors.transform import TransformExecutor

    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))
    obj = db.get(ObjectType, result.object_type_id)

    with pytest.raises(ValueError) as err:
        TransformExecutor._ods_source(db, ontology, obj, None)
    assert "派生对象" in str(err.value)


def test_api_creates_and_reads_definition(client, admin_headers, db, derived_seed):
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    body = {
        "name": f"order_wide_api_{token}",
        "display_name": "订单商品宽表",
        "grain": "一行 = 一张订单的一个商品行",
        "upstream_refs": [order_ref, item_ref],
        "joins": [
            {
                "left_ref": order_ref,
                "right_ref": item_ref,
                "on": [{"left": "id", "right": "order_id"}],
            }
        ],
        "fields": [{"property": "order_id", "from_ref": order_ref, "from_column": "id"}],
    }
    resp = client.post(
        f"/api/ontologies/{ontology.id}/derived-objects", json=body, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["layer"] == "dwd"

    detail = client.get(
        f"/api/object-types/{created['object_type_id']}/derived-definition",
        headers=admin_headers,
    )
    assert detail.status_code == 200
    assert [u["ref"] for u in detail.json()["upstreams"]] == [order_ref, item_ref]

    body["grain"] = ""
    body["name"] = f"{body['name']}_2"
    bad = client.post(
        f"/api/ontologies/{ontology.id}/derived-objects", json=body, headers=admin_headers
    )
    assert bad.status_code == 400
    assert "粒度" in bad.json()["detail"]


def test_api_404_for_non_derived_object(client, admin_headers, derived_seed):
    _ontology, order, _item, _order_ref, _item_ref = derived_seed
    resp = client.get(
        f"/api/object-types/{order.id}/derived-definition", headers=admin_headers
    )
    assert resp.status_code == 404


def test_derived_definition_row_is_stored_once(db, derived_seed):
    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))
    rows = (
        db.query(DerivedDefinition)
        .filter(DerivedDefinition.object_type_id == result.object_type_id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].ontology_id == ontology.id


def test_duplicate_name_is_refused_with_a_useful_message(db, derived_seed):
    ontology, order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    with pytest.raises(ValueError) as err:
        create_derived_object(
            db, ontology.id, _payload(order_ref, item_ref, token, name=order.name)
        )
    assert "已被" in str(err.value)


def test_derived_object_gets_no_fabricated_datahub_link(db, derived_seed):
    """派生对象在 DataHub 里没有数据集：不能给它编一个 hive URN 链接。

    编出来的后果是界面上摆着一个「在 DataHub 中查看表详情」，点开必然空白——
    与 ``source_table_of`` 拒绝原样回吐 ``manual:`` 引用是同一条戒律。
    """
    from app.api.deps import query as query_service

    ontology, _order, _item, order_ref, item_ref = derived_seed
    token = uuid.uuid4().hex[:6]
    result = create_derived_object(db, ontology.id, _payload(order_ref, item_ref, token))

    detail = query_service.get_object_type(db, result.object_type_id)
    assert detail.source_ref.startswith("derived:")
    assert detail.datahub_url is None
