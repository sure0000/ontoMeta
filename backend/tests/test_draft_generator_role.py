from types import SimpleNamespace

from app.services.draft_generator import OntologyDraftGenerator
from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    ROLE_TECHNICAL,
)


def _ot(role, conf, reason, needs_review=False, abstained=False):
    return SimpleNamespace(
        table_role=role,
        role_confidence=conf,
        role_reason=reason,
        needs_review=needs_review,
        role_signals={"abstained": True} if abstained else {},
    )


def test_no_override_keeps_heuristic():
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.8, "单列业务主键")
    out = OntologyDraftGenerator._resolve_role(ot, None)
    assert out["table_role"] == ROLE_BUSINESS_OBJECT
    assert out["role_confidence"] == 0.8
    assert out["role_reason"] == "单列业务主键"


def test_agreement_keeps_heuristic_unchanged():
    ot = _ot(ROLE_TECHNICAL, 0.9, "技术词汇字段占比高")
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_TECHNICAL, "reason": "看起来像系统表", "evidence_gap": None}
    )
    # 一致：互证，保留启发式，不改置信度、不置复核。
    assert out["table_role"] == ROLE_TECHNICAL
    assert out["role_confidence"] == 0.9
    assert out["needs_review"] is False


def test_invalid_role_hint_ignored():
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.8, "reason")
    out = OntologyDraftGenerator._resolve_role(ot, {"role": "garbage"})
    assert out["table_role"] == ROLE_BUSINESS_OBJECT
    assert out["role_confidence"] == 0.8


def test_disagreement_flags_needs_review_and_lowers_confidence():
    # 启发式**确实主张**业务对象（有主键/被引用的正分证据），LLM=技术表 → 跨越业务
    # 边界的真分歧：标记待复核、下调置信度、并陈两方观点。
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.86, "单列业务主键，具备独立身份；被 4 张表外键引用")
    out = OntologyDraftGenerator._resolve_role(
        ot,
        {
            "role": ROLE_TECHNICAL,
            "reason": "字段全是配置项，业务人员不会当业务概念",
            "evidence_gap": "无列注释，仅凭字段名推断",
        },
    )
    assert out["table_role"] == ROLE_TECHNICAL  # 展示 LLM 语义判定
    assert out["role_confidence"] == 0.5  # 下调，凸显待复核
    assert out["needs_review"] is True
    assert "LLM 判为技术/系统表" in out["role_reason"]
    assert "启发式判为业务对象" in out["role_reason"]
    assert "证据缺口：无列注释" in out["role_reason"]


def test_llm_bridge_vote_overrides_business_object():
    # 启发式=业务对象，LLM=业务事实/关系表(维修/清算这类动作表)→ 改判 bridge、待复核。
    # 正是本次修复的核心：LLM 能把误判成实体的事实/动作表拉回关系维度。
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.86, "单列业务主键，具备独立身份")
    out = OntologyDraftGenerator._resolve_role(
        ot,
        {
            "role": ROLE_BRIDGE,
            "reason": "每行是一次维修事件，真正的业务对象是设备与维修工",
            "evidence_gap": None,
        },
    )
    assert out["table_role"] == ROLE_BRIDGE
    assert out["role_confidence"] == 0.5
    assert out["needs_review"] is True
    assert "LLM 判为业务事实/关系表" in out["role_reason"]
    assert "启发式判为业务对象" in out["role_reason"]


def test_parse_role_overrides_accepts_bridge():
    from app.schemas import (
        DataHubDomainBundle,
        DatasetInput,
        DomainInput,
        FieldInput,
    )
    from app.services.evidence_builder import EvidenceBuilder

    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="域"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:repair",
                name="equip_repair",
                display_name="设备维修工单",
                fields=[FieldInput(name="equip_id"), FieldInput(name="worker_id")],
            ),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    gen = OntologyDraftGenerator(runtime_config=None)
    raw = {
        "object_types": [
            {
                "source_ref": "urn:li:dataset:repair",
                "role_hint": "bridge",
                "role_reason": "每行是一次维修事件",
            }
        ]
    }
    overrides = gen._parse_role_overrides(raw, evidence)
    assert overrides
    ov = next(iter(overrides.values()))
    assert ov["role"] == ROLE_BRIDGE


def test_disagreement_keeps_reason_as_plain_prose():
    # 复核状态已升格为独立布尔（不再是 role_reason 的 [待复核] 前缀）：
    # 原因文本保持纯描述，两方观点并陈，标记只落在 needs_review 上。
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.5, "引用多个实体，疑似关联实体", needs_review=True)
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_TECHNICAL, "reason": "系统表"}
    )
    assert "[待复核]" not in out["role_reason"]
    assert "引用多个实体，疑似关联实体" in out["role_reason"]
    assert out["needs_review"] is True


