"""多源加工：派生对象怎么从几张上游表算出来。

单源（1:1 清洗）与多源（派生对象）在**执行器里由 Spec 分岔**，不是由「回头读派生定义」
分岔——制品要能自证这次读了哪几张表。单源路径的 SQL 一个字节都不变，那条由
``test_agent_implementations`` 里的既有用例钉着。
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.drafters.transform import TransformDrafter
from app.agents.executors.transform import TransformExecutor
from app.agents.validation import validate_spec
from app.database import SessionLocal
from app.models import (
    DataSource,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyWarehouseDeployment,
    Property,
    WarehouseObjectProjection,
)
from app.services import dataset_catalog
from app.services.derived_object import (
    DerivedObjectInput,
    FieldSource,
    JoinCondition,
    UpstreamJoin,
    create_derived_object,
)

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp.{t},PROD)"


def _upstream(db, ontology, deployment, name, display, columns, *, ready=True):
    obj = ObjectType(
        ontology_id=ontology.id,
        name=name,
        display_name=display,
        source_ref=_URN.format(t=name),
        table_role="business_object",
    )
    db.add(obj)
    db.flush()
    for column, semantic in columns:
        db.add(
            Property(
                object_type_id=obj.id,
                name=column,
                display_name=column,
                data_type="varchar" if semantic == "category" else "bigint",
                semantic_type=semantic,
                # 键要真的必填：派生对象照抄上游的 required，没有键就退回整行去重，
                # 去重/滤空两条规则都失去意义。
                required=semantic == "identifier",
            )
        )
    db.add(
        WarehouseObjectProjection(
            deployment_id=deployment.id,
            object_type_id=obj.id,
            schema_status="ready",
            # ODS 槽的状态只由同步状态决定：empty = 表还没搬完，目录里就不是可用源。
            sync_status="ready" if ready else "empty",
            transform_status="not_required",
            ods_database="ods",
            ods_table=f"ods_erp_{name}",
            queryable=False,
        )
    )
    db.flush()
    return obj, dataset_catalog.dataset_ref(
        dataset_catalog.KIND_OBJECT, obj.id, dataset_catalog.SLOT_ODS
    )


@pytest.fixture
def wide_table(db):
    """两张已落地的 ODS 表 + 一个由它们派生出来的宽表对象。"""
    token = uuid.uuid4().hex[:8]
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:multi-{token}", name=f"multi-{token}"
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
    deployment = OntologyWarehouseDeployment(
        ontology_id=ontology.id,
        ontology_version=ontology.version,
        doris_datasource_id=doris.id,
        status="schema_ready",
    )
    db.add(deployment)
    db.flush()
    _order, order_ref = _upstream(
        db, ontology, deployment, f"sales_order_{token}", "销售订单",
        [("order_id", "identifier"), ("customer_code", "category")],
    )
    _customer, customer_ref = _upstream(
        db, ontology, deployment, f"customer_{token}", "客户",
        [("code", "category"), ("customer_name", "category")],
    )
    db.commit()

    result = create_derived_object(
        db,
        ontology.id,
        DerivedObjectInput(
            name=f"order_wide_{token}",
            display_name="订单客户宽表",
            grain="一行 = 一张订单（带客户名）",
            upstream_refs=[order_ref, customer_ref],
            joins=[
                UpstreamJoin(
                    left_ref=order_ref,
                    right_ref=customer_ref,
                    how="left",
                    on=[JoinCondition(left="customer_code", right="code")],
                )
            ],
            fields=[
                FieldSource(property="order_id", from_ref=order_ref, from_column="order_id"),
                FieldSource(
                    property="customer_name",
                    from_ref=customer_ref,
                    from_column="customer_name",
                ),
            ],
        ),
    )
    return {
        "ontology_id": ontology.id,
        "target": result.name,
        "order_ref": order_ref,
        "customer_ref": customer_ref,
        "order_table": f"ods.ods_erp_sales_order_{token}",
        "customer_table": f"ods.ods_erp_customer_{token}",
    }


def _spec(wide_table, **overrides):
    spec = TransformDrafter().draft(
        "生成订单客户宽表",
        {"ontology_id": wide_table["ontology_id"], "target_table": wide_table["target"]},
    )
    spec.update(overrides)
    return spec


def test_drafter_copies_the_derived_definition_into_the_spec(wide_table):
    """Spec 要自证这次读哪几张表：执行器不回头读派生定义。

    定义后来改了，一份已确认的制品不该静默换掉它读的表——这与「自检按 Spec 预演、
    不读契约」是同一条规矩。
    """
    spec = _spec(wide_table)
    assert spec["source_datasets"] == [wide_table["order_ref"], wide_table["customer_ref"]]
    assert spec["joins"][0]["on"] == [{"left": "customer_code", "right": "code"}]
    assert {f["property"] for f in spec["field_mapping"]} == {"order_id", "customer_name"}
    assert spec["grain"].startswith("一行 = ")


def test_multi_source_sql_joins_and_aliases_every_column(wide_table):
    spec = _spec(wide_table)
    out = TransformExecutor().execute(spec, {})
    sql = out["sql"]

    assert f"FROM `ods`.`{wide_table['order_table'].split('.')[1]}` t0" in sql
    assert (
        f"LEFT JOIN `ods`.`{wide_table['customer_table'].split('.')[1]}` t1 "
        "ON t0.`customer_code` = t1.`code`"
    ) in sql
    # 列必须带别名：没有 AS，外层看到的是源列名，与目标表对不上。
    assert "t0.`order_id` AS `order_id`" in sql
    assert "t1.`customer_name` AS `customer_name`" in sql
    assert sorted(out["source_tables"]) == sorted(
        [wide_table["order_table"], wide_table["customer_table"]]
    )
    assert sql.startswith("-- 粒度：")


def test_drop_null_uses_source_expressions_not_aliases(wide_table):
    """WHERE 在投影之前求值，用不了 SELECT 别名。

    写成 ```order_id` IS NOT NULL`` 的话：好的情况是报未知列，坏的情况是两张上游都有
    同名列而静默解析到错的那张。
    """
    spec = _spec(wide_table, cleansing_rules=[{"rule": "drop_null"}])
    out = TransformExecutor().execute(spec, {})
    assert out["applied_rules"] == ["drop_null"]
    assert "WHERE t0.`order_id` IS NOT NULL" in out["sql"]


def test_deduplicate_over_multi_source_ranks_on_target_columns(wide_table):
    """去重包在投影外层，那里的列已经是目标列名——不该再带表别名。"""
    spec = _spec(wide_table, cleansing_rules=[{"rule": "deduplicate"}])
    out = TransformExecutor().execute(spec, {})
    assert "ROW_NUMBER() OVER (PARTITION BY `order_id`" in out["sql"]
    assert "PARTITION BY t0." not in out["sql"]


def test_unmapped_target_column_is_refused(wide_table):
    """目标表多出一列而派生定义说不出它从哪来：宁可拒绝，也不赌某个上游恰好有这个列。"""
    with SessionLocal() as db:
        obj = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == wide_table["ontology_id"],
                ObjectType.name == wide_table["target"],
            )
            .one()
        )
        db.add(
            Property(
                object_type_id=obj.id,
                name="amount",
                display_name="金额",
                data_type="decimal",
                semantic_type="amount",
            )
        )
        db.commit()
    spec = _spec(wide_table)
    with pytest.raises(ValueError) as err:
        TransformExecutor().execute(spec, {})
    assert "amount" in str(err.value)


def test_join_without_condition_is_refused(wide_table):
    """没有连接条件的 join 是一次笛卡尔积：不报错，只把行数乘起来。"""
    spec = _spec(wide_table)
    spec["joins"] = [
        {
            "left_ref": wide_table["order_ref"],
            "right_ref": wide_table["customer_ref"],
            "how": "inner",
            "on": [],
        }
    ]
    with pytest.raises(ValueError) as err:
        TransformExecutor().execute(spec, {})
    assert "笛卡尔积" in str(err.value)


def test_unknown_upstream_ref_is_refused(wide_table):
    spec = _spec(wide_table)
    spec["source_datasets"] = [wide_table["order_ref"], "obj:gone@ods"]
    with pytest.raises(ValueError) as err:
        TransformExecutor().execute(spec, {})
    assert "obj:gone@ods" in str(err.value)


def test_upstream_that_has_not_landed_is_refused(db, wide_table):
    """上游还没搬完就 join：SQL 生成得出来，跑到 Doris 才报表不存在。"""
    entry = dataset_catalog.resolve_dataset_ref(db, wide_table["customer_ref"])
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(WarehouseObjectProjection.object_type_id == entry.entity_id)
        .one()
    )
    projection.sync_status = "empty"
    db.commit()

    spec = _spec(wide_table)
    with pytest.raises(ValueError) as err:
        TransformExecutor().execute(spec, {})
    assert "尚未就绪" in str(err.value)


def test_gate_blocks_a_source_that_is_not_ready(db, wide_table):
    """闸门与执行器同源：不会出现「确认放行、执行拒绝」。"""
    spec = _spec(wide_table)
    assert [
        i for i in validate_spec(
            db, kind="transform", spec=spec, ontology_id=wide_table["ontology_id"]
        )
        if i.code.startswith("transform_source")
    ] == []

    entry = dataset_catalog.resolve_dataset_ref(db, wide_table["order_ref"])
    projection = (
        db.query(WarehouseObjectProjection)
        .filter(WarehouseObjectProjection.object_type_id == entry.entity_id)
        .one()
    )
    projection.sync_status = "empty"
    db.commit()

    codes = {
        i.code
        for i in validate_spec(
            db, kind="transform", spec=spec, ontology_id=wide_table["ontology_id"]
        )
    }
    assert "transform_source_not_ready" in codes


def test_gate_flags_a_field_mapped_to_an_undeclared_upstream(db, wide_table):
    spec = _spec(wide_table)
    spec["field_mapping"] = list(spec["field_mapping"]) + [
        {"property": "x", "from_ref": "obj:elsewhere@ods", "from_column": "x"}
    ]
    codes = {
        i.code
        for i in validate_spec(
            db, kind="transform", spec=spec, ontology_id=wide_table["ontology_id"]
        )
    }
    assert "transform_field_source_unknown" in codes


def test_drafter_refuses_a_derived_target_with_a_dangling_upstream(db, wide_table):
    """上游被删了还去建加工任务，等于确认一份跑不通的作业。"""
    entry = dataset_catalog.resolve_dataset_ref(db, wide_table["customer_ref"])
    obj = db.get(ObjectType, entry.entity_id)
    obj.deleted_by_user = True
    db.commit()

    with pytest.raises(ValueError) as err:
        TransformDrafter().draft(
            "生成订单客户宽表",
            {"ontology_id": wide_table["ontology_id"], "target_table": wide_table["target"]},
        )
    assert "失效" in str(err.value)
