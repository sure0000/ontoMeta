"""V4 S0 单测：跨轮 compaction（O1）与遥测扩展（O6.2/O6.3）。"""

from __future__ import annotations

from app.services.agent_compaction import compact_conversation, estimate_chars
from app.services import agent_telemetry
from app.services.agent_telemetry import RunTelemetry


def _turns(n: int, size: int = 50) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"第{i}轮" + ("x" * size)})
    return out


def test_short_history_no_summary():
    """预算够时不触发摘要，近轮原样保留（等价旧行为）。"""
    hist = _turns(4, size=10)
    r = compact_conversation(hist, char_budget=100000)
    assert r.summary is None
    assert r.triggered is False
    assert [m["content"] for m in r.recent] == [h["content"] for h in hist]


def test_long_history_triggers_summary():
    """超预算时旧轮被摘要，近轮保留，且被摘掉的轮数统计正确。"""
    hist = _turns(10, size=200)
    r = compact_conversation(hist, char_budget=500)
    assert r.triggered is True
    assert r.summarized_turns > 0
    assert len(r.recent) >= 1
    # 摘要 + 近轮的字符数应显著小于原始
    assert r.chars_after < r.chars_before
    # 近轮里一定包含最新一轮
    assert r.recent[-1]["content"] == hist[-1]["content"]


def test_disabled_falls_back_to_last_6():
    """关闭时退化为 history[-6:]，不摘要。"""
    hist = _turns(10, size=10)
    r = compact_conversation(hist, char_budget=1, enabled=False)
    assert r.summary is None
    assert len(r.recent) == 6
    assert r.recent[0]["content"] == hist[-6]["content"]


def test_carried_names_extracted_for_ledger():
    """摘要里引号内的实体名被抽出，供 FactLedger 入账防误拒答。"""
    hist = [
        {"role": "user", "content": "帮我看「高价值客户」这个标签"},
        {"role": "assistant", "content": "已定位「高价值客户」标签，绑定口径为月消费。"},
        {"role": "user", "content": "换个问题" + "y" * 300},
        {"role": "assistant", "content": "好的" + "z" * 300},
        {"role": "user", "content": "继续" + "w" * 300},
    ]
    r = compact_conversation(hist, char_budget=200)
    assert r.triggered is True
    assert "高价值客户" in r.carried_names


def test_empty_history():
    r = compact_conversation([], char_budget=100)
    assert r.recent == []
    assert r.summary is None
    assert r.triggered is False


def test_t3_key_sql_preserved_in_summary():
    """V5 T3：被摘要旧轮里的 ```sql 围栏块完整保留到摘要末尾（不被首句截断）。"""
    long_sql = (
        "SELECT c.name, SUM(o.amount) AS gmv FROM sales_order o "
        "JOIN customer c ON o.customer_id = c.id "
        "WHERE o.status = 'paid' GROUP BY c.name ORDER BY gmv DESC"
    )
    hist = [
        {"role": "user", "content": "按客户算 GMV"},
        {"role": "assistant", "content": f"已用以下口径：\n```sql\n{long_sql}\n```\n结果如上。"},
        {"role": "user", "content": "再问个无关的" + "y" * 400},
        {"role": "assistant", "content": "好的" + "z" * 400},
        {"role": "user", "content": "现在把刚才的 GMV 口径按月拆开"},
    ]
    r = compact_conversation(hist, char_budget=300)
    assert r.triggered is True
    # 关键 SQL 被抢救出来、完整（含 ORDER BY 尾巴，非首句截断）
    assert len(r.key_sql) == 1
    assert "ORDER BY gmv DESC" in r.key_sql[0]
    assert "已确定的口径 SQL" in r.summary
    assert long_sql in r.summary


def test_t3_only_select_sql_preserved():
    """只抢救只读 SELECT/WITH；非查询围栏不入关键 SQL。"""
    hist = [
        {"role": "assistant", "content": "```sql\nUPDATE t SET x=1\n```"},
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 400},
        {"role": "user", "content": "继续"},
    ]
    r = compact_conversation(hist, char_budget=200)
    assert r.key_sql == []


def test_t3_key_sql_dedup_and_cap():
    """重复 SQL 去重；最多保 3 条（取最后出现的）。"""
    turns = []
    for i in range(5):
        turns.append({"role": "user", "content": f"问题{i}"})
        turns.append({"role": "assistant", "content": f"```sql\nSELECT {i} FROM t{i}\n```"})
    turns.append({"role": "user", "content": "最新一轮" + "z" * 400})
    r = compact_conversation(turns, char_budget=200)
    assert len(r.key_sql) <= 3
    # 保的是最后出现的几条
    assert "SELECT 4 FROM t4" in r.key_sql[-1]


def test_estimate_chars():
    assert estimate_chars("") == 0
    assert estimate_chars("你好abc") == 5


def test_telemetry_context_and_route_snapshot():
    """遥测快照含 context_chars_per_call 与 skill_misroute_rate。"""
    agent_telemetry.reset()
    run = RunTelemetry()
    run.context(1000)
    run.context(2000)
    run.compaction(triggered=True, summarized_turns=3)
    run.route("query")
    run.route_outcome(True)
    agent_telemetry.record(run)

    run2 = RunTelemetry()
    run2.route("create")
    run2.route_outcome(False)  # misroute
    agent_telemetry.record(run2)

    snap = agent_telemetry.snapshot()
    assert snap["context_chars_per_call"] == 1500.0
    assert snap["compaction_runs"] == 1
    assert snap["compaction_summarized_turns"] == 3
    assert snap["skill_routed"] == {"query": 1, "create": 1}
    assert snap["skill_misroute_rate"] == 0.5
    agent_telemetry.reset()


def test_f1_no_entity_excluded_from_misroute_rate():
    """V5 F1：「路对但实体不存在」不计 misroute，且从真 misroute 分母里排掉。"""
    agent_telemetry.reset()
    # 路对（用上工具）
    r1 = RunTelemetry(); r1.route("query"); r1.route_outcome(True)
    # 真路错（选了技能、没用工具、也不是无实体）
    r2 = RunTelemetry(); r2.route("create"); r2.route_outcome(False)
    # 路对但无实体（不该计 misroute）
    r3 = RunTelemetry(); r3.route("lineage"); r3.route_outcome(False, no_entity=True)
    r4 = RunTelemetry(); r4.route("lineage"); r4.route_outcome(False, no_entity=True)
    for r in (r1, r2, r3, r4):
        agent_telemetry.record(r)
    snap = agent_telemetry.snapshot()
    assert snap["skill_no_entity_runs"] == 2
    # 真 misroute 率 = 1 真路错 / (4 总 − 2 无实体) = 0.5
    assert snap["skill_misroute_rate"] == 0.5
    agent_telemetry.reset()


def test_f2_looks_like_refusal():
    """V5 F2：拒答譍气识别——只对真拒答命中，正常答案/幻觉编数不命中。"""
    from app.services.chat_bi import ChatBiService
    assert ChatBiService._looks_like_refusal("很抱歉，未找到名为客户的对象。")
    assert ChatBiService._looks_like_refusal("无法基于已发布本体回答：未检索到匹配对象。")
    # 幻觉编数的“正常答案”不是拒答（由 F4 拦，不该被 F2 逗搜）
    assert not ChatBiService._looks_like_refusal("公司今年利润率约为 18%。")
    assert not ChatBiService._looks_like_refusal("## 市场活动对象\n它用于管理营销活动。")
    assert not ChatBiService._looks_like_refusal("")
