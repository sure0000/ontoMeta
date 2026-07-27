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
    ROLE_TECHNICAL,
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


def test_isolated_dict_like_table_defaults_to_data_table():
    # 无主键、无任何业务关系（无外键、无血缘）的孤岛字典/配置表，
    # 不应默认识别为业务对象，而是降为普通数据表待人工确认。
    result = classify_object_role(
        [
            _f("code", "attribute"),
            _f("val", "attribute"),
        ],
        fk_in_degree=0,
    )
    assert result.role == ROLE_DATA_TABLE
    assert "脱节" in result.reason


def test_isolated_pk_only_table_is_not_business_object():
    # 只有一个主键 + 几个通用字段、无任何业务关系的孤岛表（如日志/临时表）：
    # 不能仅凭一个主键就被识别为业务对象。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("ts", "datetime"),
            _f("msg", "attribute"),
        ],
        fk_in_degree=0,
        lineage_upstream=0,
        lineage_downstream=0,
        has_business_naming=False,
    )
    assert result.role != ROLE_BUSINESS_OBJECT


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


def test_auth_like_table_classified_technical():
    # auth 表：技术词汇字段占多数 + 缺业务命名 → 技术/系统表，而非业务对象。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("token", "technical"),
            _f("secret", "technical"),
            _f("expires_at", "technical"),
            _f("salt", "technical"),
        ],
        fk_in_degree=0,
        has_business_naming=False,
    )
    assert result.role == ROLE_TECHNICAL
    assert "技术" in result.reason


def test_glossary_exempts_technical_table():
    # 即便字段偏技术，只要人工挂了业务术语就不判技术表。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("token", "technical"),
            _f("secret", "technical"),
            _f("salt", "technical"),
        ],
        glossary_terms=["凭证"],
    )
    assert result.role != ROLE_TECHNICAL


def test_datahub_tag_hint_drives_technical():
    # 显式 DataHub tag 标注为 system + 图孤立 → 技术表（tag 2.0 + 孤立 1.0）。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("value", "attribute"),
        ],
        tags=["System"],
        has_business_naming=True,
    )
    assert result.role == ROLE_TECHNICAL
    assert "DataHub" in result.reason


def test_isolated_business_table_without_tech_vocab_stays_business():
    # 纯业务字段的孤立小表（无技术词汇）不应被误判为技术表。
    result = classify_object_role(
        [
            _f("cust_id", "identifier", pk=True),
            _f("cust_name", "attribute"),
            _f("city", "attribute"),
        ],
        fk_in_degree=0,
        has_business_naming=True,
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_evidence_builder_flags_auth_as_technical():
    from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput

    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="域"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:auth",
                name="auth",
                display_name="auth",
                fields=[
                    FieldInput(name="id", is_primary_key=True),
                    FieldInput(name="access_token"),
                    FieldInput(name="refresh_token"),
                    FieldInput(name="secret_key"),
                    FieldInput(name="expires_at"),
                ],
            ),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    roles = {ot.candidate_name: ot.table_role for ot in evidence.object_types}
    assert roles["auth_entity"] == ROLE_TECHNICAL


# ---- Slice 1: facet B（关系被误判为实体 / 关联实体去混淆）----


def test_surrogate_pk_multi_fk_pure_link_is_bridge():
    # 代理主键 + 指向 2 个不同实体 + 无自有属性 + 无人反向引用 → 纯关系/桥接。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("user_id", "identifier", fk=True),
            _f("product_id", "identifier", fk=True),
            _f("created_at", "datetime"),
        ],
        distinct_fk_targets=2,
        fk_in_degree=0,
    )
    assert result.role == ROLE_BRIDGE
    assert result.needs_review
    assert "不同实体" in result.reason


def test_associative_entity_with_own_attrs_stays_business_but_flagged():
    # 订单：代理主键 + 指向 2 个不同实体，但有自有属性(金额/状态) → 关联实体，
    # 保留为业务对象但标记待复核（宁可漏降不可错降）。
    result = classify_object_role(
        [
            _f("order_id", "identifier", pk=True),
            _f("customer_id", "identifier", fk=True),
            _f("product_id", "identifier", fk=True),
            _f("amount", "amount"),
            _f("status", "category"),
            _f("order_date", "datetime"),
        ],
        distinct_fk_targets=2,
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert result.needs_review
    assert "关联实体" in result.reason


def test_multi_fk_but_referenced_not_demoted():
    # 无自有属性，但被其它表反向引用（fk_in_degree>0）→ 不降级为桥接。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("a_id", "identifier", fk=True),
            _f("b_id", "identifier", fk=True),
        ],
        distinct_fk_targets=2,
        fk_in_degree=2,
    )
    assert result.role != ROLE_BRIDGE
    assert result.needs_review


def test_glossary_exempts_multi_fk_demotion():
    # 挂了业务术语 → 即使结构像纯关系也不降级。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("user_id", "identifier", fk=True),
            _f("product_id", "identifier", fk=True),
            _f("created_at", "datetime"),
        ],
        distinct_fk_targets=2,
        fk_in_degree=0,
        glossary_terms=["收藏关系"],
    )
    assert result.role != ROLE_BRIDGE


def test_single_fk_does_not_trigger_facet_b():
    # 仅指向 1 个不同实体（普通明细引用一个主数据）→ 不触发 facet B 降级。
    result = classify_object_role(
        [
            _f("id", "identifier", pk=True),
            _f("owner_id", "identifier", fk=True),
            _f("name", "attribute"),
            _f("city", "attribute"),
        ],
        distinct_fk_targets=1,
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_evidence_builder_detects_surrogate_pk_junction_as_bridge():
    # 端到端：evidence_builder 应算出 distinct_fk_targets 并把代理主键关联表判为桥接。
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="域"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:users",
                name="users",
                display_name="用户",
                fields=[
                    FieldInput(name="user_id", is_primary_key=True),
                    FieldInput(name="user_name"),
                ],
            ),
            DatasetInput(
                urn="urn:li:dataset:products",
                name="products",
                display_name="商品",
                fields=[
                    FieldInput(name="product_id", is_primary_key=True),
                    FieldInput(name="product_name"),
                ],
            ),
            DatasetInput(
                urn="urn:li:dataset:fav",
                name="user_favorite",
                display_name="用户收藏",
                fields=[
                    FieldInput(name="id", is_primary_key=True),
                    FieldInput(
                        name="user_id",
                        is_foreign_key=True,
                        foreign_key_target="users.user_id",
                    ),
                    FieldInput(
                        name="product_id",
                        is_foreign_key=True,
                        foreign_key_target="products.product_id",
                    ),
                    FieldInput(name="created_at"),
                ],
            ),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    roles = {ot.candidate_name: ot.table_role for ot in evidence.object_types}
    assert roles["user_favorite_entity"] == ROLE_BRIDGE
