"""存量重算脚本的判定逻辑（纯函数部分，不碰库）。

这三条规则会**清除**人工待复核标记，所以两个方向都得钉住：该结案的结案，
不该结案的（尤其是任何指向 business_object 的方向）一条都不许放行。
"""

from types import SimpleNamespace

from app.services.object_classifier import (
    ROLE_BRIDGE,
    ROLE_BUSINESS_OBJECT,
    ROLE_DATA_TABLE,
    ROLE_TECHNICAL,
)
from scripts.backfill_role_arbitration import parse_disagreement, replay


def _obj(role, reason, needs_review=True, signals=None):
    return SimpleNamespace(
        id="o1",
        name="t",
        table_role=role,
        role_confidence=0.5,
        role_reason=reason,
        needs_review=needs_review,
        role_signals=signals,
    )


def test_parse_disagreement_reads_both_sides():
    reason = (
        "启发式↔LLM 角色分歧：LLM 判为技术/系统表（系统表）；"
        "启发式判为data_table（与业务图脱节）；证据缺口：无列注释"
    )
    assert parse_disagreement(reason) == (ROLE_DATA_TABLE, ROLE_TECHNICAL)


def test_parse_disagreement_ignores_non_disagreement():
    assert parse_disagreement("单列业务主键，具备独立身份") is None


def test_nonbusiness_disagreement_is_closed():
    obj = _obj(
        ROLE_TECHNICAL,
        "启发式↔LLM 角色分歧：LLM 判为技术/系统表；启发式判为data_table（孤岛）",
    )
    bucket, updates = replay(obj, set())
    assert "R2" in bucket
    assert updates["needs_review"] is False
    assert updates["role_confidence"] == 0.7


def test_abstained_heuristic_is_closed():
    obj = _obj(
        ROLE_TECHNICAL,
        "启发式↔LLM 角色分歧：LLM 判为技术/系统表；"
        "启发式判为业务对象（无主键；信号不足，暂按业务对象保留，待人工确认）",
    )
    bucket, updates = replay(obj, set())
    assert "R1" in bucket
    assert updates["needs_review"] is False


def test_child_anchor_downgrades_the_heuristic_side():
    """子表锚点在场：启发式那一侧本不该是业务对象 → 分歧降级为非业务分歧 → 结案。"""
    obj = _obj(
        ROLE_BRIDGE,
        "启发式↔LLM 角色分歧：LLM 判为业务事实/关系表；"
        "启发式判为业务对象（关系表未能塌缩为业务关系，智能重判为业务对象：单列业务主键）",
    )
    assert replay(obj, {"item_code"}) is None  # 没有锚点 → 真分歧，留人
    bucket, updates = replay(obj, {"parent", "parenttype", "parentfield"})
    assert bucket.startswith("R3")
    assert updates["needs_review"] is False


def test_promotion_to_business_object_never_auto_closed():
    obj = _obj(
        ROLE_BUSINESS_OBJECT,
        "启发式↔LLM 角色分歧：LLM 判为业务对象；启发式判为data_table（孤岛）",
    )
    assert replay(obj, set()) is None


def test_real_cross_boundary_disagreement_kept():
    """启发式有真证据说它是业务对象、LLM 说技术表 → 必须留给人。"""
    obj = _obj(
        ROLE_TECHNICAL,
        "启发式↔LLM 角色分歧：LLM 判为技术/系统表（MySQL 事件调度表）；"
        "启发式判为业务对象（单列业务主键，具备独立身份；被 75 张表外键引用）",
    )
    assert replay(obj, set()) is None


def test_child_promoted_object_is_demoted_but_stays_in_queue():
    """无 LLM 背书的子表误提：改回数据表，但保留待复核（只有一个源）。"""
    obj = _obj(
        ROLE_BUSINESS_OBJECT,
        "关系表未能塌缩为业务关系（连不到两个业务对象），智能重判为业务对象：单列业务主键",
        signals={"reclassified_from": "bridge"},
    )
    bucket, updates = replay(obj, {"parent", "parenttype"})
    assert updates["table_role"] == ROLE_DATA_TABLE
    assert "needs_review" not in updates  # 不结案
    assert "仍待复核" in bucket


def test_already_reviewed_rows_untouched():
    obj = _obj(ROLE_TECHNICAL, "任何理由", needs_review=False)
    assert replay(obj, set()) is None
