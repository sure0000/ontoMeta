"""事实/动词命名 → 事实关系（复用 bridge）的分类规则测试。

建模铁律：表名或含义为动作/事件动词（xx调整、xx交易）时，这张表记录的是一次业务
**事实**而非实体，真正的业务对象是它引用的键。分类器应把本会被判为业务对象者改判为
事实/关系表（bridge）并标待复核；被多表引用的枢纽也不再豁免（仅在理由里标注供人工
复核）；但仍不硬压过人工术语，也不强扭派生汇总/系统表。
"""

from __future__ import annotations

from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
from app.services.evidence_builder import EvidenceBuilder
from app.services.fact_naming import detect_fact_name, detect_weak_fact_name
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    FieldSignal,
    classify_object_role,
)


def _f(name, sem, pk=False, fk=False):
    return FieldSignal(name=name, semantic_type=sem, is_primary_key=pk, is_foreign_key=fk)


# -- detect_fact_name --------------------------------------------------------


def test_detect_cjk_and_latin_verbs():
    assert detect_fact_name("库存调整") == "调整"
    assert detect_fact_name("stock_adjustment") == "adjustment"
    # 中英并存时优先返回可读性更好的中文词元。
    assert detect_fact_name("gl_txn", "总账交易") == "交易"


def test_detect_ignores_entity_and_boundary_false_positives():
    assert detect_fact_name("customer") is None
    assert detect_fact_name("product_catalog") is None  # log 不应命中
    assert detect_fact_name("user_login") is None  # log 不应命中
    assert detect_fact_name("exchange_rate") is None  # change 不应命中


# -- detect_fact_name: 结构性事实/明细特征 ----------------------------------


def test_detect_structural_fact_detail_names():
    # 结构性命名（明细/事实/记录/行项/订单行）也表明是事实/明细而非实体。
    assert detect_fact_name("订单明细") == "明细"
    assert detect_fact_name("order_fact", "订单事实表") == "事实"
    assert detect_fact_name(None, "支付记录") == "记录"
    assert detect_fact_name(None, "财务报表行项") == "行项"
    assert detect_fact_name("采购订单行") == "订单行"


def test_detect_excludes_reference_plan_ranking_and_bare_hang():
    # 参考数据/计划/派生排名不是事实，不应命中。
    assert detect_fact_name("价格清单") is None
    assert detect_fact_name("节假日清单") is None
    assert detect_fact_name("爆品排行") is None
    assert detect_fact_name("商品销量排行") is None
    assert detect_fact_name("生产计划") is None
    assert detect_fact_name("订单") is None  # 订单是实体，非「单」类事实
    # 裸「行」不应命中：银行/行业/配送行程/质量行动。
    assert detect_fact_name("银行") is None
    assert detect_fact_name("行业类型") is None
    assert detect_fact_name("配送行程") is None
    assert detect_fact_name("质量行动") is None


# -- classify_object_role: 事实命名改判 --------------------------------------


def _business_shaped():
    """单列业务主键 + 描述性属性 → 无命名信号时会判业务对象。"""
    return [
        _f("adj_id", "identifier", pk=True),
        _f("reason", "attribute"),
        _f("status", "category"),
    ]


def test_verb_named_business_shape_reclassified_to_bridge():
    baseline = classify_object_role(_business_shaped())
    assert baseline.role == ROLE_BUSINESS_OBJECT

    result = classify_object_role(_business_shaped(), fact_name_token="调整")
    assert result.role == ROLE_BRIDGE
    assert result.needs_review is True
    assert "调整" in result.reason
    assert result.signals.get("fact_name_token") == "调整"


def test_glossary_term_exempts_verb_named_table():
    # 人工已挂业务术语 → 已确认为业务概念，命名信号豁免，保留业务对象。
    result = classify_object_role(
        _business_shaped(), fact_name_token="调整", glossary_terms=["库存调整"]
    )
    assert result.role == ROLE_BUSINESS_OBJECT
    assert "fact_name_token" not in result.signals


def test_reference_hub_still_reclassified_to_bridge_but_flagged():
    # 被多张表外键引用的枢纽**不再**否决事实判定：交易头本就会被下游单据引用，
    # 仍按「事实即关系」判为关系表(bridge)，但保留待复核并在理由里标注枢纽属性，
    # 交人工确认是否实为命名欠佳的主数据。
    result = classify_object_role(
        _business_shaped(), fact_name_token="交易", fk_in_degree=5
    )
    assert result.role == ROLE_BRIDGE
    assert result.needs_review is True
    assert "5 张表" in result.reason


