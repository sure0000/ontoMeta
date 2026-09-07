"""代码包关联键 → 本体证据的接线测试。

背景：``LineagePackageEdge.join_key`` 采集了很久，但 ``evidence_builder`` 从没读过它——
系统里置信度最高的关联证据（真实执行过的 JOIN）一直烂在库里。这组用例钉住那条新接的路。

钉住的行为：
1. ``a.b.c = d.e`` 的解析按**最后一个点**切列名（表名可能带库名前缀）；
2. 同表两列相等是过滤条件，不是关联；
3. 无向去重：``a.x = b.y`` 与 ``b.y = a.x`` 是同一条证据；
4. 两端表名对不上 DataHub、或列名不在 schema 里 → 丢掉，不猜；
5. 观察到的 JOIN 参与关系证据**与拓扑**（入度/环节聚类），置信度 0.75，描述要与
   源画像推断区分得开；
6. 基数按 distinct/rows 算，不是硬编码的 many_to_one；
7. evidence 缓存 fingerprint 含关联键摘要——否则扫了新包也不会生效。
"""

from __future__ import annotations

import uuid

import pytest

from app.database import SessionLocal
from app.models import DomainContext, LineagePackage, LineagePackageEdge
from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
from app.services import observed_joins
from app.services.evidence_builder import EvidenceBuilder
from app.services.observed_joins import ObservedJoin, parse_join_key


def _dataset(name, fields, *, rows=1000):
    return DatasetInput(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:mysql,erp_db.{name},PROD)",
        name=f"erp_db.{name}",
        row_count=rows,
        fields=fields,
    )


def _field(name, *, distinct=100, samples=("1", "2")):
    return FieldInput(
        name=name,
        data_type="VARCHAR(64)",
        unique_count=distinct,
        sample_values=list(samples),
    )


def _bundle(datasets):
    return DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:t", name="t"),
        datasets=datasets,
    )


# --------------------------------------------------------------------------- 解析


def test_parse_splits_column_at_the_last_dot():
    assert parse_join_key("erp_db.orders.customer_id = erp_db.customers.id") == (
        "erp_db.orders",
        "customer_id",
        "erp_db.customers",
        "id",
    )


def test_parse_rejects_same_table_and_malformed():
    assert parse_join_key("t.a = t.b") is None       # 同表两列 = 过滤条件
    assert parse_join_key("orders = customers") is None  # 没有列名
    assert parse_join_key("a.x > b.y") is None
    assert parse_join_key(None) is None


# --------------------------------------------------------------------------- 落库读取


@pytest.fixture
def domain_with_joins():
    db = SessionLocal()
    domain = DomainContext(
        id=str(uuid.uuid4()), name=f"t-{uuid.uuid4().hex[:6]}", datahub_domain_id="urn:li:domain:t"
    )
    db.add(domain)
    package = LineagePackage(domain_context_id=domain.id, name="p.zip", kind="scan")
    db.add(package)
    db.flush()
    for left, right in (
        ("erp_db.orders.customer_id", "erp_db.customers.id"),
        # 反向重复：同一条证据，应被去重
        ("erp_db.customers.id", "erp_db.orders.customer_id"),
    ):
        db.add(
            LineagePackageEdge(
                package_id=package.id,
                source_table=left.rsplit(".", 1)[0],
                target_table=right.rsplit(".", 1)[0],
                join_key=f"{left} = {right}",
                source_file="a.sql",
                state="ok",
            )
        )
    db.commit()
    yield db, domain.id
    db.query(LineagePackageEdge).filter(
        LineagePackageEdge.package_id == package.id
    ).delete()
    db.query(LineagePackage).filter(LineagePackage.id == package.id).delete()
    db.query(DomainContext).filter(DomainContext.id == domain.id).delete()
    db.commit()
    db.close()


def test_load_dedupes_reverse_direction(domain_with_joins):
    db, domain_id = domain_with_joins
    joins = observed_joins.load_for_domain(db, domain_id)
    assert len(joins) == 1


def test_fingerprint_changes_with_join_keys(domain_with_joins):
    """缓存指纹必须跟着关联键变——否则扫了新包，生成草稿还是命中旧证据。"""
    db, domain_id = domain_with_joins
    with_joins = observed_joins.digest(db, domain_id)
    assert with_joins != "nojoin"
    assert observed_joins.digest(db, str(uuid.uuid4())) == "nojoin"


# --------------------------------------------------------------------------- 合流（单元）


def _orders_bundle():
    """两表最小件，只用来验合流本身（不过分类器）。"""
    return _bundle(
        [
            _dataset("orders", [_field("customer_id", distinct=700)], rows=1571),
            _dataset("customers", [_field("id", distinct=1000)], rows=1000),
        ]
    )


def _join(
    left_table="erp_db.orders",
    left_column="customer_id",
    right_table="erp_db.customers",
    right_column="id",
):
    return ObservedJoin(
        left_table=left_table,
        left_column=left_column,
        right_table=right_table,
        right_column=right_column,
        source_file="a.sql",
    )


def _merge(bundle, joins):
    edges: dict[str, list] = {}
    EvidenceBuilder._merge_observed_joins(bundle, joins, edges)
    return {table: rows for table, rows in edges.items() if rows}


