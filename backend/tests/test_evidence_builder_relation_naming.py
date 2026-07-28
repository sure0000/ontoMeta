"""证据组装阶段的关系命名与业务关系精炼。

背景:关系(尤其是血缘关系)的 description 是 LLM 推断业务关系名的主要依据。
若 description 里写的是技术表名(如 order_di_entity)而非业务展示名(如
订单明细)，LLM 拿到的语义信号过弱，容易一律退化为「派生」这类无信息量的
默认词。本文件验证:
- EvidenceBuilder 把业务展示名写进了关系描述，候选名仍由技术名推导;
- 血缘默认命名按生命周期「转化」、父子「包含」、加工类关键词细分;
- 业务关系精炼:rule 1(业务关系只存在于业务对象之间)、与 FK 重复的血缘去重、
  反向「主数据 派生出 单据」翻转为「单据 引用 主数据」。

为聚焦命名/精炼逻辑而非对象角色分类，测试数据集统一挂载 glossary_terms
(最强业务信号，稳定判为业务对象)，使关系不被 rule 1 过滤。
"""

from __future__ import annotations

from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput, LineageInput
from app.services.relation_terms import infer_relation_term, reference_term


def _ds(urn: str, name: str, display: str, fields: list[FieldInput], **kw) -> DatasetInput:
    """业务对象数据集(挂业务术语豁免角色降级)，默认给一批描述性字段坐实身份。"""
    return DatasetInput(
        urn=urn,
        name=name,
        display_name=display,
        fields=fields,
        glossary_terms=[display],
        **kw,
    )


