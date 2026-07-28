"""证据组装阶段应把分类器的结构化判定证据(role_signals)随对象一并产出。

背景:object_classifier 已算出 score / needs_review / signals(主键、外键入度、
字段占比、tech_score、连通性等),但历史上组装 ObjectTypeEvidencePack 时被丢弃,
导致复核界面拿不到量化证据。本文件验证这些证据被完整带出,且短路分支(桥接/
子表)也不例外——「判定依据」面板据此渲染。
"""

from __future__ import annotations

from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
from app.services.evidence_builder import EvidenceBuilder


def _pack(evidence, urn):
    return next(p for p in evidence.object_types if p.source_dataset_urn == urn)


def _bundle() -> DataHubDomainBundle:
    customer = DatasetInput(
        urn="urn:li:dataset:customer",
        name="customer",
        display_name="客户",
        fields=[
            FieldInput(name="id", data_type="string", is_primary_key=True),
            FieldInput(name="name", data_type="string"),
            FieldInput(name="status", data_type="string"),
            FieldInput(name="level", data_type="string"),
        ],
    )
    # 三张订单表外键指向 customer → customer 的外键入度=3(枢纽/主数据信号)。
    orders = [
        DatasetInput(
            urn=f"urn:li:dataset:order{i}",
            name=f"order{i}",
            display_name=f"订单{i}",
            fields=[
                FieldInput(name="id", data_type="string", is_primary_key=True),
                FieldInput(
                    name="customer_id",
                    data_type="string",
                    is_foreign_key=True,
                    foreign_key_target="customer.id",
                ),
            ],
        )
        for i in range(1, 4)
    ]
    # 桥接表:主键完全由两个外键组成(引用 user / product,不影响 customer 入度)。
    favorite = DatasetInput(
        urn="urn:li:dataset:user_favorite",
        name="user_favorite",
        display_name="用户收藏",
        fields=[
            FieldInput(
                name="user_id",
                data_type="string",
                is_primary_key=True,
                is_foreign_key=True,
                foreign_key_target="user.id",
            ),
            FieldInput(
                name="product_id",
                data_type="string",
                is_primary_key=True,
                is_foreign_key=True,
                foreign_key_target="product.id",
            ),
        ],
    )
    return DataHubDomainBundle(
        domain=DomainInput(id="urn:li:domain:test", name="测试域"),
        datasets=[customer, *orders, favorite],
        lineages=[],
    )


def test_every_pack_carries_structured_role_signals():
    evidence = EvidenceBuilder().build(_bundle())
    assert evidence.object_types
    for pack in evidence.object_types:
        rs = pack.role_signals
        assert isinstance(rs, dict), f"{pack.candidate_name} 缺少 role_signals"
        # 关键结构:score / needs_review / role / signals 齐备,供面板渲染。
        assert {"score", "needs_review", "role", "signals"} <= set(rs)
        assert isinstance(rs["signals"], dict)
        # 组装阶段 role_signals.role 与 pack.table_role 一致(LLM 改判在后续阶段)。
        assert rs["role"] == pack.table_role


def test_customer_hub_scored_as_business_object_with_evidence():
    evidence = EvidenceBuilder().build(_bundle())
    pack = _pack(evidence, "urn:li:dataset:customer")
    assert pack.table_role == "business_object"
    signals = pack.role_signals["signals"]
    # 被多张表外键引用的枢纽信号被量化记录,且综合得分越过业务对象阈值。
    assert signals.get("fk_in_degree", 0) >= 3
    assert pack.role_signals["score"] >= 2.0


def test_bridge_shortcircuit_still_emits_signals():
    evidence = EvidenceBuilder().build(_bundle())
    pack = _pack(evidence, "urn:li:dataset:user_favorite")
    assert pack.table_role == "bridge"
    # 即便走短路分支,也要带上结构化证据(主键列数)供复核界面展示。
    assert isinstance(pack.role_signals, dict)
    assert pack.role_signals["signals"].get("pk_columns") == 2