def test_merge_orients_reference_by_distinctness():
    """近唯一的一端是被引用的主数据端，与 JOIN 写法的左右无关。"""
    forward = _merge(_orders_bundle(), [_join()])
    backward = _merge(
        _orders_bundle(),
        [_join("erp_db.customers", "id", "erp_db.orders", "customer_id")],
    )

    assert list(forward) == ["erp_db.orders"] == list(backward)
    for merged in (forward, backward):
        edge = merged["erp_db.orders"][0]
        assert (edge.column, edge.target_table, edge.target_column) == (
            "customer_id",
            "erp_db.customers",
            "id",
        )
        assert edge.origin == "observed_join"
        assert edge.confidence == 0.75


def test_merge_drops_unresolvable_table_or_column():
    """对不上就丢，不猜——与 lineage_inventory.resolve 同一条口径。"""
    assert _merge(_orders_bundle(), [_join(right_table="erp_db.suppliers")]) == {}
    assert _merge(_orders_bundle(), [_join(left_column="not_a_column")]) == {}


def test_merge_is_idempotent_for_the_same_edge():
    merged = _merge(_orders_bundle(), [_join(), _join()])
    assert len(merged["erp_db.orders"]) == 1


# --------------------------------------------------------------------------- 接进证据（端到端）


def _erp_bundle():
    """三张表连成一个业务环节——关系要留得住，两端都得判成业务对象
    （``_refine_business_relations`` 的 rule1：非业务对象的关系会被剥掉）。"""
    return _bundle(
        [
            _dataset(
                "orders",
                [
                    _field("order_no", distinct=1571, samples=("SO0001", "SO0002")),
                    _field("customer_id", distinct=700),
                    _field("status", distinct=5, samples=("待付款", "已发货")),
                    _field("remark", distinct=900, samples=("加急", "客户自提")),
                ],
                rows=1571,
            ),
            _dataset(
                "customers",
                [
                    _field("id", distinct=1000),
                    _field("name", distinct=990, samples=("张三", "李四")),
                    _field("city", distinct=40, samples=("上海", "杭州")),
                ],
                rows=1000,
            ),
            _dataset(
                "order_items",
                [
                    _field("order_no", distinct=1571, samples=("SO0001", "SO0002")),
                    _field("sku", distinct=300, samples=("A-01", "B-02")),
                    _field("qty", distinct=20, samples=("1", "2")),
                ],
                rows=5000,
            ),
        ]
    )


def _erp_joins():
    return [
        _join(),
        _join("erp_db.order_items", "order_no", "erp_db.orders", "order_no"),
    ]


def _observed_relations(evidence):
    return [
        r
        for r in evidence.relations
        if r.structure_type == "foreign_key" and "JOIN" in (r.description or "")
    ]


def test_observed_join_becomes_a_foreign_key_relation_in_evidence():
    evidence = EvidenceBuilder().build(_erp_bundle(), observed_joins=_erp_joins())

    rels = _observed_relations(evidence)
    assert len(rels) == 2
    rel = next(r for r in rels if "customer" in (r.description or ""))
    assert rel.confidence == 0.75
    assert "真实执行过的 JOIN" in rel.description
    assert "customer_id = id" in rel.description


def test_observed_join_cardinality_is_computed_not_hardcoded():
    """orders.customer_id 700/1571 → customers.id 1000/1000 是 many_to_one；
    两端都不唯一时必须给 many_to_many——硬编码 many_to_one 的实现过不了这条。"""
    evidence = EvidenceBuilder().build(_erp_bundle(), observed_joins=_erp_joins())
    by_target = {r.target_object: r.cardinality for r in _observed_relations(evidence)}

    assert by_target["erp_db_customers"] == "many_to_one"

    loose = _bundle(
        [
            _dataset("aj_bl_zl", [_field("bl_bh", distinct=700)], rows=1571),
            _dataset("aj_cyry_ql", [_field("ry_bh", distinct=800)], rows=1153),
        ]
    )
    join = _join("erp_db.aj_bl_zl", "bl_bh", "erp_db.aj_cyry_ql", "ry_bh")
    merged = _merge(loose, [join])
    edge = next(iter(merged.values()))[0]
    from app.services.evidence_builder import _fk_cardinality

    source = next(d for d in loose.datasets if d.name.endswith("aj_bl_zl"))
    target = next(d for d in loose.datasets if d.name.endswith("aj_cyry_ql"))
    assert _fk_cardinality(source, edge, target) == "many_to_many"


def test_observed_join_feeds_topology_not_just_relations():
    """接进拓扑才是重点：孤岛表因此凑出业务环节，不再被判成 data_table。"""
    bundle = _erp_bundle()

    def roles(evidence):
        return {o.candidate_name: o.table_role for o in evidence.object_types}

    without = roles(EvidenceBuilder().build(bundle))
    with_joins = roles(EvidenceBuilder().build(bundle, observed_joins=_erp_joins()))

    assert set(without.values()) == {"data_table"}
    assert with_joins["erp_db_customers"] == "business_object"


def test_declared_foreign_key_wins_over_observed_join():
    """列上已有声明式外键时，观察到的 JOIN 不再为它另出一条关系。"""
    bundle = _erp_bundle()
    orders = next(d for d in bundle.datasets if d.name.endswith("orders"))
    customer_id = next(f for f in orders.fields if f.name == "customer_id")
    customer_id.is_foreign_key = True
    customer_id.foreign_key_target = "erp_db.customers.id"

    evidence = EvidenceBuilder().build(bundle, observed_joins=_erp_joins())

    assert not [
        r for r in _observed_relations(evidence) if "customer_id" in (r.description or "")
    ]
    # 另一条没有声明式外键的 JOIN 不受影响，仍然出关系。
    assert [r for r in _observed_relations(evidence) if "order_no" in (r.description or "")]
