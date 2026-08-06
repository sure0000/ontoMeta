"""ChatBI 渲染块投影单测（V3 S0）。

盯住 answer_to_blocks 的两条不变式：
1. **顺序与出场**对齐改造前 ChatBubble 的 && 阶梯，空字段自动略过；
2. 口径卡**自适应形态**——平凡单步映射走 inline，多步/含业务逻辑走 caliber。

这是「块投影不改变既有展示语义」的回归面（V3 §6 不变式 4）：契约变更要有测试钉住。
"""

from __future__ import annotations

from app.services.chat_bi_blocks import answer_to_blocks


def _types(payload: dict) -> list[str]:
    return [b["type"] for b in answer_to_blocks(payload)]


def test_trivial_query_projects_answer_only():
    """最朴素的问答：只有正文 → 只有一个 markdown 块。"""
    blocks = answer_to_blocks({"answer": "客户表当前有 1200 行。"})
    assert [b["type"] for b in blocks] == ["markdown"]
    assert blocks[0]["content"] == "客户表当前有 1200 行。"
    assert blocks[0]["id"] == "b0"


def test_full_query_order_matches_legacy_ladder():
    """取数全要素：块顺序 = steps → 正文 → 口径 → SQL → 表。命中本体并入口径卡，不再另发 refs 块。"""
    payload = {
        "answer": "GMV 为 380 万。",
        "steps": [{"index": 0, "tool": "run_sql", "arguments": {"sql": "SELECT 1"}}],
        "caliber_decomposition": [
            {"label": "汇总 GMV", "references": [{"kind": "business_logic", "name": "GMV"}]},
        ],
        "suggested_sql": "SELECT SUM(amount) FROM orders",
        "data_result": {"columns": [{"key": "gmv"}], "rows": [{"gmv": 3800000}]},
        "referenced_objects": [{"name": "订单"}],
        "referenced_logics": [{"name": "GMV"}],
    }
    assert _types(payload) == ["steps", "markdown", "mapping", "sql", "table"]
    # 命中本体并入口径卡，不再另发底部 refs 块
    mapping = next(b for b in answer_to_blocks(payload) if b["type"] == "mapping")
    assert [r.get("name") or r.get("display_name") for r in mapping["references"]] == ["GMV", "订单"]


def test_ids_are_stable_and_sequential():
    payload = {"answer": "x", "suggested_sql": "SELECT 1"}
    blocks = answer_to_blocks(payload)
    assert [b["id"] for b in blocks] == ["b0", "b1"]


def test_mapping_inline_when_only_hits_no_trace():
    """纯检索（无口径展开）但命中对象 → inline 形态，只显一行命中本体。"""
    payload = {"answer": "订单包含金额、状态等字段。", "referenced_objects": [{"id": "O1", "name": "订单"}]}
    mapping = next(b for b in answer_to_blocks(payload) if b["type"] == "mapping")
    assert mapping["variant"] == "inline"
    assert mapping["items"] == []
    assert [(r["kind"], r.get("id")) for r in mapping["references"]] == [("object_type", "O1")]


def test_mapping_hits_deduped_across_caliber_and_refs():
    """命中本体去重：口径展开引用的口径与 referenced_logics 同一 id → 只出现一次，顺序=口径→对象。"""
    payload = {
        "answer": "见下。",
        "caliber_decomposition": [
            {"label": "口径展开 · GMV",
             "references": [{"kind": "business_logic", "id": "L1", "display_name": "GMV"}]},
        ],
        "referenced_objects": [{"id": "O1", "name": "订单"}],
        "referenced_logics": [{"id": "L1", "name": "GMV"}],
    }
    blocks = answer_to_blocks(payload)
    assert "refs" not in [b["type"] for b in blocks]
    mapping = next(b for b in blocks if b["type"] == "mapping")
    assert [(r["kind"], r.get("id")) for r in mapping["references"]] == [
        ("business_logic", "L1"), ("object_type", "O1")
    ]


def test_no_mapping_block_without_caliber_or_hits():
    """既无口径展开也无命中本体 → 不发 mapping 块。"""
    assert "mapping" not in _types({"answer": "客户表当前有 1200 行。"})


def test_mapping_variant_caliber_when_has_trace_items():
    """有口径展开轨迹 → caliber 形态。"""
    payload = {
        "answer": "见下。",
        "caliber_decomposition": [
            {"label": "口径展开 · 复购率", "references": [{"kind": "business_logic", "name": "复购率"}]},
        ],
    }
    mapping = next(b for b in answer_to_blocks(payload) if b["type"] == "mapping")
    assert mapping["variant"] == "caliber"


def test_refusal_projects_notice_then_answer_keeping_steps():
    """拒答：保留工具轨迹 + 拒答提示 + 拒答正文，与 ask/ask_stream 的处置一致。"""
    payload = {
        "answer": "为避免不准确信息，已谨慎拒答。",
        "grounding_refused": True,
        "steps": [{"index": 0, "tool": "search_objects", "arguments": {"keyword": "x"}}],
    }
    assert _types(payload) == ["steps", "notice", "markdown"]
    notice = next(b for b in answer_to_blocks(payload) if b["type"] == "notice")
    assert notice["variant"] == "refused" and notice["level"] == "warning"