def test_parse_role_overrides_captures_evidence_gap():
    from app.schemas import (
        DataHubDomainBundle,
        DatasetInput,
        DomainInput,
        FieldInput,
    )
    from app.services.evidence_builder import EvidenceBuilder

    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d1", name="域"),
        datasets=[
            DatasetInput(
                urn="urn:li:dataset:cfg",
                name="app_config",
                display_name="配置",
                fields=[FieldInput(name="k"), FieldInput(name="v")],
            ),
        ],
    )
    evidence = EvidenceBuilder().build(bundle)
    gen = OntologyDraftGenerator(runtime_config=None)
    raw = {
        "object_types": [
            {
                "source_ref": "urn:li:dataset:cfg",
                "role_hint": "technical",
                "role_reason": "配置表",
                "evidence_gap": "未开样例",
            }
        ]
    }
    overrides = gen._parse_role_overrides(raw, evidence)
    assert overrides
    ov = next(iter(overrides.values()))
    assert ov["role"] == "technical"
    assert ov["evidence_gap"] == "未开样例"


# --------------------------------------------------------------------------
# 弃权 ≠ 判定：启发式证据不足时兜底成业务对象，那不是一方观点，没有分歧可言。
# --------------------------------------------------------------------------


def test_abstained_heuristic_adopts_llm_without_review():
    """启发式弃权 + LLM 判非业务 → 直接采纳，不占人工队列。"""
    ot = _ot(
        ROLE_BUSINESS_OBJECT,
        0.55,
        "无主键（元数据缺失或非实体）；信号不足，暂按业务对象保留，待人工确认",
        needs_review=True,
        abstained=True,
    )
    out = OntologyDraftGenerator._resolve_role(
        ot,
        {
            "role": ROLE_TECHNICAL,
            "reason": "MySQL 系统库下的权限配置表",
            "evidence_gap": "无列注释",
        },
    )
    assert out["table_role"] == ROLE_TECHNICAL
    assert out["needs_review"] is False
    assert out["role_confidence"] == 0.7  # 单源结论：高于真分歧，低于两源互证
    assert "启发式证据不足未作判定" in out["role_reason"]
    assert "分歧" not in out["role_reason"]


def test_abstained_heuristic_still_reviews_promotion_to_business_object():
    """反方向不放行：提升为业务对象会被发布，必须有人点头。"""
    ot = _ot(
        ROLE_DATA_TABLE,
        0.55,
        "与业务图脱节且无显著业务信号，暂判数据表待人工确认",
        needs_review=True,
    )
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_BUSINESS_OBJECT, "reason": "客户主数据", "evidence_gap": None}
    )
    assert out["table_role"] == ROLE_BUSINESS_OBJECT
    assert out["needs_review"] is True


def test_nonbusiness_disagreement_resolved_without_human():
    """data_table ↔ technical 的分歧对发布零影响（只有业务对象会被发布）→ 不进队列。"""
    ot = _ot(ROLE_DATA_TABLE, 0.6, "与业务图脱节且无显著业务信号", needs_review=True)
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_TECHNICAL, "reason": "InnoDB 数据字典表", "evidence_gap": None}
    )
    assert out["table_role"] == ROLE_TECHNICAL
    assert out["needs_review"] is False
    assert "差异不影响发布" in out["role_reason"]


def test_nonbusiness_agreement_clears_heuristic_review_mark():
    """两个独立源都判非业务 → 互证，启发式自己挂的待复核可以撤掉。"""
    ot = _ot(
        ROLE_BRIDGE,
        0.6,
        "明细/子表（含 parent/parenttype 等子表锚点，隶属父表）",
        needs_review=True,
    )
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_BRIDGE, "reason": "每行是一次明细", "evidence_gap": None}
    )
    assert out["table_role"] == ROLE_BRIDGE
    assert out["needs_review"] is False
    assert "两源互证" in out["role_reason"]


def test_business_object_agreement_keeps_review_mark():
    """业务对象方向不因互证而免检：它是会被发布的那一类。"""
    ot = _ot(
        ROLE_BUSINESS_OBJECT,
        0.86,
        "引用 3 个不同实体但具自有属性/被引用，疑似关联实体，请确认为对象而非纯关系",
        needs_review=True,
    )
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_BUSINESS_OBJECT, "reason": "订单实体", "evidence_gap": None}
    )
    assert out["needs_review"] is True
