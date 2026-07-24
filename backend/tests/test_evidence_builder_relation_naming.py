"""证据组装阶段的关系描述应使用业务展示名，而非技术表名。

背景:关系(尤其是血缘关系)的 description 是 LLM 推断业务关系名的主要依据。
若 description 里写的是技术表名(如 order_di_entity)而非业务展示名(如
订单明细)，LLM 拿到的语义信号过弱，容易一律退化为「派生」这类无信息量的
默认词。本文件验证 EvidenceBuilder 把业务展示名写进了关系描述，且候选名
(source_object/target_object)仍由技术名推导，不受影响。
"""

from __future__ import annotations

from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput, LineageInput
from app.services.evidence_builder import EvidenceBuilder
from app.services.relation_terms import infer_relation_term


def _bundle() -> DataHubDomainBundle:
    order_detail = DatasetInput(
        urn="urn:li:dataset:order_di_entity",
        name="order_di_entity",
        display_name="订单明细",
        fields=[
            FieldInput(name="order_id", data_type="string"),
            FieldInput(
                name="customer_id",
                data_type="string",
                is_foreign_key=True,
                foreign_key_target="customer_entity.id",
            ),
        ],
    )
    customer = DatasetInput(
        urn="urn:li:dataset:customer_entity",
        name="customer_entity",
        display_name="客户",
        fields=[FieldInput(name="id", data_type="string")],
    )
    reconciliation = DatasetInput(
        urn="urn:li:dataset:finance_reconciliation_1d_entity",
        name="finance_reconciliation_1d_entity",
        display_name="财务对账1日汇总",
        fields=[FieldInput(name="id", data_type="string")],
    )
    return DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:test", name="测试域"),
        datasets=[order_detail, customer, reconciliation],
        lineages=[
            LineageInput(
                source_urn="urn:li:dataset:order_di_entity",
                target_urn="urn:li:dataset:finance_reconciliation_1d_entity",
            )
        ],
    )


def test_lineage_relation_description_uses_business_display_names():
    evidence = EvidenceBuilder().build(_bundle())
    lineage_rel = next(r for r in evidence.relations if r.structure_type != "foreign_key")

    assert "订单明细" in lineage_rel.description
    assert "财务对账1日汇总" in lineage_rel.description
    # 技术表名不应残留在描述里。
    assert "order_di_entity" not in lineage_rel.description
    assert "finance_reconciliation_1d_entity" not in lineage_rel.description

    # 候选名(回链/结构组装用)仍必须由技术名推导，不受描述文本变化影响。
    assert lineage_rel.source_object == "order_di_entity"
    assert lineage_rel.target_object == "finance_reconciliation_1d_entity"


def test_lineage_relation_default_term_uses_target_keyword_not_generic_derivation():
    evidence = EvidenceBuilder().build(_bundle())
    lineage_rel = next(r for r in evidence.relations if r.structure_type != "foreign_key")

    # 目标对象展示名含「对账」，默认词应体现这一点，而不是笼统的「派生」。
    assert lineage_rel.display_name != "派生"
    assert lineage_rel.display_name == "对账生成"


def test_foreign_key_relation_description_uses_target_business_display_name():
    evidence = EvidenceBuilder().build(_bundle())
    fk_rel = next(r for r in evidence.relations if r.structure_type == "foreign_key")

    assert "订单明细" in fk_rel.description
    # 业务展示名出现在可读的关联短语里(而非仅出现在末尾括号中的原始
    # foreign_key_target 引用里)，这才是 LLM 真正能读到的语义信号。
    assert "通过外键 customer_id 关联 客户" in fk_rel.description


def test_infer_relation_term_lineage_keyword_matrix():
    assert infer_relation_term("lineage", target_label="财务对账结果") == "对账生成"
    assert infer_relation_term("lineage", target_label="销售统计报表") == "统计汇总"
    assert infer_relation_term("lineage", target_label="每日销售报表") == "生成报表"
    assert infer_relation_term("lineage", target_label="结算单") == "结算生成"
    assert infer_relation_term("lineage", target_label="未知目标") == "加工生成"
    assert infer_relation_term("lineage") == "加工生成"
