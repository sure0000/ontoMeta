"""启发式 filter 识别端到端测试。

模拟 LLM 漏识别场景:LLM 只产出 args/group_by,filter=null,
看后端 `_normalize_llm_output` 能否从自然语言文本里把条件救回来。

运行:
    cd backend && python scripts/test_heuristic_filter.py
"""

from __future__ import annotations

from app.services.expression_formatter import ExpressionFormatterService, _mock_format


def make_segments(text_refs: list[tuple[str, str | None]]):
    """text_refs: [(text_or_None, ref_id_or_None), ...]"""
    segs = []
    for text, rid in text_refs:
        if rid is None:
            if text:
                segs.append({"type": "text", "value": text})
        else:
            segs.append({"type": "ref", "ref_id": rid})
    return segs


def make_refs(ids: list[str]) -> list[dict]:
    return [{"ref_id": rid} for rid in ids]


def simulate_llm_no_filter(args_ids: list[str], group_by_ids: list[str] | None = None) -> dict:
    """模拟 LLM 漏识别 filter 的产出。"""
    return {
        "type": "metric",
        "description": "",
        "refs": [{"ref_id": rid} for rid in args_ids + (group_by_ids or [])],
        "body": {
            "operation": "sum",
            "args": [{"ref": r} for r in args_ids],
            "filter": None,
            "group_by": [{"ref": r} for r in (group_by_ids or [])],
            "window": None,
        },
    }