def _bundle() -> DataHubDomainBundle:
    order_detail = _ds(
        "urn:li:dataset:order_di_entity", "order_di_entity", "订单明细",
        [
            FieldInput(name="order_id", data_type="string"),
            FieldInput(
                name="customer_id", data_type="string",
                is_foreign_key=True, foreign_key_target="customer_entity.id",
            ),
        ],
    )
    customer = _ds(
        "urn:li:dataset:customer_entity", "customer_entity", "客户",
        [FieldInput(name="id", data_type="string")],
    )
    reconciliation = _ds(
        "urn:li:dataset:finance_reconciliation_1d_entity",
        "finance_reconciliation_1d_entity", "财务对账1日汇总",
        [FieldInput(name="id", data_type="string")],
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


def _build(bundle: DataHubDomainBundle):
    from app.services.evidence_builder import EvidenceBuilder

    return EvidenceBuilder().build(bundle)


# --------------------------------------------------------------------------- #
# 描述文本使用业务展示名
# --------------------------------------------------------------------------- #
def test_lineage_relation_description_uses_business_display_names():
    evidence = _build(_bundle())
    lineage_rel = next(r for r in evidence.relations if r.structure_type == "derivation")

    assert "订单明细" in lineage_rel.description
    assert "财务对账1日汇总" in lineage_rel.description
    assert "order_di_entity" not in lineage_rel.description
    assert "finance_reconciliation_1d_entity" not in lineage_rel.description
    # 候选名(回链/结构组装用)仍必须由技术名推导。
    assert lineage_rel.source_object == "order_di_entity"
    assert lineage_rel.target_object == "finance_reconciliation_1d_entity"


def test_lineage_relation_default_term_uses_target_keyword_not_generic_derivation():
    evidence = _build(_bundle())
    lineage_rel = next(r for r in evidence.relations if r.structure_type == "derivation")

    assert lineage_rel.structure_type == "derivation"
    # 目标含「对账」→ 加工类前向谓词，而非笼统「派生」。
    assert lineage_rel.display_name == "对账为"
    assert "生成" not in lineage_rel.display_name
    assert "派生" not in lineage_rel.display_name


def test_foreign_key_relation_description_uses_target_business_display_name():
    evidence = _build(_bundle())
    fk_rel = next(r for r in evidence.relations if r.structure_type == "foreign_key")

    assert "订单明细" in fk_rel.description
    assert "通过外键 customer_id 关联 客户" in fk_rel.description


# --------------------------------------------------------------------------- #
# infer_relation_term 直测:转化 / 包含 / 加工类 / 兜底
# --------------------------------------------------------------------------- #
def test_infer_relation_term_lineage_keyword_matrix():
    assert infer_relation_term("lineage", target_label="财务对账结果") == "对账为"
    assert infer_relation_term("lineage", target_label="销售统计报表") == "统计为"
    assert infer_relation_term("lineage", target_label="每日销售报表") == "统计为"
    assert infer_relation_term("lineage", target_label="销售汇总表") == "汇总为"
    assert infer_relation_term("lineage", target_label="结算单") == "结算为"
    assert infer_relation_term("lineage", target_label="用户画像") == "刻画"
    assert infer_relation_term("lineage", target_label="未知目标") == "派生出"
    assert infer_relation_term("lineage") == "派生出"


def test_infer_relation_term_lifecycle_transition():
    # 同一实体的生命周期阶段(去阶段前缀后核心名相等)→ 转化。
    assert infer_relation_term("lineage", source_label="潜客商机", target_label="商机") == "转化"
    assert infer_relation_term("lineage", source_label="潜客线索", target_label="线索") == "转化"
    assert infer_relation_term("lineage", source_label="客户", target_label="潜在客户") == "转化"
    # CRM 漏斗跨名相邻(线索/潜客 ↔ 商机)→ 转化。
    assert infer_relation_term("lineage", source_label="潜在客户", target_label="潜客商机") == "转化"
    assert infer_relation_term("lineage", source_label="线索", target_label="商机") == "转化"
    # 仅共享一个词但非同一实体阶段 → 不判转化(避免误伤维度)。
    assert infer_relation_term("lineage", source_label="客户", target_label="客户分组") != "转化"
    assert infer_relation_term("lineage", source_label="潜客商机", target_label="币种") != "转化"


def test_infer_relation_term_parent_detail_is_contains():
    assert infer_relation_term("lineage", source_label="商机", target_label="商机明细") == "包含"
    assert (
        infer_relation_term(
            "lineage", source_label="长期协议订单", target_label="长期协议订单明细"
        )
        == "包含"
    )
    # 转化优先级高于包含,但二者输入不重叠;顺序上转化不误吞明细。
    assert infer_relation_term("lineage", source_label="订单", target_label="订单行") == "包含"


def test_reference_term_by_master_label():
    assert reference_term("地址") == "位于"
    assert reference_term("国家") == "位于"
    assert reference_term("公司") == "属于"
    assert reference_term("币种") == "采用"
    assert reference_term("某主数据") == "引用"


# --------------------------------------------------------------------------- #
# rule 1:业务关系只存在于业务对象之间
# --------------------------------------------------------------------------- #
def test_rule1_drops_relations_touching_non_business_object():
    """一端是技术/系统表(无业务术语、技术字段为主)时,其关系被过滤。"""
    biz = _ds(
        "urn:li:dataset:invoice_entity", "invoice_entity", "发票",
        [
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(
                name="session_id", data_type="string",
                is_foreign_key=True, foreign_key_target="auth_session_entity.id",
            ),
        ],
    )
    # 技术表:大量技术字段、无业务术语、无业务命名 → technical。
    session = DatasetInput(
        urn="urn:li:dataset:auth_session_entity", name="auth_session_entity",
        fields=[
            FieldInput(name="token", data_type="string"),
            FieldInput(name="secret", data_type="string"),
            FieldInput(name="jwt", data_type="string"),
            FieldInput(name="cookie", data_type="string"),
        ],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:erp", name="ERP"),
        datasets=[biz, session],
        lineages=[
            LineageInput(
                source_urn="urn:li:dataset:invoice_entity",
                target_urn="urn:li:dataset:auth_session_entity",
            )
        ],
    )
    evidence = _build(bundle)
    session_obj = next(
        o for o in evidence.object_types if o.candidate_name == "auth_session_entity"
    )
    assert session_obj.table_role != "business_object"
    # 指向/来自技术表的外键与血缘都被 rule 1 过滤。
    assert all(
        "auth_session_entity" not in (r.source_object, r.target_object)
        for r in evidence.relations
    )


# --------------------------------------------------------------------------- #
# 血缘与已有 FK 重复 → 去重
# --------------------------------------------------------------------------- #
def test_derivation_duplicating_fk_pair_is_dropped():
    """同一对象对既有外键又有血缘时,血缘边(重复表达同一关联)被丢弃。"""
    lead = _ds(
        "urn:li:dataset:lead_entity", "lead_entity", "线索",
        [
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(
                name="customer_id", data_type="string",
                is_foreign_key=True, foreign_key_target="customer_entity.id",
            ),
        ],
    )
    customer = _ds(
        "urn:li:dataset:customer_entity", "customer_entity", "客户",
        [FieldInput(name="id", data_type="string", is_primary_key=True)],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:crm", name="CRM"),
        datasets=[lead, customer],
        # 外键方向 lead→customer;血缘同对(反向),应被折叠/去重,不额外留一条派生。
        lineages=[
            LineageInput(source_urn="urn:li:dataset:customer_entity", target_urn="urn:li:dataset:lead_entity"),
        ],
    )
    evidence = _build(bundle)
    pair_rels = [
        r for r in evidence.relations
        if {r.source_object, r.target_object} == {"lead_entity", "customer_entity"}
    ]
    # 仅保留外键关系,不再有并列的血缘/派生。
    assert len(pair_rels) == 1
    assert pair_rels[0].structure_type == "foreign_key"


# --------------------------------------------------------------------------- #
# 反向「主数据 派生出 单据」翻转为「单据 引用 主数据」
# --------------------------------------------------------------------------- #
def test_reversed_master_to_document_derivation_is_flipped_to_reference():
    """主数据(少行)作为血缘源、单据(多行)为目标 → 翻转为 单据→主数据 的引用关系。"""
    address = _ds(
        "urn:li:dataset:address_entity", "address_entity", "地址",
        [FieldInput(name="line1"), FieldInput(name="city")],
        row_count=50,
    )
    opportunity = _ds(
        "urn:li:dataset:opportunity_entity", "opportunity_entity", "商机",
        [FieldInput(name="id", data_type="string", is_primary_key=True), FieldInput(name="stage")],
        row_count=5000,
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:crm", name="CRM"),
        datasets=[address, opportunity],
        # 血缘方向反了:主数据 地址 → 单据 商机。
        lineages=[
            LineageInput(source_urn="urn:li:dataset:address_entity", target_urn="urn:li:dataset:opportunity_entity"),
        ],
    )
    evidence = _build(bundle)
    rels = [r for r in evidence.relations if {r.source_object, r.target_object} == {"address_entity", "opportunity_entity"}]
    assert len(rels) == 1
    rel = rels[0]
    # 翻转为 单据(商机) → 主数据(地址),引用类命名,归为业务关联结构。
    assert rel.source_object == "opportunity_entity"
    assert rel.target_object == "address_entity"
    assert rel.display_name == "位于"
    assert rel.structure_type == "foreign_key"
    assert rel.display_name != "派生出"


def test_ambiguous_derivation_keeps_generic_term():
    """行数、关联度都判不出主/明细时,诚实保留「派生出」而非乱翻转。"""
    a = _ds(
        "urn:li:dataset:aa_entity", "aa_entity", "甲对象",
        [FieldInput(name="id", data_type="string", is_primary_key=True), FieldInput(name="note")],
    )
    b = _ds(
        "urn:li:dataset:bb_entity", "bb_entity", "乙对象",
        [FieldInput(name="id", data_type="string", is_primary_key=True), FieldInput(name="memo")],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:x", name="X"),
        datasets=[a, b],
        lineages=[LineageInput(source_urn="urn:li:dataset:aa_entity", target_urn="urn:li:dataset:bb_entity")],
    )
    evidence = _build(bundle)
    rel = next(r for r in evidence.relations if {r.source_object, r.target_object} == {"aa_entity", "bb_entity"})
    # 无行数差、关联度相同(各 1)→ 保留派生出。
    assert rel.display_name == "派生出"
    assert rel.structure_type == "derivation"


# --------------------------------------------------------------------------- #
# 结构类型推断与双向折叠(既有回归)
# --------------------------------------------------------------------------- #
def test_infer_relation_structure_type_lineage_is_derivation():
    from app.services.relation_structure import (
        RELATION_STRUCTURE_TYPES,
        infer_relation_structure_type,
        validate_relation_structure_type,
    )

    assert infer_relation_structure_type("血缘：订单明细 加工至 结算汇总") == "derivation"
    assert infer_relation_structure_type("lineage: A -> B") == "derivation"
    assert infer_relation_structure_type("通过外键 order_id 关联") == "foreign_key"
    assert infer_relation_structure_type("桥表关联") == "bridge_table"
    assert "derivation" in RELATION_STRUCTURE_TYPES
    assert validate_relation_structure_type("derivation") is None


def test_bidirectional_lineage_collapsed_to_single_direction():
    """DataHub 将引用型外键按双向血缘导入时,证据组装应折叠为单向。

    国家作为主数据被多表引用,关联度更高,应作为目标;反向应被丢弃。三张表都挂
    业务术语,确保不被 rule 1 过滤,聚焦验证折叠与方向。
    """
    country = _ds(
        "urn:li:dataset:country", "country", "国家",
        [FieldInput(name="country_name"), FieldInput(name="code")],
    )
    address_template = _ds(
        "urn:li:dataset:address_template", "address_template", "地址模板",
        [FieldInput(name="template"), FieldInput(name="country")],
    )
    address = _ds(
        "urn:li:dataset:address", "address", "地址",
        [FieldInput(name="line1"), FieldInput(name="country")],
    )
    bundle = DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:erp", name="ERP"),
        datasets=[country, address_template, address],
        lineages=[
            LineageInput(source_urn="urn:li:dataset:address_template", target_urn="urn:li:dataset:country"),
            LineageInput(source_urn="urn:li:dataset:country", target_urn="urn:li:dataset:address_template"),
            LineageInput(source_urn="urn:li:dataset:address", target_urn="urn:li:dataset:country"),
            LineageInput(source_urn="urn:li:dataset:country", target_urn="urn:li:dataset:address"),
        ],
    )
    evidence = _build(bundle)
    pairs = {(r.source_object, r.target_object) for r in evidence.relations}
    # 主数据国家作为目标(被多表引用,关联度高),反向被折叠。
    assert ("address_template_entity", "country_entity") in pairs
    assert ("country_entity", "address_template_entity") not in pairs
    assert ("address", "country_entity") in pairs
    assert ("country_entity", "address") not in pairs
    # 同一无序对不再同时出现两个方向。
    assert not any((t, s) in pairs for (s, t) in pairs)