def test_verb_named_summary_stays_data_table():
    # 度量为主、无主键的派生汇总即便动词命名，也维持数据表，不强扭成关系表。
    fields = [_f("txn_date", "datetime"), _f("amount", "amount"), _f("fee", "amount")]
    result = classify_object_role(fields, fact_name_token="交易")
    assert result.role == ROLE_DATA_TABLE


# -- 端到端：evidence_builder 组装 -------------------------------------------


def test_evidence_builder_marks_verb_named_table_as_bridge():
    adjustment = DatasetInput(
        urn="urn:li:dataset:stock_adjustment",
        name="stock_adjustment",
        display_name="库存调整",
        fields=[
            FieldInput(name="adj_id", data_type="string", is_primary_key=True),
            FieldInput(
                name="product_id",
                data_type="string",
                is_foreign_key=True,
                foreign_key_target="product.id",
            ),
            FieldInput(name="reason", data_type="string"),
        ],
    )
    product = DatasetInput(
        urn="urn:li:dataset:product",
        name="product",
        display_name="商品",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="category", data_type="string"),
        ],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:test", name="测试域"),
        datasets=[adjustment, product],
        lineages=[],
    )
    evidence = EvidenceBuilder().build(bundle)
    pack = next(
        p
        for p in evidence.object_types
        if p.source_dataset_urn == "urn:li:dataset:stock_adjustment"
    )
    # 动词命名 → 初判事实/关系表(bridge)，但仅引用 1 个对象、连不到两个业务对象
    # → 智能重判为对象。分类器层面的「动词→bridge」由 test_verb_named_business_shape_
    # reclassified_to_bridge 直接覆盖。
    assert pack.table_role != ROLE_BRIDGE
    assert "重判" in (pack.role_reason or "")


# -- 结构性明细命名：改判 bridge + 端到端丢边 --------------------------------


def test_detail_named_business_shape_reclassified_to_bridge():
    # 结构性明细命名的业务型表 → 事实/关系表(bridge)，与动词命名同一条改判路径。
    result = classify_object_role(_business_shaped(), fact_name_token="明细")
    assert result.role == ROLE_BRIDGE
    assert result.needs_review is True
    assert "明细" in result.reason


def test_glossary_exempts_detail_named_table():
    result = classify_object_role(
        _business_shaped(), fact_name_token="明细", glossary_terms=["订单明细"]
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_detail_table_edges_dropped_after_bridge_reclassification():
    """事实/明细表改判为关系(bridge)后，其发出的外键边被 rule 1 丢弃（源非业务对象）。"""
    order_detail = DatasetInput(
        urn="urn:li:dataset:order_line",
        name="order_line_entity",
        display_name="订单明细",
        fields=[
            FieldInput(name="line_no", data_type="string", is_primary_key=True),
            FieldInput(name="remark", data_type="string"),
            FieldInput(name="status", data_type="string"),
            FieldInput(
                name="order_id", data_type="string",
                is_foreign_key=True, foreign_key_target="order_entity.id",
            ),
            FieldInput(
                name="product_id", data_type="string",
                is_foreign_key=True, foreign_key_target="product_entity.id",
            ),
        ],
    )
    order = DatasetInput(
        urn="urn:li:dataset:order", name="order_entity", display_name="订单",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="channel", data_type="string"),
            FieldInput(name="status", data_type="string"),
        ],
    )
    product = DatasetInput(
        urn="urn:li:dataset:product", name="product_entity", display_name="商品",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="category", data_type="string"),
        ],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:sales", name="销售"),
        datasets=[order_detail, order, product],
        lineages=[],
    )
    evidence = EvidenceBuilder().build(bundle)
    od = next(
        p for p in evidence.object_types
        if p.source_dataset_urn == "urn:li:dataset:order_line"
    )
    # 明细表不再是业务对象，改判为关系表(bridge) + 待复核。
    assert od.table_role == ROLE_BRIDGE
    assert od.role_signals["signals"].get("fact_name_token") == "明细"
    # 其散射外键边全部被丢弃：最终关系中不再出现该明细表任一端。
    assert all(
        od.candidate_name not in (r.source_object, r.target_object)
        for r in evidence.relations
    )


# -- B: 弱交易命名 + 结构证据 → 事实/关系表 --------------------------------