# 测试用例:(描述, segments 构造, refs, LLM 模拟产出, 期望 filter, 期望 op, 期望 right_value)
CASES = [
    (
        "伪代码 WHERE + 引号字符串",
        [("统计 SUM(", None), (None, "r1"), (") WHERE ", None), (None, "r2"),
         (" = '已支付'", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "已支付",
    ),
    (
        "自然语言:大于 2026年7月(无空格)",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 大于 2026年7月", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">", "2026-07",
    ),
    (
        "自然语言:大于 2026 年 7 月(带空格)",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 大于 2026 年 7 月", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">", "2026-07",
    ),
    (
        "自然语言:大于 2026 年 7 月 5 日(带空格带日)",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 大于 2026 年 7 月 5 日", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">", "2026-07-05",
    ),
    (
        "自然语言:超过 1000 元",
        [("求 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" 超过 1000 元", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">", 1000,
    ),
    (
        "自然语言:为「已支付」",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" 为「已支付」", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "已支付",
    ),
    (
        "自然语言:不低于 100",
        [("求 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 不低于 100", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">=", 100,
    ),
    (
        "自然语言:不高于 5000",
        [("求 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 不高于 5000", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "<=", 5000,
    ),
    (
        "自然语言:不少于 3 次",
        [("统计 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 不少于 3 次", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">=", 3,
    ),
    (
        "自然语言:不超过 5000",
        [("求 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 不超过 5000", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "<=", 5000,
    ),
    (
        "自然语言:不等于 已取消",
        [("统计 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 不等于 已取消", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "!=", "已取消",
    ),
    (
        "自然语言:包含 北京",
        [("统计 ", None), (None, "r1"), (",", None), (None, "r2"),
         (" 包含 北京", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "like", "北京",
    ),
    (
        "自然语言:其中(无显式比较,仅有其中关键词 + 未使用 ref + 裸值)",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" 为高优先级", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "高优先级",
    ),
    (
        "多条件:状态=已支付 且 金额>1000",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" = '已支付' 且 ", None), (None, "r3"), (" 超过 1000", None)],
        ["r1", "r2", "r3"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "and", None,
    ),
    (
        "无 ref 紧跟 op 的隐式过滤(纯文本描述,无法识别)",
        [("统计已支付订单的 ", None), (None, "r1"), (" 总和", None)],
        ["r1"],
        simulate_llm_no_filter(["r1"]),
        "no_filter", None, None,
    ),
    (
        "LLM 产出 filter 引用了幻觉 ref → 应回退到启发式抽取",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 大于 2026年7月", None)],
        ["r1", "r2"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "filter": {"left": {"ref": "rX"}, "op": ">", "right": {"value": "2026-07"}},
                "group_by": [], "window": None,
            },
        },
        "non_empty", ">", "2026-07",
    ),
    (
        "LLM 产出 filter 为空对象 {} → 应启发式抽取",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 大于 2026年7月", None)],
        ["r1", "r2"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "filter": {}, "group_by": [], "window": None,
            },
        },
        "non_empty", ">", "2026-07",
    ),
    (
        "LLM 产出 filter 有合法 ref 但右值为 null → 应启发式填补",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 为已支付", None)],
        ["r1", "r2"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "filter": {"left": {"ref": "r2"}, "op": "=", "right": {"value": None}},
                "group_by": [], "window": None,
            },
        },
        "non_empty", "=", "已支付",
    ),
    (
        "LLM 产出 and 组合条件中某一条右值为 null → 仅填补 null 那条",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" = '已支付' 且 ", None), (None, "r3"), (" 超过 1000", None)],
        ["r1", "r2", "r3"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "filter": {
                    "op": "and",
                    "conditions": [
                        {"left": {"ref": "r2"}, "op": "=", "right": {"value": "已支付"}},
                        {"left": {"ref": "r3"}, "op": "=", "right": {"value": None}},
                    ],
                },
                "group_by": [], "window": None,
            },
        },
        "non_empty", "and", None,
    ),
    (
        "LLM 产出 filter 值不为 null → 不填补,保留 LLM 结果",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"),
         (" 为已支付", None)],
        ["r1", "r2"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "filter": {"left": {"ref": "r2"}, "op": "=", "right": {"value": "已完成"}},
                "group_by": [], "window": None,
            },
        },
        "non_empty", "=", "已完成",
    ),
    (
        "LLM 把 filter 放在 where 字段 → 应统一到 filter",
        [("统计 SUM(", None), (None, "r1"), (") WHERE ", None), (None, "r2"),
         (" = '已支付'", None)],
        ["r1", "r2"],
        {
            "type": "metric", "description": "", "refs": [],
            "body": {
                "operation": "sum", "args": [{"ref": "r1"}],
                "where": {"left": {"ref": "r2"}, "op": "=", "right": {"value": "已支付"}},
                "group_by": [], "window": None,
            },
        },
        "non_empty", "=", "已支付",
    ),
    (
        "形态2:ref 与 op 之间隔小词(的值是)",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" 的值是已支付", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "已支付",
    ),
    (
        "形态3:op+value 在 ref 之前(大于 2026年7月 的 @时间)",
        [("统计 ", None), (None, "r1"), (",大于 2026年7月 的 ", None), (None, "r2"), (" 求和", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", ">", "2026-07",
    ),
    (
        "形态4:ref 后直接跟引号值(无 op,默认 =)",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"), (" 「已支付」", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "已支付",
    ),
    (
        "形态2变体:ref 后冒号分隔(@状态:已支付)",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"), (":已支付", None)],
        ["r1", "r2"],
        simulate_llm_no_filter(["r1"]),
        "non_empty", "=", "已支付",
    ),
]


def extract_filter_info(filter_node):
    """返回 (op, right_value);组合条件返回 ('and', None)。"""
    if filter_node is None:
        return None, None
    if isinstance(filter_node, dict):
        if filter_node.get("op") in ("and", "or"):
            return filter_node.get("op"), None
        if "left" in filter_node and "op" in filter_node:
            right = filter_node.get("right") or {}
            return filter_node.get("op"), right.get("value")
    return None, None


def main() -> int:
    svc = ExpressionFormatterService.__new__(ExpressionFormatterService)
    svc.use_mock = True  # 不实际调 LLM,只测归一化
    failed = 0
    for idx, (desc, text_refs, ref_ids, llm_out, expect, expect_op, expect_val) in enumerate(CASES, 1):
        segments = make_segments(text_refs)
        refs = make_refs(ref_ids)
        result = svc._normalize_llm_output(
            llm_out, segments, refs, "metric", ""
        )
        body = result.get("body") or {}
        filter_node = body.get("filter")
        op, val = extract_filter_info(filter_node)
        if expect == "non_empty":
            ok = filter_node is not None and op == expect_op
            if ok and expect_val is not None:
                ok = val == expect_val
        else:  # no_filter
            ok = filter_node is None
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{status}] case {idx}: {desc}")
        print(f"   filter={filter_node}")
        print(f"   op={op} val={val} (expect op={expect_op} val={expect_val})")
    print()
    print(f"Total: {len(CASES)}, Failed: {failed}")
    return 1 if failed else 0


# ── _mock_format 直测:mock 模式下整个 format 流程的条件识别 ──

MOCK_CASES = [
    # (描述, segments, refs, logic_type, 期望 filter_op, 期望 filter_val)
    (
        "mock:统计 @金额 中 @状态 为已支付",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"), (" 为已支付", None)],
        ["r1", "r2"],
        "metric",
        "=", "已支付",
    ),
    (
        "mock:仅统计 @金额(无条件)",
        [("统计 ", None), (None, "r1"), (" 总和", None)],
        ["r1"],
        "metric",
        None, None,
    ),
    (
        "mock:SUM(@金额) WHERE @状态 = '已支付'",
        [("SUM(", None), (None, "r1"), (") WHERE ", None), (None, "r2"), (" = '已支付'", None)],
        ["r1", "r2"],
        "metric",
        "=", "已支付",
    ),
    (
        "mock:按 @城市 统计 @金额,其中 @状态 = '已支付'",
        [("按 ", None), (None, "r1"), (" 统计 ", None), (None, "r2"), (",其中 ", None), (None, "r3"), (" = '已支付'", None)],
        ["r1", "r2", "r3"],
        "metric",
        "=", "已支付",
    ),
    (
        "mock:@时间 大于 2026年7月 的 @金额 总和",
        [("统计 ", None), (None, "r1"), (" 在 ", None), (None, "r2"), (" 大于 2026年7月 的汇总", None)],
        ["r1", "r2"],
        "metric",
        ">", "2026-07",
    ),
    (
        "mock:@金额 超过 1000",
        [("求 ", None), (None, "r1"), (" 中 ", None), (None, "r2"), (" 超过 1000 元", None)],
        ["r1", "r2"],
        "metric",
        ">", 1000,
    ),
    (
        "mock:@状态 为「已支付」",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"), (" 为「已支付」", None)],
        ["r1", "r2"],
        "metric",
        "=", "已支付",
    ),
    (
        "mock:@金额 不少于 100",
        [("统计 ", None), (None, "r1"), (" 中 ", None), (None, "r2"), (" 不少于 100", None)],
        ["r1", "r2"],
        "metric",
        ">=", 100,
    ),
    (
        "mock:多条件 @状态 = '已支付' 且 @金额 超过 1000",
        [("统计 ", None), (None, "r1"), (",其中 ", None), (None, "r2"),
         (" = '已支付' 且 ", None), (None, "r3"), (" 超过 1000", None)],
        ["r1", "r2", "r3"],
        "metric",
        "and", None,
    ),
    (
        "mock:rule 当 @金额 > 10000 时触发风控",
        [("当 ", None), (None, "r1"), (" > 10000 时触发风控", None)],
        ["r1"],
        "rule",
        ">", 10000,
    ),
    (
        "mock:rule 无条件描述",
        [("检查 ", None), (None, "r1"), (" 是否异常", None)],
        ["r1"],
        "rule",
        "=", None,
    ),
    (
        "mock:tag 若 @年龄 >= 18 标记为成年",
        [("若 ", None), (None, "r1"), (" >= 18 标记为成年", None)],
        ["r1"],
        "tag",
        ">=", 18,
    ),
    (
        "mock:metric 无过滤关键词但 ref 紧接 op+value",
        [("求 ", None), (None, "r1"), (" 的平均值,", None), (None, "r2"), (" 不低于 60", None)],
        ["r1", "r2"],
        "metric",
        ">=", 60,
    ),
    (
        "mock:按 @城市 统计 @金额(group_by 在开头,无 filter)",
        [("按 ", None), (None, "r1"), (" 统计 ", None), (None, "r2")],
        ["r1", "r2"],
        "metric",
        None, None,
    ),
    (
        "mock:统计 @金额 按 @城市(group_by 在末尾,无 filter)",
        [("统计 ", None), (None, "r1"), (" 按 ", None), (None, "r2")],
        ["r1", "r2"],
        "metric",
        None, None,
    ),
    (
        "mock:统计 @金额 按 @城市,其中 @状态 = '已支付'(group_by + filter)",
        [("统计 ", None), (None, "r1"), (" 按 ", None), (None, "r2"), (",其中 ", None), (None, "r3"), (" = '已支付'", None)],
        ["r1", "r2", "r3"],
        "metric",
        "=", "已支付",
    ),
]


def _extract_filter_op_val(filter_node):
    """从 filter/condition 节点提取 op 与 right value。"""
    if filter_node is None:
        return None, None
    if isinstance(filter_node, dict):
        if filter_node.get("op") in ("and", "or"):
            return filter_node.get("op"), None
        if "left" in filter_node and "op" in filter_node:
            right = filter_node.get("right") or {}
            return filter_node.get("op"), right.get("value")
    return None, None


def _extract_first_when_op_val(body: dict):
    """从 tag body 中提取第一个 when 条件的 op 与 right value。"""
    cases = body.get("cases") or []
    for c in cases:
        when = c.get("when")
        if when:
            return _extract_filter_op_val(when)
    return None, None


def test_mock_format() -> int:
    failed = 0
    for idx, (desc, text_refs, ref_ids, logic_type, expect_op, expect_val) in enumerate(MOCK_CASES, 1):
        segments = make_segments(text_refs)
        refs = make_refs(ref_ids)
        result = _mock_format(segments, refs, logic_type, "")
        body = result.get("body") or {}

        if logic_type == "rule":
            cond = body.get("condition")
            op, val = _extract_filter_op_val(cond)
        elif logic_type == "tag":
            op, val = _extract_first_when_op_val(body)
        else:
            filter_node = body.get("filter")
            op, val = _extract_filter_op_val(filter_node)

        ok = op == expect_op
        if ok and expect_val is not None:
            ok = val == expect_val

        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{status}] {desc}")
        if not ok:
            print(f"   body={body}")
            print(f"   op={op} val={val} (expect op={expect_op} val={expect_val})")

    print()
    print(f"Mock format: Total {len(MOCK_CASES)}, Failed {failed}")
    return failed


if __name__ == "__main__":
    err1 = main()
    err2 = test_mock_format()
    raise SystemExit(err1 or err2)
