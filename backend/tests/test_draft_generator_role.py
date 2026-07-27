from types import SimpleNamespace

from app.services.draft_generator import OntologyDraftGenerator
from app.services.object_classifier import ROLE_BUSINESS_OBJECT, ROLE_TECHNICAL


def _ot(role, conf, reason):
    return SimpleNamespace(table_role=role, role_confidence=conf, role_reason=reason)


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
    # 一致：互证，保留启发式，不改置信度、不加待复核。
    assert out["table_role"] == ROLE_TECHNICAL
    assert out["role_confidence"] == 0.9
    assert "[待复核]" not in out["role_reason"]


def test_invalid_role_hint_ignored():
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.8, "reason")
    out = OntologyDraftGenerator._resolve_role(ot, {"role": "garbage"})
    assert out["table_role"] == ROLE_BUSINESS_OBJECT
    assert out["role_confidence"] == 0.8


def test_disagreement_flags_needs_review_and_lowers_confidence():
    # 启发式=业务对象，LLM=技术表 → 分歧：标记待复核、下调置信度、并陈两方观点。
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.55, "信号不足，暂按业务对象保留")
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
    assert out["role_reason"].startswith("[待复核]")
    assert "LLM 判为技术/系统表" in out["role_reason"]
    assert "启发式判为业务对象" in out["role_reason"]
    assert "证据缺口：无列注释" in out["role_reason"]


def test_disagreement_strips_nested_review_prefix():
    # 启发式原因本就带 [待复核] 前缀时，并陈时不应重复嵌套该前缀。
    ot = _ot(ROLE_BUSINESS_OBJECT, 0.5, "[待复核] 引用多个实体，疑似关联实体")
    out = OntologyDraftGenerator._resolve_role(
        ot, {"role": ROLE_TECHNICAL, "reason": "系统表"}
    )
    # 仅开头一个 [待复核]
    assert out["role_reason"].count("[待复核]") == 1


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
