"""F4 缺口的特征化测试：**大白话编造**能不能被答案核对器放过。

结论（本文件所断言的现状）：能。`verify_answer` 只抽取两类断言——
反引号/「」里的**具名实体**、带业务单位的**数值**——逐条对账本核。
凡是「不带标记的自然语言业务推理/关系/口径解释」都不在抽取范围内，
因此即便本体里毫无依据，也照样通过校验（v.ok == True）。

这些测试**故意断言当前的漏行为**（assert v.ok），作为可执行的漏洞证据：
- 它们现在应当全部通过（证明漏洞真实存在）；
- 将来若收紧核对器把某类漏洞堵上，对应用例会转红——那正是"缺口已收口"的信号，
  届时把该条断言翻成 `assert not v.ok` 并移出本文件即可。

与 test_formal_grounding.py 的区别：那里每个被拦的幻觉都带 `` ` `` 或「」标记，
或带业务单位的数字；这里正相反，全是**无标记的散文**。
"""

from __future__ import annotations

from app.services.agent_grounding import FactLedger
from app.services.answer_verifier import verify_answer


def _ledger_with_order() -> FactLedger:
    """只登记「订单」对象及其 金额/状态 两个字段——与 test_formal_grounding 同构。

    账本里**没有**：客户、库存、退款、毛利率，也没有任何 run_sql 单元格。
    下面每条答案凭空引入这些，看核对器拦不拦得住。
    """
    led = FactLedger()
    led.add_object_detail(
        {
            "id": "o1", "name": "order", "display_name": "订单",
            "properties": [
                {"name": "amount", "display_name": "金额", "semantic_type": "measure"},
                {"name": "status", "display_name": "状态", "semantic_type": "categorical"},
            ],
            "relations": [],
        }
    )
    return led


# ---------------------------------------------------------------------------
# 缺口 A：无标记的业务关系。**对象名级**的散文关系（订单关联客户、影响库存）仍开放——
# 无分词器无法可靠抽取实体名，强行正则必误伤。**关联键子类**（通过 X 关联 / 外键为 X）
# 已收口 → 迁至 test_formal_grounding.py::test_joinkey_*。
# ---------------------------------------------------------------------------


def test_gap_plain_prose_relation_slips_through():
    led = _ledger_with_order()
    answer = "订单通常会关联到客户，并且会直接影响库存水平。"  # 客户/库存均不在账本
    v = verify_answer(answer, led, strict_numbers=False)
    assert v.ok, v.unverified  # 现状：漏过（对象名级散文关系，仍开放）


def test_gap_marked_up_version_of_same_claim_IS_caught():
    """对照组：**同一句**只要给编造实体加上标记，立刻被拦——

    证明剩余漏洞的成因恰恰是「有没有标记」，而非语义。守卫的抽取面对无标记散文太窄。
    """
    led = _ledger_with_order()
    v = verify_answer("订单可通过 `客户编号` 关联到「客户」。", led, strict_numbers=False)
    assert not v.ok
    assert any("客户" in u for u in v.unverified)


# ---------------------------------------------------------------------------
# 缺口 B：无标记的常识性因果/业务推断。纯粹从常识生成、与本体无关，
# 没有具名实体也没有带单位的数字 → 完全不在抽取范围。
# ---------------------------------------------------------------------------


def test_gap_commonsense_causal_reasoning_slips_through():
    led = _ledger_with_order()
    answer = "一般来说，退款金额越高，客户流失的可能性就越大，应重点关注高退款客户。"
    v = verify_answer(answer, led, strict_numbers=False)
    assert v.ok, v.unverified  # 现状：漏过（缺口）


def test_gap_invented_business_advice_slips_through():
    led = _ledger_with_order()
    answer = "建议按周对订单做同比分析，因为零售业务通常存在明显的周末峰值。"
    v = verify_answer(answer, led, strict_numbers=False)
    assert v.ok, v.unverified  # 现状：漏过（缺口）


# ---------------------------------------------------------------------------
# 缺口 C（口径定义）已收口 → 迁至 test_formal_grounding.py 作为回归测试
# （test_caliber_* 系列）。此处不再保留。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 缺口 D：无业务单位的裸数字。%/‰/倍/百分点已纳入核对 → 迁至 test_formal_grounding.py
# ::test_pct_*。**纯无单位裸数**（0.35 / 12）仍开放：计数/ID/月份满地都是，收了必误报。
# ---------------------------------------------------------------------------


def test_gap_unitless_ratio_slips_through():
    led = _ledger_with_order()  # 无任何 run_sql 单元格
    answer = "订单的平均复购率大约是 0.35，退货率约为 12。"  # 无「%」无业务单位
    v = verify_answer(answer, led, strict_numbers=True)
    assert v.ok, v.unverified  # 现状：漏过（无单位裸数，仍开放）


def test_gap_same_number_with_unit_IS_caught():
    """对照组：同样是编的数字，一旦带上业务单位「单」，立刻要凭证并被拦。"""
    led = _ledger_with_order()
    v = verify_answer("共有 35 单未支付。", led, strict_numbers=True)
    assert not v.ok
    assert any("35" in u for u in v.unverified)
