#!/usr/bin/env python3
"""V5 T1.2：把 agent_trace 的 JSONL 轨迹汇总成「V4 收益对照表」。

读 `settings.agent_trace_dir`（默认 `.logs/agent_traces/`）下的 `agent-trace-*.jsonl`，
每行是一次问答的运行轨迹（见 chat_bi.py 的 write_trace）。本脚本做**只读**聚合，
不改任何轨迹、不落库——纯观测。

用法（在 backend/ 目录）：
  source .venv/bin/activate
  python scripts/summarize_agent_traces.py                # 汇总默认目录全部
  python scripts/summarize_agent_traces.py --dir /path    # 指定目录
  python scripts/summarize_agent_traces.py --json         # 输出机读 JSON
  python scripts/summarize_agent_traces.py --day 2026-08-07  # 只看某天

输出：总体均值 + 按 skill/intent 分组 + V4 六项收益的实测对照。
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 保证可从 scripts/ 直接运行时找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_records(trace_dir: Path, day: str | None) -> list[dict]:
    pattern = f"agent-trace-{day}.jsonl" if day else "agent-trace-*.jsonl"
    records: list[dict] = []
    for fp in sorted(trace_dir.glob(pattern)):
        with fp.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 坏行跳过，不因一行脏数据毁掉整份汇总
    return records


def _avg(nums: list[float]) -> float:
    return round(statistics.mean(nums), 1) if nums else 0.0


def summarize(records: list[dict]) -> dict:
    """把一堆运行轨迹压成一份指标汇总。"""
    n = len(records)
    if n == 0:
        return {"runs": 0}

    def col(key: str) -> list[float]:
        return [float(r.get(key) or 0) for r in records]

    def flag(key: str) -> int:
        return sum(1 for r in records if r.get(key))

    # 按 skill / intent 分组的 misroute 与上下文
    by_skill: dict[str, list[dict]] = defaultdict(list)
    by_intent: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_skill[r.get("skill") or "(none)"].append(r)
        by_intent[r.get("intent") or "(none)"].append(r)

    routed = [r for r in records if r.get("skill")]
    # V5 F1：「路对但无实体」不算 misroute；真路错率从分母里排掉 no_entity。
    no_entity = [r for r in routed if r.get("skill_no_entity")]
    misrouted = sum(
        1 for r in routed
        if r.get("skill_matched") is False and not r.get("skill_no_entity")
    )
    routed_effective = len(routed) - len(no_entity)

    offload_runs = [r for r in records if (r.get("offload_count") or 0) > 0]
    subagent_runs = [r for r in records if (r.get("subagent_runs") or 0) > 0]
    compaction_runs = [r for r in records if r.get("compaction_triggered")]

    return {
        "runs": n,
        "refused_runs": flag("refused"),
        "refusal_rate": round(flag("refused") / n, 4),
        "avg_llm_calls": _avg(col("llm_calls")),
        "avg_steps": _avg(col("steps")),
        "avg_context_chars_per_call": _avg(col("context_chars_per_call")),
        # V4 O2 大结果离场
        "offload": {
            "runs_with_offload": len(offload_runs),
            "total_offloaded_chars": int(sum(col("offloaded_chars"))),
            "avg_offloaded_chars_per_offload_run": _avg(
                [float(r.get("offloaded_chars") or 0) for r in offload_runs]
            ),
        },
        # V4 O1 compaction
        "compaction": {
            "triggered_runs": len(compaction_runs),
            "trigger_rate": round(len(compaction_runs) / n, 4),
            "avg_summarized_turns": _avg(
                [float(r.get("compaction_summarized_turns") or 0) for r in compaction_runs]
            ),
        },
        # V4 O4 子 agent 隔离
        "subagent": {
            "runs_using_subagent": len(subagent_runs),
            "total_isolated_chars": int(sum(col("subagent_isolated_chars"))),
            "total_subagent_llm_calls": int(sum(col("subagent_llm_calls"))),
            "isolation_ratio": (
                round(
                    sum(col("subagent_isolated_chars"))
                    / max(1, sum(col("subagent_llm_calls"))),
                    1,
                )
                if subagent_runs else 0.0
            ),
        },
        # V4 O6 skill 路由
        "routing": {
            "routed_runs": len(routed),
            "routed_no_entity": len(no_entity),
            "skill_misroute_rate": round(misrouted / routed_effective, 4) if routed_effective > 0 else 0.0,
            "skill_distribution": dict(Counter(r.get("skill") or "(none)" for r in records)),
        },
        "by_skill": {
            k: {"runs": len(v), "avg_context_chars_per_call": _avg(
                [float(x.get("context_chars_per_call") or 0) for x in v]
            )}
            for k, v in sorted(by_skill.items())
        },
        "by_intent": {
            k: {"runs": len(v), "refusal_rate": round(
                sum(1 for x in v if x.get("refused")) / len(v), 4
            )}
            for k, v in sorted(by_intent.items())
        },
    }


def _print_table(s: dict) -> None:
    if s.get("runs", 0) == 0:
        print("没有轨迹记录。请先设 agent_trace_enabled=True 采一段真实会话。")
        return
    print(f"\n=== Agent Trace 汇总（{s['runs']} 次问答）===")
    print(f"拒答率            : {s['refusal_rate']:.1%}  ({s['refused_runs']}/{s['runs']})")
    print(f"平均 LLM 调用     : {s['avg_llm_calls']}   （基线 2.6，不应回涨）")
    print(f"平均步数          : {s['avg_steps']}")
    print(f"平均上下文字符/调用: {s['avg_context_chars_per_call']}   （O1/O2/O3 降它）")

    o = s["offload"]
    print(f"\n[O2 大结果离场] 触发 {o['runs_with_offload']} 次，"
          f"累计移出上下文 {o['total_offloaded_chars']} 字符，"
          f"均 {o['avg_offloaded_chars_per_offload_run']} 字符/次")

    c = s["compaction"]
    print(f"[O1 compaction] 触发 {c['triggered_runs']} 次（{c['trigger_rate']:.1%}），"
          f"均摘 {c['avg_summarized_turns']} 轮")

    sa = s["subagent"]
    print(f"[O4 子 agent] 使用 {sa['runs_using_subagent']} 次，"
          f"隔离 {sa['total_isolated_chars']} 字符未进主上下文，"
          f"代价 {sa['total_subagent_llm_calls']} 次子 LLM 调用，"
          f"隔离比 {sa['isolation_ratio']}×")

    r = s["routing"]
    print(f"[O6 路由] 选技能 {r['routed_runs']} 次（其中路对但无实体 {r['routed_no_entity']} 次），"
          f"真 misroute 率 {r['skill_misroute_rate']:.1%}")
    print(f"          分布 {r['skill_distribution']}")

    print("\n按 skill 的上下文字符/调用：")
    for k, v in s["by_skill"].items():
        print(f"  {k:12s} runs={v['runs']:3d}  ctx/call={v['avg_context_chars_per_call']}")
    print("按 intent 的拒答率：")
    for k, v in s["by_intent"].items():
        print(f"  {k:12s} runs={v['runs']:3d}  refusal={v['refusal_rate']:.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="汇总 agent_trace JSONL 轨迹")
    ap.add_argument("--dir", default=None, help="轨迹目录（默认取 settings.agent_trace_dir）")
    ap.add_argument("--day", default=None, help="只看某天，如 2026-08-07")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    args = ap.parse_args()

    if args.dir:
        trace_dir = Path(args.dir)
    else:
        from app.config import settings
        trace_dir = Path(settings.agent_trace_dir)
        if not trace_dir.is_absolute():
            trace_dir = Path.cwd() / trace_dir

    if not trace_dir.exists():
        print(f"轨迹目录不存在：{trace_dir}")
        print("提示：设 agent_trace_enabled=True 后跑一段真实会话再来汇总。")
        return

    records = _load_records(trace_dir, args.day)
    summary = summarize(records)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_table(summary)


if __name__ == "__main__":
    main()
