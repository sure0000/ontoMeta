"""业务环节归属门槛：只有隶属于某个业务环节（关系图 >=3 成员聚类）的表才可能是业务
对象。孤立表/孤对即便有干净的单列业务主键 + 描述属性，也不作为业务对象——这是对
「内在业务身份」豁免的收紧。人工术语(glossary)仍豁免。
"""

from __future__ import annotations

from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
from app.services.evidence_builder import EvidenceBuilder
from app.services.object_classifier import (
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    FieldSignal,
    classify_object_role,
)


def _f(name, sem, pk=False, fk=False):
    return FieldSignal(name=name, semantic_type=sem, is_primary_key=pk, is_foreign_key=fk)


def _business_shaped():
    """单列业务主键 + 描述属性 + 业务命名 → 无环节信号时判业务对象（且有内在身份）。"""
    return [
        _f("cust_id", "identifier", pk=True),
        _f("cust_name", "attribute"),
        _f("level", "category"),
    ]


def test_segment_size_below_threshold_demotes_to_data_table():
    for size in (1, 2):
        result = classify_object_role(
            _business_shaped(), has_business_naming=True, segment_size=size
        )
        assert result.role == ROLE_DATA_TABLE, f"segment_size={size}"
        assert result.needs_review is True
        assert "业务环节" in result.reason
        assert result.signals.get("segment_size") == size


def test_segment_size_at_threshold_stays_business_object():
    result = classify_object_role(
        _business_shaped(), has_business_naming=True, segment_size=3
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert result.signals.get("segment_size") == 3


def test_glossary_exempts_isolated_table():
    result = classify_object_role(
        _business_shaped(),
        has_business_naming=True,
        segment_size=1,
        glossary_terms=["客户"],
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_segment_size_none_is_backward_compatible_noop():
    # 未提供环节信号（如直接单测/旧调用方）→ 不降级。
    result = classify_object_role(_business_shaped(), has_business_naming=True)
    assert result.role == ROLE_BUSINESS_OBJECT
    assert "segment_size" not in result.signals


def test_evidence_builder_demotes_isolated_entity_keeps_connected_segment():
    # customer ← order1/order2 构成 3 成员环节；region 孤立（无任何关系）。
    customer = DatasetInput(
        urn="urn:li:dataset:customer",
        name="customer",
        display_name="客户",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="level", data_type="string"),
        ],
    )
    orders = [
        DatasetInput(
            urn=f"urn:li:dataset:order{i}",
            name=f"order{i}",
            display_name=f"订单{i}",
            fields=[
                FieldInput(name="id", data_type="string", is_primary_key=True),
                FieldInput(name="note", data_type="string"),
                FieldInput(
                    name="customer_id",
                    data_type="string",
                    is_foreign_key=True,
                    foreign_key_target="customer.id",
                ),
            ],
        )
        for i in range(1, 3)
    ]
    region = DatasetInput(
        urn="urn:li:dataset:region",
        name="region",
        display_name="地区",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="code", data_type="string"),
        ],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:test", name="测试域"),
        datasets=[customer, *orders, region],
        lineages=[],
    )
    evidence = EvidenceBuilder().build(bundle)
    by_urn = {p.source_dataset_urn: p for p in evidence.object_types}

    assert by_urn["urn:li:dataset:customer"].table_role == ROLE_BUSINESS_OBJECT
    assert by_urn["urn:li:dataset:customer"].role_signals["signals"]["segment_size"] >= 3

    region_pack = by_urn["urn:li:dataset:region"]
    assert region_pack.table_role == ROLE_DATA_TABLE
    assert "待复核" in (region_pack.role_reason or "")
    assert region_pack.role_signals["signals"]["segment_size"] == 1
