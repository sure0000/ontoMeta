"""重跑本体不得累积：同一份草稿合并 N 次，库里的东西必须一模一样。

「重新生成会产生大量重复」的验收标准就是这一条。散在各处的单点修（按字段名去重、
陈旧属性清理、撞名消歧）各自都有用例，但没有一处钉住**整体**：只要还有任何一条
路径按不稳定的键去 upsert，重跑就还会长。这里用同一份输入连跑三次，直接量计数。
"""

from __future__ import annotations

import uuid

import pytest

from app.database import Base, SessionLocal, engine
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.schemas import (
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    OntologyDraftOutput,
)
from app.services.ontology_merge import OntologyMergeService


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def ontology(db):
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:idem-{uuid.uuid4().hex[:8]}", name="幂等域"
    )
    db.add(domain)
    db.flush()
    row = Ontology(
        domain_context_id=domain.id,
        status=OntologyStatus.DRAFT.value,
        generated_by="llm",
    )
    db.add(row)
    db.flush()
    return row


def _urn(table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:mysql,erp.{table},PROD)"


def _draft() -> OntologyDraftOutput:
    return OntologyDraftOutput(
        object_types=[
            DraftObjectType(
                name="customer",
                display_name="客户",
                source_ref=_urn("customer"),
                confidence=0.6,
            ),
            DraftObjectType(
                name="order",
                display_name="订单",
                source_ref=_urn("order"),
                confidence=0.6,
            ),
        ],
        properties=[
            DraftProperty(
                object_type_name="customer",
                name="customer_id",
                display_name="客户ID",
                data_type="bigint",
                source_field_ref=f"{_urn('customer')}:customer_id",
            ),
            DraftProperty(
                object_type_name="customer",
                name="name",
                display_name="名称",
                data_type="varchar",
                source_field_ref=f"{_urn('customer')}:name",
            ),
            DraftProperty(
                object_type_name="order",
                name="order_id",
                display_name="订单ID",
                data_type="bigint",
                source_field_ref=f"{_urn('order')}:order_id",
            ),
        ],
        relation_types=[
            DraftRelationType(
                name="customer_order",
                display_name="客户下单",
                source_object_type_name="customer",
                target_object_type_name="order",
                cardinality="one_to_many",
                structure_type="foreign_key",
            )
        ],
    )


