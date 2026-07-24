from app.schemas import (
    DatasetInput,
    DomainInput,
    FieldInput,
    LineageInput,
    DataHubDomainBundle,
)
from app.services.evidence_builder import EvidenceBuilder
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    FieldSignal,
    classify_object_role,
)


def _f(name, sem, pk=False, fk=False):
    return FieldSignal(name=name, semantic_type=sem, is_primary_key=pk, is_foreign_key=fk)


def _f2(name, sem, pk=False, fk=False, unique=None):
    return FieldSignal(
        name=name,
        semantic_type=sem,
        is_primary_key=pk,
        is_foreign_key=fk,
        unique_count=unique,
    )


def test_glossary_term_tips_borderline_to_business_object():
    fields = [_f("stat_date", "datetime"), _f("biz_no", "identifier")]
    # 无主键、无描述属性 → 基线偏数据表；挂了业务术语后被拉回业务对象。
    assert classify_object_role(fields).role == ROLE_DATA_TABLE
    result = classify_object_role(fields, glossary_terms=["客户"])
    assert result.role == ROLE_BUSINESS_OBJECT
    assert "业务术语" in result.reason


def test_pk_uniqueness_confirms_identity():
    result = classify_object_role(
        [
            _f2("cust_id", "identifier", pk=True, unique=1000),
            _f("cust_name", "attribute"),
        ],
        row_count=1000,
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert "唯一度" in result.reason


def test_non_unique_declared_pk_penalized():
    # 声明为主键但实际唯一度很低（分区键）+ 度量为主 → 汇总表。
    result = classify_object_role(
        [
            _f2("stat_date", "identifier", pk=True, unique=30),
            _f("gmv", "amount"),
            _f("cnt", "amount"),
        ],
        row_count=9000,
    )
    assert "唯一度" in result.reason
    assert result.role == ROLE_DATA_TABLE


def test_single_pk_with_attributes_is_business_object():
    result = classify_object_role(
        [
            _f("cust_id", "identifier", pk=True),
            _f("cust_name", "attribute"),
            _f("level", "category"),
            _f("city", "attribute"),
        ],
        fk_in_degree=4,
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert result.confidence >= 0.7
    assert "主键" in result.reason


def test_aggregate_table_with_measures_and_grain_is_data_table():
    result = classify_object_role(
        [
            _f("stat_date", "datetime"),
            _f("region", "category"),
            _f("gmv", "amount"),
            _f("order_cnt", "amount"),
            _f("refund_amt", "amount"),
        ],
        fk_in_degree=0,
        lineage_upstream=2,
        lineage_downstream=0,
    )
    assert result.role == ROLE_DATA_TABLE
    assert "度量" in result.reason


def test_composite_fk_primary_key_is_bridge():
    result = classify_object_role(
        [
            _f("order_id", "identifier", pk=True, fk=True),
            _f("product_id", "identifier", pk=True, fk=True),
            _f("qty", "amount"),
        ],
    )
    assert result.role == ROLE_BRIDGE


def test_middle_ground_defaults_to_business_object_low_confidence():
    result = classify_object_role(
        [
            _f("code", "attribute"),
            _f("val", "attribute"),
        ],
        fk_in_degree=0,
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert result.confidence <= 0.55


def test_evidence_builder_annotates_roles():
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="域"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:cust",
                name="customer",
                display_name="客户",
                fields=[
                    FieldInput(name="cust_id", is_primary_key=True),
                    FieldInput(name="cust_name"),
                    FieldInput(name="level"),
                ],
            ),
            DatasetInput(
                urn="urn:li:dataset:agg",
                name="sales_summary",
                display_name="销售汇总",
                fields=[
                    FieldInput(name="stat_date"),
                    FieldInput(name="gmv"),
                    FieldInput(name="order_amount"),
                    FieldInput(name="refund_amount"),
                ],
            ),
            DatasetInput(
                urn="urn:li:dataset:order",
                name="orders",
                display_name="订单",
                fields=[
                    FieldInput(name="order_id", is_primary_key=True),
                    FieldInput(
                        name="cust_id",
                        is_foreign_key=True,
                        foreign_key_target="customer.cust_id",
                    ),
                ],
            ),
        ],
        lineages=[
            LineageInput(source_urn="urn:li:dataset:order", target_urn="urn:li:dataset:agg"),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    roles = {ot.candidate_name: ot.table_role for ot in evidence.object_types}
    # customer 被 orders 外键引用 + 单列主键 → 业务对象
    assert roles["customer_entity"] == ROLE_BUSINESS_OBJECT
    # sales_summary 无主键 + 度量为主 + 血缘末端 → 数据表
    assert roles["sales_summary_entity"] == ROLE_DATA_TABLE
    # 每个对象都带 reason
    assert all(ot.role_reason for ot in evidence.object_types)
