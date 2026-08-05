"""G0 规约平移的忠实性测试。

内置默认规约里 ``enforced=True`` 的字面值，必须与它们各自的 source-of-truth **逐字节一致**——
否则 G1 把规约接进闸门时就会改变行为。这些断言把「平移」钉死，任一处漂移立即红。
"""

from __future__ import annotations

from app.agents.validation import _WARNING_CODES
from app.governance import DEFAULT_STANDARD, active_standard
from app.models.warehouse import LoadStrategy, MaterializationLayer


def test_required_fields_match_expected_contract():
    """必填字段规约钉在独立的期望映射上（防止规约里手滑打错字段名）。

    G1 起 Validation Gate 直接读规约的 per_artifact，故这里不再与闸门里的副本比对
    （副本已删），改为对齐一份显式契约——任一制品的必填集变化都必须在此显式改。
    """
    expected = {
        "cluster": ("hosts", "services"),
        "sync": ("source", "target"),
        "transform": ("target_table", "ontology_id"),
        "metric": ("metric_name",),
        "materialize": ("ontology_id", "target_datasource_id"),
    }
    assert dict(DEFAULT_STANDARD.required_metadata.per_artifact) == expected


def test_layers_match_warehouse_enum():
    std_layers = set(DEFAULT_STANDARD.layering.layers)
    enum_layers = {m.value for m in MaterializationLayer}
    assert std_layers == enum_layers


def test_layering_targets_are_valid_layers():
    """所有落层映射的目标层必须是合法层，且指标层同 enum。"""
    layers = set(DEFAULT_STANDARD.layering.layers)
    for target in DEFAULT_STANDARD.layering.role_to_layer.values():
        assert target in layers
    for target in DEFAULT_STANDARD.layering.structure_to_layer.values():
        assert target in layers
    assert DEFAULT_STANDARD.layering.business_logic_layer == MaterializationLayer.ADS.value


def test_default_load_strategy_matches_enum():
    assert DEFAULT_STANDARD.tasks.default_load_strategy == LoadStrategy.FULL.value


def test_credential_tokens_cover_validation_check():
    """凭据词元须覆盖 validation.py:196-214 的检查集合。"""
    assert set(DEFAULT_STANDARD.security.forbidden_tokens) == {
        "password", "secret", "token", "private_key", "credential",
    }
    assert set(DEFAULT_STANDARD.security.allowed_ref_suffixes) == {"_ref", "_alias"}


def test_enforced_rules_reuse_existing_codes():
    """enforced=True 的平移规则必须沿用既有 ValidationIssue code（护住拒绝码分布）。

    既有 error 码里被规约平移的两条：missing_required_field、credential_in_spec。
    它们都不在 warning 白名单里，接线后仍应是 error 级。
    """
    enforced_codes = {r.code for r in DEFAULT_STANDARD.enforced_rules()}
    assert "missing_required_field" in enforced_codes
    assert "credential_in_spec" in enforced_codes
    for code in enforced_codes:
        assert code not in _WARNING_CODES  # enforced=error，不得落进 warning 白名单


def test_advisory_rules_not_enforced_in_g0():
    """G0 新增约束一律 advisory：接闸门前不得有任何 enforced 的新码。

    只允许 missing_required_field / credential_in_spec 两条既有码是 enforced。"""
    enforced_codes = {r.code for r in DEFAULT_STANDARD.enforced_rules()}
    assert enforced_codes == {"missing_required_field", "credential_in_spec"}


def test_active_standard_returns_default():
    assert active_standard() is DEFAULT_STANDARD
    assert active_standard(db=None) is DEFAULT_STANDARD


def test_prompt_card_is_compact():
    """约束卡是给 prompt 的，必须简短（不倾倒整份 JSON）。"""
    card = DEFAULT_STANDARD.compile_prompt_card()
    assert f"v{DEFAULT_STANDARD.version}" in card
    assert len(card) < 800  # 简短底线
    assert "dim" in card and "decimal" in card


def test_all_rules_have_unique_codes():
    codes = [r.code for r in DEFAULT_STANDARD.all_rules()]
    # missing_required_field 只声明一条规则，其余各不相同
    assert len(codes) == len(set(codes))