def _identities(db, ontology_id: str) -> set[str]:
    """对象 id 集合。计数不变掩盖得了「删了再建」，id 掩盖不了——而挂在
    object_type_id 上的落点登记、逻辑绑定全靠这个 id 稳定。"""
    return {
        row[0]
        for row in db.query(ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    }


def _counts(db, ontology_id: str) -> tuple[int, int, int]:
    object_ids = [
        row[0]
        for row in db.query(ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    ]
    props = (
        db.query(Property).filter(Property.object_type_id.in_(object_ids)).count()
        if object_ids
        else 0
    )
    rels = (
        db.query(RelationType)
        .filter(RelationType.ontology_id == ontology_id)
        .count()
    )
    return len(object_ids), props, rels


def _merge(db, ontology_id: str, draft: OntologyDraftOutput, gen: str) -> None:
    OntologyMergeService().merge_full(db, ontology_id, draft, gen)
    db.commit()


def test_repeated_merge_of_identical_draft_grows_nothing(db, ontology):
    """同一份草稿连跑三次：对象/属性/关系计数必须一次都不涨。"""
    _merge(db, ontology.id, _draft(), "gen1")
    first = _counts(db, ontology.id)
    first_ids = _identities(db, ontology.id)
    assert first == (2, 3, 1)

    _merge(db, ontology.id, _draft(), "gen2")
    _merge(db, ontology.id, _draft(), "gen3")

    assert _counts(db, ontology.id) == first
    assert _identities(db, ontology.id) == first_ids


def test_repeated_merge_keeps_source_field_ref_drift_from_duplicating(db, ontology):
    """``source_field_ref`` 漂移不得让同名字段再插一份。

    这是历史上属性 ×3 累积的真因：曾用 ``source_field_ref or name`` 作键，而它随源
    URN/命名在重跑之间变化。字段名才是对象内的稳定唯一标识。
    """
    _merge(db, ontology.id, _draft(), "gen1")
    before = _counts(db, ontology.id)

    drifted = _draft()
    for prop in drifted.properties:
        prop.source_field_ref = f"{prop.source_field_ref}#v2"
    _merge(db, ontology.id, drifted, "gen2")

    assert _counts(db, ontology.id) == before


def test_repeated_merge_survives_llm_renaming_the_same_table(db, ontology):
    """LLM 换个业务名，同一张源表仍是同一个对象——合并按 source_ref，不按 name。"""
    _merge(db, ontology.id, _draft(), "gen1")
    before = _counts(db, ontology.id)
    before_ids = _identities(db, ontology.id)

    renamed = _draft()
    renamed.object_types[0].name = "client"
    renamed.object_types[0].display_name = "客户主体"
    for prop in renamed.properties:
        if prop.object_type_name == "customer":
            prop.object_type_name = "client"
    renamed.relation_types[0].source_object_type_name = "client"
    _merge(db, ontology.id, renamed, "gen2")

    assert _counts(db, ontology.id) == before
    assert _identities(db, ontology.id) == before_ids
    names = {o.name for o in db.query(ObjectType).filter_by(ontology_id=ontology.id)}
    assert "client" in names and "customer" not in names


def test_repeated_merge_with_colliding_names_grows_nothing(db, ontology):
    """两张不同源表被 LLM 压成同名：消歧后重跑仍不得再长。

    撞名消歧会给其中一个改名，下一轮机器还是产出原来那个名字——若合并按 name 认人，
    就会每轮新建一个，重名与重复一起累积。
    """
    draft = _draft()
    draft.object_types[1].name = "customer"  # 与第一个撞名
    draft.properties[2].object_type_name = "customer"
    draft.relation_types = []

    _merge(db, ontology.id, draft, "gen1")
    first = _counts(db, ontology.id)
    first_ids = _identities(db, ontology.id)
    assert first[0] == 2  # 两个不同源表 → 两个对象（改名消歧，不是删一个）

    _merge(db, ontology.id, draft, "gen2")
    _merge(db, ontology.id, draft, "gen3")

    assert _counts(db, ontology.id) == first
    assert _identities(db, ontology.id) == first_ids
    names = [o.name for o in db.query(ObjectType).filter_by(ontology_id=ontology.id)]
    assert len(names) == len(set(names)), f"对象标识重名：{names}"


def test_repeated_merge_without_source_ref_grows_nothing(db, ontology):
    """没有 source_ref 的对象也不能每轮新建一份。

    合并认的是 source_ref；一个连 source_ref 都没有的机器对象，如果没有别的稳定键
    兜住，就会在每次重跑时被当成全新对象插入——库里于是长出一串同源的孪生对象。
    """
    draft = OntologyDraftOutput(
        object_types=[
            DraftObjectType(name="floating", display_name="无源对象", source_ref=None)
        ],
        properties=[
            DraftProperty(
                object_type_name="floating", name="code", display_name="编码"
            )
        ],
    )
    _merge(db, ontology.id, draft, "gen1")
    first = _counts(db, ontology.id)
    first_ids = _identities(db, ontology.id)
    assert first[0] == 1

    _merge(db, ontology.id, draft, "gen2")
    _merge(db, ontology.id, draft, "gen3")

    assert _counts(db, ontology.id) == first
    # 计数不变还不够：曾经是「每轮删了再建」，id 每次都换人，
    # 挂在 object_type_id 上的落点登记与逻辑绑定随之断链。
    assert _identities(db, ontology.id) == first_ids


# --- 落地对象不得被重跑抹掉 ---------------------------------------------------


@pytest.fixture
def landed_object(db, ontology):
    """一个已物化+已同步的对象：数仓里有实表、有数据，登记行按 object_type_id 挂着。"""
    from app.models import (
        DataSource,
        IngestionContract,
        OntologyWarehouseDeployment,
        WarehouseObjectProjection,
    )

    _merge(
        db,
        ontology.id,
        OntologyDraftOutput(
            object_types=[
                DraftObjectType(
                    name="customer", display_name="客户", source_ref=_urn("customer")
                )
            ]
        ),
        "gen1",
    )
    obj = db.query(ObjectType).filter_by(ontology_id=ontology.id).one()
    doris = DataSource(
        name=f"Doris-{uuid.uuid4().hex[:6]}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.flush()
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
            ods_database="ods",
            ods_table="ods_erp_customer",
            schema_status="ready",
            sync_status="ready",
        )
    )
    db.add(
        IngestionContract(
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            object_type_id=obj.id,
            source_datasource_id=doris.id,
            source_physical_table="erp.customer",
            doris_datasource_id=doris.id,
            target_ods_database="ods",
            target_ods_table="ods_erp_customer",
            status="ready",
        )
    )
    db.commit()
    return obj


def test_landed_object_is_deprecated_not_deleted_when_upstream_vanishes(
    db, ontology, landed_object
):
    """上游那张源表从元数据里消失时，已落地的对象只降级、绝不硬删。

    硬删会让 warehouse_object_projections / ingestion_contracts 变成指向不存在
    object_type_id 的孤儿：Doris 里那张表还在、还有数据，本体里却没有任何东西对应它
    ——「任务建的表在建模里看不见」原样重现，而且这次连登记都找不回来。
    """
    object_id = landed_object.id

    _merge(db, ontology.id, OntologyDraftOutput(object_types=[]), "gen2")

    survivor = db.get(ObjectType, object_id)
    assert survivor is not None, "已落地对象被硬删了，落点登记成了孤儿"
    assert survivor.upstream_removed is True
    assert survivor.status == "deprecated"


def test_regeneration_leaves_no_orphaned_landing_rows(db, ontology, landed_object):
    """重跑后不得留下指向不存在对象的登记行。

    比「落点还读得出来」更严：登记表按 object_type_id 查，孤儿行照样查得到，只是
    再也没有对象承载它。这里直接查全局不变式——每条登记行都必须找得到它的对象。
    """
    from app.models import IngestionContract, WarehouseObjectProjection

    _merge(db, ontology.id, OntologyDraftOutput(object_types=[]), "gen2")

    live_ids = _identities(db, ontology.id)
    projection_owners = {
        row[0]
        for row in db.query(WarehouseObjectProjection.object_type_id).all()
        if row[0] in {landed_object.id}
    }
    contract_owners = {
        row[0]
        for row in db.query(IngestionContract.object_type_id).all()
        if row[0] in {landed_object.id}
    }
    assert projection_owners <= live_ids, "物化落点登记指向了已不存在的对象"
    assert contract_owners <= live_ids, "接入契约指向了已不存在的对象"


def test_unlanded_object_is_still_hard_deleted(db, ontology):
    """没落地过的机器对象上游消失时照旧硬删——保护范围不能扩大到「什么都不删」。"""
    _merge(
        db,
        ontology.id,
        OntologyDraftOutput(
            object_types=[
                DraftObjectType(
                    name="scratch", display_name="临时表", source_ref=_urn("scratch")
                )
            ]
        ),
        "gen1",
    )
    obj_id = db.query(ObjectType).filter_by(ontology_id=ontology.id).one().id

    _merge(db, ontology.id, OntologyDraftOutput(object_types=[]), "gen2")

    assert db.get(ObjectType, obj_id) is None


# --- 存量孤儿：体检与接回 -----------------------------------------------------


def test_orphan_scan_reattaches_projection_to_the_remodeled_object(db, ontology):
    """对象被删后又重新建模：孤儿落点应按 ODS 表名接回新对象。

    ODS 表名是确定性的（``ods_{数据域}_{原表}``），所以「同一张表」可以从名字反推，
    不必猜也不会接错。接回去之后落点立刻在工作区重新显示，数据一行不用重搬。
    """
    from app.models import DataSource, OntologyWarehouseDeployment, WarehouseObjectProjection
    from app.services.object_landing import object_landing
    from app.services.ods_naming import target_ods_table_name
    from scripts.check_landing_orphans import scan

    _merge(
        db,
        ontology.id,
        OntologyDraftOutput(
            object_types=[
                DraftObjectType(
                    name="customer", display_name="客户", source_ref=_urn("customer")
                )
            ]
        ),
        "gen1",
    )
    obj = db.query(ObjectType).filter_by(ontology_id=ontology.id).one()
    ods_table = target_ods_table_name(db, ontology.id, obj)

    doris = DataSource(
        name=f"Doris-{uuid.uuid4().hex[:6]}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.flush()
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    projection = WarehouseObjectProjection(
        deployment_id=deployment.id,
        # 模拟存量孤儿：指向一个已不存在的旧对象 id。
        object_type_id=f"gone-{uuid.uuid4().hex}",
        ods_database="ods",
        ods_table=ods_table,
        schema_status="ready",
        sync_status="ready",
    )
    db.add(projection)
    db.commit()

    assert object_landing(db, obj.id) is None, "前置：新对象此时还看不到落点"

    stats = scan(db, reattach=True, purge=False)
    db.commit()

    assert stats["projection_orphans"] == 1
    assert stats["projection_reattached"] == 1
    landing = object_landing(db, obj.id)
    assert landing is not None and landing.ods_table == f"ods.{ods_table}"


def test_orphan_scan_reports_without_touching_anything_by_default(db, ontology):
    """默认只报告：接不回去的孤儿不会被悄悄删掉。"""
    from app.models import DataSource, OntologyWarehouseDeployment, WarehouseObjectProjection
    from scripts.check_landing_orphans import scan

    doris = DataSource(
        name=f"Doris-{uuid.uuid4().hex[:6]}",
        kind="doris",
        purpose="warehouse",
        enabled=True,
        dsn_secret_ref="mysql+pymysql://reader@fe:9030",
    )
    db.add(doris)
    db.flush()
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    projection = WarehouseObjectProjection(
        deployment_id=deployment.id,
        object_type_id=f"gone-{uuid.uuid4().hex}",
        ods_database="ods",
        ods_table="ods_nowhere_table",
        schema_status="ready",
    )
    db.add(projection)
    db.commit()
    projection_id = projection.id

    stats = scan(db, reattach=True, purge=False)
    db.commit()

    assert stats["projection_orphans"] == 1
    assert stats["projection_reattached"] == 0
    assert db.get(WarehouseObjectProjection, projection_id) is not None
