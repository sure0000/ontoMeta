from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    DomainInput,
    FieldInput,
)
from app.services.evidence_builder import EvidenceBuilder
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    classify_object_role,
    FieldSignal,
)
from app.services.source_profile import (
    FrappeProfile,
    detect_source_profile,
)


def _std_cols():
    return [
        FieldInput(name="name", is_primary_key=False),
        FieldInput(name="creation"),
        FieldInput(name="modified"),
        FieldInput(name="modified_by"),
        FieldInput(name="owner"),
        FieldInput(name="docstatus"),
        FieldInput(name="idx"),
    ]


def _tab(name, extra_fields):
    return DatasetInput(
        urn=f"urn:li:dataset:{name}",
        name=name,
        display_name=name,
        fields=_std_cols() + extra_fields,
    )


def _frappe_bundle():
    return DataHubDomainBundle(
        domain=DomainInput(id="d1", name="ERP"),
        datasets=[
            _tab("tabCustomer", [FieldInput(name="customer_name"), FieldInput(name="territory")]),
            _tab("tabSales Order", [FieldInput(name="customer"), FieldInput(name="grand_total")]),
            # 第二张引用 Customer 的单据：与 Sales Order 一起让 Customer 落入 >=3 成员的
            # 业务环节（社区聚类），满足业务对象的环节归属必要条件。
            _tab("tabSales Invoice", [FieldInput(name="customer"), FieldInput(name="net_total")]),
            _tab(
                "tabSales Order Item",
                [
                    FieldInput(name="parent"),
                    FieldInput(name="parenttype"),
                    FieldInput(name="parentfield"),
                    FieldInput(name="item_code"),
                    FieldInput(name="qty"),
                ],
            ),
        ],
    )


def test_detect_frappe_profile():
    assert isinstance(detect_source_profile(_frappe_bundle()), FrappeProfile)


def test_non_frappe_uses_default_profile():
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d", name="x"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:orders",
                name="orders",
                fields=[FieldInput(name="order_id", is_primary_key=True)],
            )
        ],
    )
    profile = detect_source_profile(bundle)
    assert profile.name == "default"
    assert not isinstance(profile, FrappeProfile)


def test_frappe_name_is_primary_key():
    ds = _frappe_bundle().datasets[0]
    assert "name" in FrappeProfile().primary_key_names(ds)


def test_frappe_strips_standard_columns_but_keeps_name():
    p = FrappeProfile()
    assert p.is_system_column("creation")
    assert p.is_system_column("parenttype")
    assert not p.is_system_column("name")  # 主键，非噪声
    assert not p.is_system_column("customer_name")


def test_frappe_child_table_detection():
    p = FrappeProfile()
    datasets = {d.name: d for d in _frappe_bundle().datasets}
    assert p.is_child_table(datasets["tabSales Order Item"])
    assert not p.is_child_table(datasets["tabCustomer"])


def test_frappe_link_field_inferred_fk():
    bundle = _frappe_bundle()
    p = FrappeProfile()
    index = p.build_table_index(bundle)
    so = {d.name: d for d in bundle.datasets}["tabSales Order"]
    edges = p.inferred_fks(so, index)
    # customer 列命中 tabCustomer(DocType 名 Customer) → 推断外键
    targets = {e.target_table for e in edges}
    assert "tabCustomer" in targets


def _affix_bundle():
    # 目标 DocType + 一张带角色词缀引用列的明细表（贴近真实 Asset Capitalization）。
    V = "VARCHAR(140)"
    return DataHubDomainBundle(
        domain=DomainInput(id="d1", name="ERP"),
        datasets=[
            _tab("tabItem", [FieldInput(name="item_name", data_type=V)]),
            _tab("tabAccount", [FieldInput(name="account_name", data_type=V)]),
            _tab("tabWarehouse", [FieldInput(name="warehouse_name", data_type=V)]),
            _tab(
                "tabStock Entry Detail",
                [
                    FieldInput(name="item_code", data_type=V),  # → Item（后缀角色词）
                    FieldInput(name="fixed_asset_account", data_type=V),  # → Account
                    FieldInput(name="default_warehouse", data_type=V),  # → Warehouse
                    FieldInput(name="valuation_rate", data_type="DECIMAL(21, 9)"),
                ],
            ),
        ],
    )


