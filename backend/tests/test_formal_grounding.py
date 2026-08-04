"""F4：事实账本 + 答案可靠性校验器单元测试。

核心指标：答案里出现账本外的具名实体、或未经查询证实的数值 → 判不可靠（触发拒答）；
真实接地的答案与噪声数值不被误杀。
"""

from __future__ import annotations

from app.services.agent_grounding import FactLedger
from app.services.answer_verifier import verify_answer


def _ledger_with_order() -> FactLedger:
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


def test_hallucinated_entity_flagged():
    led = _ledger_with_order()
    # 「毛利率」不在账本
    v = verify_answer("订单的「毛利率」约为 30%。", led, strict_numbers=False)
    assert not v.ok
    assert any("毛利率" in u for u in v.unverified)


def test_grounded_entity_passes():
    led = _ledger_with_order()
    v = verify_answer("「订单」对象包含 `amount` 字段。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_fabricated_number_flagged():
    led = _ledger_with_order()  # 无 run_sql cells
    v = verify_answer("共有 1234 单。", led, strict_numbers=True)
    assert not v.ok
    assert any("1234" in u for u in v.unverified)


def test_number_from_cells_passes():
    led = _ledger_with_order()
    led.add_cells(["cnt"], [{"cnt": 1234}])
    v = verify_answer("共有 1234 单。", led, strict_numbers=True)
    assert v.ok, v.unverified


def test_noise_number_not_flagged():
    led = _ledger_with_order()
    # 「近 7 天」「第 2 步」是噪声，不要求凭证
    v = verify_answer("请看近 7 天数据；第 2 步执行查询。", led, strict_numbers=True)
    assert v.ok, v.unverified


def test_number_in_code_block_ignored():
    led = _ledger_with_order()
    v = verify_answer("建议 SQL：\n```sql\nSELECT 999 FROM order\n```\n以上供参考。", led, strict_numbers=True)
    assert v.ok, v.unverified


def test_ledger_numeric_normalization():
    led = FactLedger()
    led.add_cells(["total"], [{"total": 1234.50}])
    assert led.has_numeric("1234.5")
    assert led.has_numeric("1,234.5")
    led2 = FactLedger()
    led2.add_cells(["c"], [{"c": 12}])
    assert led2.has_numeric("12")


def test_empty_answer_ok():
    assert verify_answer("", _ledger_with_order()).ok