def test_detect_weak_transaction_nouns():
    # 弱信号词（订单/发票/工单/order/invoice）——歧义名词，命中弱检测。
    assert detect_weak_fact_name("purchase_order") == "order"
    assert detect_weak_fact_name("sales_invoice") == "invoice"
    assert detect_weak_fact_name(None, "生产工单") == "工单"
    assert detect_weak_fact_name(None, "采购订单") == "订单"
    # 且它们**不在**强事实词表里——所以必须靠结构证据才改判，而非强命名。
    assert detect_fact_name("purchase_order") is None
    assert detect_fact_name(None, "采购订单") is None
    # 不含交易词头的参考维度不匹配弱信号。
    assert detect_weak_fact_name(None, "付款方式") is None
    assert detect_weak_fact_name(None, "付款条款") is None


def _txn_header_shaped():
    """交易头形态：单列业务主键 + 外键 + 度量字段（会被结构信号带偏成业务对象）。"""
    return [
        _f("po_id", "identifier", pk=True),
        _f("supplier_id", "identifier", fk=True),
        _f("total", "amount"),
        _f("status", "category"),
    ]


def test_weak_txn_name_with_structure_reclassified_to_bridge():
    # 无弱信号 → 交易头被主键/结构带偏成业务对象。
    baseline = classify_object_role(_txn_header_shaped(), distinct_fk_targets=3)
    assert baseline.role == ROLE_BUSINESS_OBJECT
    # 弱交易命名 + 引用≥2维度 + 含度量 → 结构上是事实，改判 bridge + 待复核。
    result = classify_object_role(
        _txn_header_shaped(), distinct_fk_targets=3, weak_fact_name_token="订单"
    )
    assert result.role == ROLE_BRIDGE
    assert result.needs_review is True
    assert "订单" in result.reason
    assert result.signals.get("weak_fact_name_token") == "订单"


def test_weak_txn_name_without_measure_stays_business_object():
    # 弱交易命名但**无度量字段**（如「订单类型」这类参考维度）→ 结构不达标，保留业务对象。
    dim = [
        _f("type_id", "identifier", pk=True),
        _f("type_name", "attribute"),
        _f("memo", "attribute"),
    ]
    result = classify_object_role(
        dim, distinct_fk_targets=0, weak_fact_name_token="订单"
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_weak_txn_name_multi_fk_but_no_measure_not_forced_fact():
    # 多外键但无度量：不满足「事实」结构证据，弱规则不改判（仍可作关联实体待复核）。
    fields = [
        _f("id", "identifier", pk=True),
        _f("a_id", "identifier", fk=True),
        _f("b_id", "identifier", fk=True),
        _f("note", "attribute"),
    ]
    result = classify_object_role(
        fields, distinct_fk_targets=2, weak_fact_name_token="订单"
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_glossary_exempts_weak_txn_name():
    # 挂了人工业务术语 → 已确认业务身份，弱事实改判豁免。
    result = classify_object_role(
        _txn_header_shaped(),
        distinct_fk_targets=3,
        weak_fact_name_token="订单",
        glossary_terms=["采购订单"],
    )
    assert result.role == ROLE_BUSINESS_OBJECT


def test_evidence_builder_marks_transaction_header_as_bridge():
    # 端到端：采购订单（引用供应商/仓库两个维度 + 含金额度量）→ 事实/关系表(bridge)。
    supplier = DatasetInput(
        urn="urn:li:dataset:supplier", name="supplier", display_name="供应商",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="contact", data_type="string"),
        ],
    )
    warehouse = DatasetInput(
        urn="urn:li:dataset:warehouse", name="warehouse", display_name="仓库",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="location", data_type="string"),
        ],
    )
    purchase_order = DatasetInput(
        urn="urn:li:dataset:purchase_order", name="purchase_order", display_name="采购订单",
        fields=[
            FieldInput(name="po_id", data_type="string", is_primary_key=True),
            FieldInput(name="supplier_id", data_type="string", is_foreign_key=True, foreign_key_target="supplier.id"),
            FieldInput(name="warehouse_id", data_type="string", is_foreign_key=True, foreign_key_target="warehouse.id"),
            FieldInput(name="total_amount", data_type="decimal"),
            FieldInput(name="status", data_type="string"),
        ],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:proc", name="采购域"),
        datasets=[purchase_order, supplier, warehouse],
        lineages=[],
    )
    evidence = EvidenceBuilder().build(bundle)
    po = next(
        p for p in evidence.object_types
        if p.source_dataset_urn == "urn:li:dataset:purchase_order"
    )
    assert po.table_role == ROLE_BRIDGE
    assert po.needs_review is True
    assert po.role_signals["signals"].get("weak_fact_name_token") == "订单"