def test_clarification_replaces_markdown():
    """澄清反问：出 clarify 块，不再出 markdown 正文块。"""
    payload = {
        "answer": "irrelevant",
        "clarification": {"question": "指哪个口径？", "options": ["含税", "不含税"]},
    }
    types = _types(payload)
    assert "clarify" in types
    assert "markdown" not in types


def test_empty_answer_emits_no_markdown_block():
    """正文为空（如纯拒答无文案）→ 不发空 markdown 块。"""
    assert _types({"answer": "", "grounding_refused": True}) == ["notice"]


def test_table_skipped_when_no_rows():
    payload = {"answer": "无数据。", "data_result": {"columns": [{"key": "c"}], "rows": []}}
    assert _types(payload) == ["markdown"]


def test_chart_block_follows_table_and_carries_data():
    """V3 S1：render_chart 产出的图表规格投影成 chart 块，紧随结果表并自带数据行。"""
    payload = {
        "answer": "见下。",
        "data_result": {
            "columns": [{"key": "月份"}, {"key": "gmv"}],
            "rows": [{"月份": "1月", "gmv": 100}, {"月份": "2月", "gmv": 200}],
        },
        "charts": [{"kind": "line", "x": "月份", "y": "gmv"}],
    }
    types = _types(payload)
    assert types == ["markdown", "table", "chart"]
    chart = next(b for b in answer_to_blocks(payload) if b["type"] == "chart")
    assert chart["spec"] == {"kind": "line", "x": "月份", "y": "gmv"}
    # 自带列与行，前端无需回看兄弟表格即可渲染
    assert len(chart["rows"]) == 2
    assert chart["columns"] == [{"key": "月份"}, {"key": "gmv"}]


def test_no_chart_block_without_charts():
    payload = {"answer": "x", "data_result": {"columns": [{"key": "c"}], "rows": [{"c": 1}]}}
    assert "chart" not in _types(payload)


def test_lineage_block_from_payload():
    """V3 S2：get_lineage 产出投影成 lineage 块，携带 center + nodes + edges。"""
    payload = {
        "answer": "订单的上游是客户。",
        "lineage": {
            "center_id": "o1",
            "nodes": [
                {"id": "o1", "display_name": "订单"},
                {"id": "c1", "display_name": "客户"},
            ],
            "edges": [
                {"id": "e1", "source": "o1", "target": "c1", "label": "归属", "structure_type": "foreign_key"},
            ],
            "truncated": False,
        },
    }
    blocks = answer_to_blocks(payload)
    assert [b["type"] for b in blocks] == ["markdown", "lineage"]
    lin = next(b for b in blocks if b["type"] == "lineage")
    assert lin["center_id"] == "o1"
    assert len(lin["nodes"]) == 2 and len(lin["edges"]) == 1


def test_no_lineage_block_without_nodes():
    assert "lineage" not in _types({"answer": "x", "lineage": {"center_id": "o1", "nodes": []}})


def test_draft_proposal_block_from_payload():
    """V3 S3：propose_draft 产出投影成 draft_proposal 块，携带 create_payload。"""
    payload = {
        "answer": "已拟好复购率指标提案。",
        "draft_proposals": [
            {
                "kind": "business_logic",
                "logic_type": "metric",
                "display_name": "复购率",
                "name": "repurchase_rate",
                "description": "90天内再次购买占比",
                "create_payload": {
                    "domain_id": "d1",
                    "name": "repurchase_rate",
                    "display_name": "复购率",
                    "logic_type": "metric",
                },
            }
        ],
    }
    blocks = answer_to_blocks(payload)
    assert [b["type"] for b in blocks] == ["markdown", "draft_proposal"]
    dp = next(b for b in blocks if b["type"] == "draft_proposal")
    assert dp["proposal"]["display_name"] == "复购率"
    assert dp["proposal"]["create_payload"]["domain_id"] == "d1"


def test_no_draft_proposal_block_without_proposals():
    assert "draft_proposal" not in _types({"answer": "x", "draft_proposals": []})


def test_form_request_replaces_markdown():
    """P6：交互表单出口——出 form 块，不再出 markdown 正文块（与澄清同构）。"""
    payload = {
        "answer": "确认取数需求",
        "form_request": {
            "title": "确认取数需求",
            "fields": [
                {"name": "metric", "label": "指标", "type": "select", "options": ["GMV", "客单价"]},
                {"name": "range", "label": "时间范围", "type": "text"},
            ],
        },
    }
    types = _types(payload)
    assert "form" in types
    assert "markdown" not in types
    form = next(b for b in answer_to_blocks(payload) if b["type"] == "form")
    assert form["form"]["title"] == "确认取数需求"
    assert len(form["form"]["fields"]) == 2


def test_clarification_takes_precedence_over_form():
    """澄清与表单同时在场时，澄清优先（都在场是异常，取其一即可，不出重复正文）。"""
    payload = {
        "answer": "x",
        "clarification": {"question": "指哪个口径？", "options": ["含税"]},
        "form_request": {"title": "t", "fields": [{"name": "a", "label": "A", "type": "text"}]},
    }
    types = _types(payload)
    assert "clarify" in types
    assert "form" not in types
    assert "markdown" not in types


def test_no_form_block_without_form_request():
    assert "form" not in _types({"answer": "客户表当前有 1200 行。"})

