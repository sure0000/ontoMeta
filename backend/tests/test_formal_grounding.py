"""F4：事实账本 + 答案可靠性校验器单元测试。

核心指标：答案里出现账本外的具名实体、或未经查询证实的数值 → 判不可靠（触发拒答）；
真实接地的答案与噪声数值不被误杀。
"""

from __future__ import annotations

from app.services.agent_grounding import FactLedger
from app.services.answer_verifier import verify_answer
from app.services.chat_bi import _AGENT_TOOL_SCHEMAS


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


def test_relation_from_overview_passes():
    """get_domain_overview 现在登记已发布关系：概览/列举类问题引用关系名不再被拒答。"""
    led = _ledger_with_order()
    led.add_relation({"id": "r1", "name": "order_customer", "display_name": "下单客户"})
    v = verify_answer("当前数据域存在关系「下单客户」。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_context_domain_name_not_flagged():
    """植入的当前数据域名是可信上下文，答案引用时不得被判为幻觉实体。"""
    led = _ledger_with_order()
    led.add_context_name("销售域")
    v = verify_answer("数据域「销售域」已发布的关系涉及「订单」对象。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_tool_names_not_flagged():
    """复现拒答场景：模型在正文提到工具名（get_domain_overview/search_relations）时不应被拒答。"""
    led = _ledger_with_order()
    led.add_context_name(*[t["function"]["name"] for t in _AGENT_TOOL_SCHEMAS])
    v = verify_answer(
        "我通过「get_domain_overview」与「search_relations」检索到当前数据域的已发布关系。",
        led,
        strict_numbers=False,
    )
    assert v.ok, v.unverified


def test_enumeration_answer_end_to_end_not_refused():
    """端到端复现『有哪些已发布的本体有关系？』：账本按真实流程植入
    （数据域名 + 工具名 + 概览关系），答案列举关系显示名 + 顺带提到工具名，不应拒答。"""
    led = _ledger_with_order()
    led.add_context_name("数据域-ERP-全量")
    led.add_context_name(*[t["function"]["name"] for t in _AGENT_TOOL_SCHEMAS])
    # get_domain_overview 现在会登记关系
    for r in [
        {"id": "r1", "name": "order_customer", "display_name": "下单客户",
         "source_object_name": "订单", "target_object_name": "客户"},
        {"id": "r2", "name": "order_item", "display_name": "订单明细",
         "source_object_name": "订单", "target_object_name": "商品"},
    ]:
        led.add_relation(r)
    answer = (
        "数据域「数据域-ERP-全量」已发布若干关系，例如「下单客户」（连接「订单」与「客户」）"
        "和「订单明细」。以上通过「get_domain_overview」检索得到。"
    )
    v = verify_answer(answer, led, strict_numbers=False)
    assert v.ok, v.unverified


# ---------------------------------------------------------------------------
# V5 F5：抽取精度。以下四类是 ERP 域 24 轮实测里**全部** 4 次拒答的成因，
# 每一条都取自真实拒答文案的原文——无一是真幻觉。
# ---------------------------------------------------------------------------


def test_f5_field_list_in_one_quote_is_split_and_verified():
    """实测原文：「文档状态、序号、成品物料…」——字段清单挤在同一对括号里。

    整串当一个名字核必然核不到，可每一段都在账本里。
    """
    led = _ledger_with_order()
    v = verify_answer("该对象包含「金额、状态」两个关键字段。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_f5_lineage_path_arrow_chain_is_split():
    """实测原文：「供应商 → 采购订单 → 采购收货 → 采购发票」——血缘路径。"""
    led = _ledger_with_order()
    v = verify_answer("链路为「订单 → 金额」。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_f5_composite_still_refuses_when_one_part_is_hallucinated():
    """拆开不等于放行：只要有一段不可证，照旧拒答——精度提高，强度不降。"""
    led = _ledger_with_order()
    v = verify_answer("包含「金额、毛利率」两个字段。", led, strict_numbers=False)
    assert not v.ok
    assert any("毛利率" in u for u in v.unverified)


def test_f5_sql_fragment_in_quotes_is_not_an_entity_claim():
    """实测原文：「SELECT COUNT(*) FROM sales_order」。SQL 的正确性归 F3 语义证明器管。"""
    led = _ledger_with_order()
    v = verify_answer("我执行的是「SELECT COUNT(*) FROM order」。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_f5_question_echo_is_not_an_entity_claim():
    """实测原文：「按客户统计销售订单总金额、取前 30 名」——把用户的问题原样引了一遍。"""
    led = _ledger_with_order()
    q = "按客户统计销售订单总金额，取前 30 名"
    v = verify_answer(
        f"你问的是「{q}」，我来查。", led, strict_numbers=False, question=q
    )
    assert v.ok, v.unverified


def test_f5_number_echoed_from_question_needs_no_query_proof():
    """实测原文：数值 `50个` 来自问句「取前 50 个科目」，不是模型算出来的结论值。"""
    led = _ledger_with_order()
    v = verify_answer(
        "已按你要的前 50 个科目统计。", led, strict_numbers=True,
        question="总账分录按科目统计条数，取前 50 个科目",
    )
    assert v.ok, v.unverified


def test_f5_number_not_in_question_still_needs_proof():
    """真结论值仍要凭证：问句里没有的数字，编出来照样拦。"""
    led = _ledger_with_order()
    v = verify_answer(
        "共有 73912 条记录。", led, strict_numbers=True, question="销售订单有多少条？"
    )
    assert not v.ok
    assert any("73912" in u for u in v.unverified)


def test_f5_hallucinated_entity_still_flagged_even_with_question_passed():
    """传了 question 也不放过真幻觉——豁免只覆盖「用户自己写过的字眼」。"""
    led = _ledger_with_order()
    v = verify_answer(
        "订单的「毛利率」是核心指标。", led, strict_numbers=False, question="订单有哪些字段？"
    )
    assert not v.ok
    assert any("毛利率" in u for u in v.unverified)


def test_f5_short_token_not_exempted_by_question_echo():
    """短片段不吃复述豁免：「毛利率」即便出现在问句里，仍按实体核。"""
    led = _ledger_with_order()
    v = verify_answer(
        "「毛利率」在本域有定义。", led, strict_numbers=False, question="毛利率是多少？"
    )
    assert not v.ok


def test_f5_echo_corpus_covers_prior_turns():
    """复述豁免要覆盖多轮：第 6 轮引第 5 轮的问句，同样不是模型编的。

    实测原文：「总账分录按科目统计条数、取前 50」——上一轮的提问，
    只看本轮问句就漏了，于是长会话越往后越容易被误拒。
    """
    from app.services.chat_bi import ChatBiService

    led = _ledger_with_order()
    history = [
        {"role": "user", "content": "总账分录按科目统计条数，取前 50 个科目"},
        {"role": "assistant", "content": "（上一轮的回答）"},
    ]
    echo = ChatBiService._echo_corpus("排在第 12 到第 20 位的是哪些？", history)
    v = verify_answer(
        "承接「总账分录按科目统计条数、取前 50」那份结果。",
        led, strict_numbers=False, question=echo,
    )
    assert v.ok, v.unverified


def test_f5_echo_corpus_ignores_assistant_turns():
    """只豁免**用户**说过的话：模型自己上一轮编的实体，这一轮复读照样要拦。"""
    from app.services.chat_bi import ChatBiService

    led = _ledger_with_order()
    history = [{"role": "assistant", "content": "订单的毛利率是核心指标之一"}]
    echo = ChatBiService._echo_corpus("订单有哪些字段？", history)
    v = verify_answer(
        "如上一轮所说，「订单的毛利率是核心指标」。", led, strict_numbers=False, question=echo
    )
    assert not v.ok


def test_f5_clause_paraphrase_is_not_an_entity_claim():
    """根因：「」在中文里是引用任意短语的括号，不只标实体名。

    实测原文（模型用自己的话转述任务，非逐字复述，故子串豁免够不着）：
      「各订单状态分别有多少条记录」「按客户汇总销售订单金额」
    """
    led = _ledger_with_order()
    for token in (
        "各订单状态分别有多少条记录",
        "按客户汇总销售订单金额",
        "销售订单一共有多少条记录",
        "根据状态筛选出来的那批",
    ):
        v = verify_answer(f"我来算「{token}」。", led, strict_numbers=False)
        assert v.ok, (token, v.unverified)


def test_f5_clause_rule_does_not_swallow_real_entity_names():
    """判别刻意留窄：正常的实体名（短名词短语）照旧要核，编的照旧拦。"""
    led = _ledger_with_order()
    for token in ("毛利率", "客户分层", "退款流水表"):
        v = verify_answer(f"「{token}」是核心指标。", led, strict_numbers=False)
        assert not v.ok, token
        assert any(token in u for u in v.unverified)


# ---------------------------------------------------------------------------
# 口径断言接地（收口缺口 C）：散文里「<指标> 定义为/计算为/口径为/口径是 …」
# 若指标名不在账本 → 编造口径，即便**没加任何标记**也要拦。
# 反向：账本内指标、指代主语、非指标语境的「口径/等于」不得误伤。
# ---------------------------------------------------------------------------


def test_caliber_prose_definition_of_offledger_metric_flagged():
    """无标记的「毛利率定义为…」——毛利率不在账本，判编造口径。"""
    led = _ledger_with_order()
    v = verify_answer(
        "毛利率定义为毛利除以营业收入，是衡量盈利能力的核心口径。",
        led, strict_numbers=False,
    )
    assert not v.ok
    assert any("毛利率" in u for u in v.unverified)


def test_caliber_prose_various_verbs_flagged():
    """定义为 / 计算为 / 口径为 / 口径是 四种定义式动词都要生效。"""
    led = _ledger_with_order()
    for sent in (
        "客单价计算为销售额除以订单数。",
        "复购率口径为复购用户数占总用户数的比例。",
        "留存率口径是次月仍活跃的用户占比。",
    ):
        v = verify_answer(sent, led, strict_numbers=False)
        assert not v.ok, sent


def test_caliber_subject_strips_de_prefix():
    """「订单的毛利率定义为…」被定义的是毛利率（账本外）而非订单，须剥前缀后核。"""
    led = _ledger_with_order()
    v = verify_answer("订单的毛利率定义为毛利除以营收。", led, strict_numbers=False)
    assert not v.ok
    assert any("毛利率" in u for u in v.unverified)


def test_caliber_grounded_metric_not_flagged():
    """账本内指标的口径定义（哪怕未加标记）不误伤——毛利率此处在账本。"""
    led = _ledger_with_order()
    led.add_metric_summary({"id": "m1", "name": "gross_margin", "display_name": "毛利率"})
    v = verify_answer("毛利率定义为毛利除以营业收入。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_caliber_marked_metric_reported_once():
    """加了书名号的账本外口径名仍被拦，但只报一次（不因口径通道重复计数）。"""
    led = _ledger_with_order()
    v = verify_answer("「毛利率」定义为毛利除以营业收入。", led, strict_numbers=False)
    assert not v.ok
    assert sum("毛利率" in u for u in v.unverified) == 1


def test_caliber_pronoun_subject_not_flagged():
    """指代/泛称主语（该指标/该口径）不是具名断言，不核。"""
    led = _ledger_with_order()
    for sent in (
        "该指标定义为毛利除以营收。",
        "其口径为按自然月汇总。",
        "上述口径是以本体为准。",
    ):
        v = verify_answer(sent, led, strict_numbers=False)
        assert v.ok, (sent, v.unverified)


def test_caliber_non_definition_context_not_flagged():
    """裸「口径」「等于」不触发：'统计口径以本体为准'、'总数等于…' 非指标定义。"""
    led = _ledger_with_order()
    for sent in (
        "口径以已发布本体为准，请勿自行重写。",
        "统计口径需与业务方确认。",
        "请注意各系统口径可能不一致。",
    ):
        v = verify_answer(sent, led, strict_numbers=False)
        assert v.ok, (sent, v.unverified)


def test_caliber_definition_echoing_question_not_flagged():
    """用户问句里就写了这个口径名 → 复述不算模型编造。"""
    led = _ledger_with_order()
    v = verify_answer(
        "你问的动销率，通常定义为动销 SKU 数占比。",
        led, strict_numbers=False, question="动销率是怎么定义的？",
    )
    assert v.ok, v.unverified


# ---------------------------------------------------------------------------
# 关联键断言接地（收口缺口 A 的高危子类）：散文里「通过 X 关联」「外键为 X」
# 若 X 长得像键名却不在账本 → 编造连接键（会诱导用户写出错 SQL），即便无标记也拦。
# 反向：真键、泛称键词、动词、对象名级关系不得误伤。
# ---------------------------------------------------------------------------


def test_joinkey_fabricated_key_in_prose_flagged():
    """「通过客户编号进行关联」——客户编号不在账本（真字段是 customer_id）→ 拦。"""
    led = _ledger_with_order()
    v = verify_answer("订单表和客户表可以通过客户编号进行关联查询。", led, strict_numbers=False)
    assert not v.ok
    assert any("客户编号" in u for u in v.unverified)


def test_joinkey_explicit_declaration_flagged():
    """显式「外键为 X / 关联键是 X」形式同样生效。"""
    led = _ledger_with_order()
    for sent in ("两表的外键为 buyer_id。", "关联键是 cust_code。"):
        v = verify_answer(sent, led, strict_numbers=False)
        assert not v.ok, sent


def test_joinkey_real_key_not_flagged():
    """账本内的真连接键（未加标记）不误伤——customer_id 在账本。"""
    led = _ledger_with_order()
    led.add_object_detail({
        "id": "o1", "name": "order", "display_name": "订单",
        "properties": [{"name": "customer_id", "display_name": "客户ID",
                        "semantic_type": "identifier"}],
        "relations": [],
    })
    v = verify_answer("订单表通过 customer_id 关联客户表。", led, strict_numbers=False)
    assert v.ok, v.unverified


def test_joinkey_generic_keyword_not_flagged():
    """泛称键词（外键/主键/关联键）不是具体字段名，不核。"""
    led = _ledger_with_order()
    for sent in ("两张表通过外键关联。", "它们的关联键是主键。"):
        v = verify_answer(sent, led, strict_numbers=False)
        assert v.ok, (sent, v.unverified)


def test_joinkey_verb_after_tongguo_not_flagged():
    """「通过分析/计算/对比 … 关联」里的动词不长得像键名 → 不误当连接键。"""
    led = _ledger_with_order()
    for sent in (
        "通过分析可以关联出用户偏好。",
        "通过计算得到各维度的关联度。",
        "通过对比不同渠道来匹配最优方案。",
    ):
        v = verify_answer(sent, led, strict_numbers=False)
        assert v.ok, (sent, v.unverified)


# ---------------------------------------------------------------------------
# 百分比/倍数数值接地（收口缺口 D 的低噪子类）：%/‰/倍/百分点是结论统计量，
# 无 run_sql 凭证即编造。反向：来自单元格的百分比、问句复述的比例不误伤。
# ---------------------------------------------------------------------------


def test_pct_fabricated_percentage_flagged():
    led = _ledger_with_order()  # 无 cells
    v = verify_answer("订单的退款率约为 18%。", led, strict_numbers=True)
    assert not v.ok
    assert any("18" in u for u in v.unverified)


def test_pct_fabricated_multiple_flagged():
    led = _ledger_with_order()
    v = verify_answer("今年销量增长了 3 倍。", led, strict_numbers=True)
    assert not v.ok
    assert any("3" in u for u in v.unverified)


def test_pct_percentage_from_cells_passes():
    led = _ledger_with_order()
    led.add_cells(["rate"], [{"rate": 18}])
    v = verify_answer("退款率为 18%。", led, strict_numbers=True)
    assert v.ok, v.unverified


def test_pct_echoed_from_question_not_flagged():
    """用户问句里给定的比例阈值（复述）不要求凭证。"""
    led = _ledger_with_order()
    v = verify_answer(
        "已按你说的 20% 阈值筛选。", led, strict_numbers=True,
        question="退款率超过 20% 的订单有哪些？",
    )
    assert v.ok, v.unverified


