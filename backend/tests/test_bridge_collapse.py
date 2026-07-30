"""桥表塌缩：端点选择纯函数 + EvidenceBuilder 生成期塌缩的端到端。"""

from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    DomainInput,
    FieldInput,
)
from app.services.bridge_collapse import select_bridge_endpoints
from app.services.evidence_builder import EvidenceBuilder, _infer_object_name
from app.services.object_classifier import ROLE_BRIDGE, ROLE_BUSINESS_OBJECT

BO = ROLE_BUSINESS_OBJECT


# --------------------------- 纯函数 select_bridge_endpoints ---------------------------


def test_junction_two_business_objects():
    """恰好两个业务对象引用 → 塌缩到这两个（列序：第一个作 source）。"""
    role = {"a": BO, "b": BO}
    assert select_bridge_endpoints(["a", "b"], role) == ("a", "b")


def test_multi_ref_source_is_first_target_is_top_master():
    """多引用事实表：source 取列序第一个业务对象（定义性主体），
    target 取其余里入度最高的主数据——而非让通用维度抢占两端。"""
    role = {"supplier": BO, "company": BO, "account": BO}
    degree = {"account": 10, "company": 5, "supplier": 1}
    assert (
        select_bridge_endpoints(["supplier", "company", "account"], role, degree)
        == ("supplier", "account")
    )


def test_fewer_than_two_business_objects_returns_none():
    """只引用 <2 个业务对象（典型：只连父表的明细/子表）→ 不物化。"""
    role = {"item": BO, "some_tech": "technical"}
    assert select_bridge_endpoints(["item", "some_tech"], role) is None
    assert select_bridge_endpoints([], role) is None


def test_non_business_object_refs_filtered_and_dedup_and_self():
    """非业务对象引用被剔除、重复去重、排除自指。"""
    role = {"a": BO, "b": BO, "tech": "technical", "br": "bridge"}
    got = select_bridge_endpoints(
        ["self", "tech", "a", "a", "br", "b"], role, self_name="self"
    )
    assert got == ("a", "b")


def test_target_tiebreak_by_row_count_then_order():
    """入度相同 → 行数少者(更像主数据)优先作 target。"""
    role = {"party": BO, "big": BO, "small": BO}
    degree = {"big": 3, "small": 3}
    row_count = {"big": 1000, "small": 10}
    assert (
        select_bridge_endpoints(["party", "big", "small"], role, degree, row_count)
        == ("party", "small")
    )


# --------------------------- EvidenceBuilder 端到端 ---------------------------


def _std_cols():
    return [
        FieldInput(name="name"),
        FieldInput(name="creation"),
        FieldInput(name="modified"),
        FieldInput(name="modified_by"),
        FieldInput(name="owner"),
        FieldInput(name="docstatus"),
        FieldInput(name="idx"),
    ]


def _tab(name, extra):
    return DatasetInput(
        urn=f"urn:li:dataset:{name}", name=name, display_name=name,
        fields=_std_cols() + extra,
    )


def _bundle_with_collapsible_bridge():
    """一个 Frappe 域：Customer/Company 为业务对象；Order Party 是引用二者的
    子表(bridge)，应被塌缩成 customer→company（mapping=order_party）。"""
    return DataHubDomainBundle(
        domain=DomainInput(id="d1", name="ERP"),
        datasets=[
            _tab("tabCustomer", [FieldInput(name="customer_name"), FieldInput(name="territory")]),
            _tab("tabCompany", [FieldInput(name="company_name"), FieldInput(name="abbr")]),
            _tab("tabSales Order", [FieldInput(name="customer"), FieldInput(name="company")]),
            _tab("tabSales Invoice", [FieldInput(name="customer"), FieldInput(name="company")]),
            _tab(
                "tabOrder Party",
                [
                    FieldInput(name="parent"),
                    FieldInput(name="parenttype"),
                    FieldInput(name="parentfield"),
                    FieldInput(name="customer"),
                    FieldInput(name="company"),
                ],
            ),
        ],
    )


def test_evidence_builder_collapses_bridge_with_mapping():
    evidence = EvidenceBuilder().build(_bundle_with_collapsible_bridge())
    roles = {o.candidate_name: o.table_role for o in evidence.object_types}
    party = _infer_object_name("tabOrder Party")
    assert roles.get(party) == ROLE_BRIDGE

    # 至少有一条塌缩关系（mapping 指向某桥表），且每条两端都是业务对象、类型为 bridge_table。
    collapsed = [r for r in evidence.relations if r.mapping_object]
    assert collapsed
    for rel in collapsed:
        assert roles.get(rel.source_object) == ROLE_BUSINESS_OBJECT
        assert roles.get(rel.target_object) == ROLE_BUSINESS_OBJECT
        assert rel.structure_type == "bridge_table"
        assert roles.get(rel.mapping_object) == ROLE_BRIDGE

    # 具体到 Order Party：塌缩为 {customer, company}。
    party_rel = [r for r in collapsed if r.mapping_object == party]
    assert len(party_rel) == 1
    assert {party_rel[0].source_object, party_rel[0].target_object} == {
        _infer_object_name("tabCustomer"),
        _infer_object_name("tabCompany"),
    }

    # rule 1：留存关系里没有任何一端是桥表（桥表只作 mapping，不作端点）。
    assert all(
        roles.get(r.source_object) != ROLE_BRIDGE and roles.get(r.target_object) != ROLE_BRIDGE
        for r in evidence.relations
    )


def test_pure_child_table_not_materialized():
    """只引用父表(+1 个 Link)的子表 → 选不出两个业务对象端点，不生成塌缩边。"""
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="ERP"),
        datasets=[
            _tab("tabItem", [FieldInput(name="item_name")]),
            _tab("tabSales Order", [FieldInput(name="item_code")]),
            _tab("tabSales Invoice", [FieldInput(name="item_code")]),
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
    evidence = EvidenceBuilder().build(bundle)
    soi = _infer_object_name("tabSales Order Item")
    assert not [r for r in evidence.relations if r.mapping_object == soi]
