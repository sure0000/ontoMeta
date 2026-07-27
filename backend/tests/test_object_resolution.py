from app.services.object_resolution import (
    MergeCandidate,
    ObjectResolver,
    ResolutionInput,
    default_same_entity_judge,
)


def _o(id, name, fields, role="business_object", fk_in=0, certified=False, published=False):
    return ResolutionInput(
        id=id,
        name=name,
        display_name=name,
        business_fields=frozenset(fields),
        role=role,
        fk_in_degree=fk_in,
        is_certified=certified,
        is_published=published,
    )


def test_block_groups_identical_field_signature():
    inputs = [
        _o("u1", "dim_customer", {"cust_name", "level", "city"}),
        _o("u2", "customer_entity", {"cust_name", "level", "city"}),
        _o("u3", "orders", {"order_no", "amount"}),
    ]
    groups = ObjectResolver().block(inputs)
    assert len(groups) == 1
    assert {m.id for m in groups[0]} == {"u1", "u2"}


def test_block_groups_name_stem_variants():
    # stg_customer 与 dim_customer 同词干 + 高字段相似 → 同组。
    inputs = [
        _o("s", "stg_customer", {"cust_name", "level", "city", "phone"}),
        _o("d", "dim_customer", {"cust_name", "level", "city"}),
    ]
    groups = ObjectResolver().block(inputs)
    assert len(groups) == 1
    assert {m.id for m in groups[0]} == {"s", "d"}


def test_block_excludes_non_business_and_singletons():
    inputs = [
        _o("a", "config", {"k", "v"}, role="technical"),
        _o("b", "config2", {"k", "v"}, role="technical"),
        _o("c", "lonely", {"x", "y", "z"}),
    ]
    assert ObjectResolver().block(inputs) == []


def test_authority_certified_direct_select():
    group = [
        _o("hub", "customer", {"cust_name", "city"}, fk_in=9),
        _o("cert", "customer_master", {"cust_name"}, certified=True),
    ]
    canonical, margin, reason = ObjectResolver()._pick_canonical(group)
    assert canonical.id == "cert"
    assert "认证" in reason


def test_authority_stage_penalized_hub_wins():
    group = [
        _o("stg", "stg_customer", {"cust_name", "city", "level"}, fk_in=0),
        _o("dim", "dim_customer", {"cust_name", "city", "level"}, fk_in=6),
    ]
    canonical, margin, reason = ObjectResolver()._pick_canonical(group)
    assert canonical.id == "dim"  # hub + 分层加成 + stg 惩罚
    assert margin > 0


def test_resolve_all_need_review_and_exclude_canonical():
    inputs = [
        _o("dim", "dim_customer", {"cust_name", "city", "level"}, fk_in=6),
        _o("stg", "stg_customer", {"cust_name", "city", "level"}, fk_in=0),
    ]
    cands = ObjectResolver().resolve(inputs)
    assert len(cands) == 1
    c = cands[0]
    assert isinstance(c, MergeCandidate)
    assert c.needs_review is True
    assert c.canonical_id == "dim"
    assert c.member_ids == ["stg"]


def test_injectable_judge_can_reject():
    inputs = [
        _o("a", "customer_a", {"cust_name", "city"}),
        _o("b", "customer_b", {"cust_name", "city"}),
    ]
    resolver = ObjectResolver(judge=lambda g: (False, 0.0, "判非同一实体"))
    assert resolver.resolve(inputs) == []


def test_margin_gating_lowers_confidence_when_close():
    # 两个几乎等分的成员 → margin 不足 → 置信度被压低。
    inputs = [
        _o("a", "customer_x", {"cust_name", "city", "level"}, fk_in=2),
        _o("b", "customer_y", {"cust_name", "city", "level"}, fk_in=2),
    ]
    cands = ObjectResolver(margin_threshold=1.0).resolve(inputs)
    assert len(cands) == 1
    assert cands[0].confidence <= 0.6


def test_default_judge_identical_signature_high_conf():
    group = [
        _o("a", "customer", {"cust_name", "city"}),
        _o("b", "customer2", {"cust_name", "city"}),
    ]
    same, conf, reason = default_same_entity_judge(group)
    assert same is True
    assert conf >= 0.8
