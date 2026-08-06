"""V5 P0 单测：trace 汇总脚本（T1.2）——纯聚合，喂合成轨迹验证指标计算。

不依赖真实 LLM/会话：直接构造若干条 trace 记录（形同 chat_bi.write_trace 落的行），
断言 summarize() 把 V4 六项收益算对。
"""

from __future__ import annotations

from scripts.summarize_agent_traces import summarize


def _rec(**kw) -> dict:
    base = {
        "skill": None, "intent": "analytical", "skill_matched": None,
        "llm_calls": 2, "steps": 1, "tools": {}, "refused": False, "unverified": [],
        "context_chars_per_call": 1000,
        "compaction_triggered": False, "compaction_summarized_turns": 0,
        "offloaded_chars": 0, "offload_count": 0,
        "subagent_runs": 0, "subagent_llm_calls": 0, "subagent_isolated_chars": 0,
    }
    base.update(kw)
    return base


def test_empty_returns_zero_runs():
    assert summarize([]) == {"runs": 0}


def test_basic_aggregates():
    recs = [
        _rec(llm_calls=2, steps=1, context_chars_per_call=1000, refused=False),
        _rec(llm_calls=4, steps=3, context_chars_per_call=2000, refused=True),
    ]
    s = summarize(recs)
    assert s["runs"] == 2
    assert s["refused_runs"] == 1
    assert s["refusal_rate"] == 0.5
    assert s["avg_llm_calls"] == 3.0
    assert s["avg_steps"] == 2.0
    assert s["avg_context_chars_per_call"] == 1500.0


def test_offload_metrics():
    recs = [
        _rec(offloaded_chars=8000, offload_count=1),
        _rec(offloaded_chars=0, offload_count=0),
        _rec(offloaded_chars=4000, offload_count=1),
    ]
    o = summarize(recs)["offload"]
    assert o["runs_with_offload"] == 2
    assert o["total_offloaded_chars"] == 12000
    assert o["avg_offloaded_chars_per_offload_run"] == 6000.0


def test_compaction_metrics():
    recs = [
        _rec(compaction_triggered=True, compaction_summarized_turns=3),
        _rec(compaction_triggered=True, compaction_summarized_turns=5),
        _rec(compaction_triggered=False),
    ]
    c = summarize(recs)["compaction"]
    assert c["triggered_runs"] == 2
    assert c["trigger_rate"] == round(2 / 3, 4)
    assert c["avg_summarized_turns"] == 4.0


def test_subagent_isolation():
    recs = [
        _rec(subagent_runs=1, subagent_llm_calls=2, subagent_isolated_chars=8000),
        _rec(subagent_runs=0),
    ]
    sa = summarize(recs)["subagent"]
    assert sa["runs_using_subagent"] == 1
    assert sa["total_isolated_chars"] == 8000
    assert sa["total_subagent_llm_calls"] == 2
    assert sa["isolation_ratio"] == 4000.0  # 8000 / 2


def test_routing_misroute_rate():
    recs = [
        _rec(skill="query", skill_matched=True),
        _rec(skill="create", skill_matched=False),   # misroute
        _rec(skill=None),                              # 未选技能，不计
    ]
    r = summarize(recs)["routing"]
    assert r["routed_runs"] == 2
    assert r["skill_misroute_rate"] == 0.5
    assert r["skill_distribution"]["query"] == 1
    assert r["skill_distribution"]["(none)"] == 1


def test_grouping_by_skill_and_intent():
    recs = [
        _rec(skill="query", intent="analytical", context_chars_per_call=1000, refused=False),
        _rec(skill="query", intent="analytical", context_chars_per_call=3000, refused=True),
        _rec(skill="overview", intent="structural", context_chars_per_call=500, refused=False),
    ]
    s = summarize(recs)
    assert s["by_skill"]["query"]["runs"] == 2
    assert s["by_skill"]["query"]["avg_context_chars_per_call"] == 2000.0
    assert s["by_intent"]["analytical"]["refusal_rate"] == 0.5
    assert s["by_intent"]["structural"]["refusal_rate"] == 0.0