def test_frappe_inferred_fk_resolves_role_affix_columns():
    # 整名不命中时，按边界词元取语义中心词：真实引用列多带角色词缀，
    # 只做整名精确会漏掉约一半。
    bundle = _affix_bundle()
    p = FrappeProfile()
    index = p.build_table_index(bundle)
    detail = {d.name: d for d in bundle.datasets}["tabStock Entry Detail"]
    edges = {e.column: e.target_table for e in p.inferred_fks(detail, index)}
    assert edges.get("item_code") == "tabItem"
    assert edges.get("fixed_asset_account") == "tabAccount"
    assert edges.get("default_warehouse") == "tabWarehouse"


def test_frappe_inferred_fk_skips_measure_column():
    # 度量列（DECIMAL）不得被当作 Link：valuation_rate 不能误命中任何 DocType。
    bundle = _affix_bundle()
    p = FrappeProfile()
    index = p.build_table_index(bundle)
    detail = {d.name: d for d in bundle.datasets}["tabStock Entry Detail"]
    cols = {e.column for e in p.inferred_fks(detail, index)}
    assert "valuation_rate" not in cols



def test_child_table_classified_as_bridge():
    # 明细/子表按建模原则判为业务关系(bridge) + 待复核，而非独立业务实体/数据表。
    result = classify_object_role(
        [FieldSignal(name="name", semantic_type="identifier", is_primary_key=True),
         FieldSignal(name="qty", semantic_type="amount")],
        is_child_table=True,
    )
    assert result.role == ROLE_BRIDGE
    assert result.needs_review
    assert "子表" in result.reason
    assert "关系" in result.reason


def test_glossary_exempts_child_table_downgrade():
    # 挂了人工业务术语 → 豁免子表改判（bridge），落回正常打分为业务对象。
    result = classify_object_role(
        [FieldSignal(name="name", semantic_type="identifier", is_primary_key=True)],
        is_child_table=True,
        glossary_terms=["订单明细"],
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_evidence_builder_frappe_end_to_end():
    evidence = EvidenceBuilder().build(_frappe_bundle())
    roles = {ot.display_name: ot.table_role for ot in evidence.object_types}
    # 子表初判为关系表(bridge)，但连不到两个业务对象（bundle 内无 tabItem 可解析）
    # → 落数据表(明细)：塌缩不成不代表它是个独立业务对象，子表锚点是源事实。
    assert roles["tabSales Order Item"] != ROLE_BRIDGE
    assert roles["tabSales Order Item"] != ROLE_BUSINESS_OBJECT
    soi = next(ot for ot in evidence.object_types if ot.display_name == "tabSales Order Item")
    assert "明细/子表" in (soi.role_reason or "")
    # Customer 被 Sales Order 通过 Link 字段引用（推断外键入度）→ 业务对象
    assert roles["tabCustomer"] == ROLE_BUSINESS_OBJECT
    # 推断出的 Link 外键关系存在
    fk_rels = [r for r in evidence.relations if r.structure_type == "foreign_key"]
    assert any("tabcustomer" in r.target_object.lower() for r in fk_rels)


def test_default_profile_preserves_declared_metadata():
    # 非 Frappe 源：声明式外键/主键行为不变（回归保护）。
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d", name="x"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:c",
                name="customer",
                fields=[FieldInput(name="cust_id", is_primary_key=True),
                        FieldInput(name="cust_name")],
            ),
            DatasetInput(
                urn="urn:li:dataset:o",
                name="orders",
                fields=[
                    FieldInput(name="order_id", is_primary_key=True),
                    FieldInput(name="cust_id", is_foreign_key=True,
                               foreign_key_target="customer.cust_id"),
                ],
            ),
            # 第二张引用 customer 的表：让 customer 落入 >=3 成员业务环节，满足环节归属
            # 必要条件（本用例意在验证声明式外键/主键透传，而非环节门槛）。
            DatasetInput(
                urn="urn:li:dataset:p",
                name="payments",
                fields=[
                    FieldInput(name="payment_id", is_primary_key=True),
                    FieldInput(name="cust_id", is_foreign_key=True,
                               foreign_key_target="customer.cust_id"),
                ],
            ),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    roles = {ot.candidate_name: ot.table_role for ot in evidence.object_types}
    assert roles["customer_entity"] == ROLE_BUSINESS_OBJECT
