"""长会话驱动器（V5 T2）：按固定问句序列跑同一 conversation，供 compaction 调参用。

**为什么需要它**：`agent_history_char_budget` 只有在 history 累到超预算时才起作用，
而单轮 curl 永远触发不了。P0.6 是手工敲 10 轮 curl 采到的第一份长会话数据——不可复现，
换个预算值就得重敲一遍。调参要的是「同一序列、只换预算」的对照，故把驱动固化下来。

用法：
    python scripts/drive_long_session.py --domain-id <id> [--base-url http://localhost:8000]
    python scripts/drive_long_session.py --domain-id <id> --questions my_turns.txt

每轮把上一轮的问答追加进 history 再发下一轮（history 由客户端持有，服务端不回读），
所以 history 是**真累积**的。指标不在这里算：开 `AGENT_TRACE_ENABLED=true` 后由
`scripts/summarize_agent_traces.py` 从轨迹里读，本脚本只负责把会话跑出来并打印每轮的
history 字符数（预算是否触发的直接依据）与是否拒答。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# 采购—供应商—发票这条链路上的结构性下钻。全部落在已发布对象上（不指望 run_sql），
# 于是每轮都能拿到实打实的正文，history 才涨得起来；换域时用 --questions 覆盖。
DEFAULT_QUESTIONS = [
    "这个域里和采购相关的对象有哪些？",
    "purchase_order 这个对象有哪些字段？",
    "purchase_invoice 呢？它和 purchase_order 是什么关系？",
    "supplier 对象有哪些字段？",
    "supplier 和 purchase_order 之间有关系吗？",
    "purchase_invoice_item 有哪些字段？",
    "沿着刚才的采购链路，从供应商到发票明细要经过哪些对象？",
    "supplier_group 是做什么的？和 supplier 什么关系？",
    "purchase_receipt 这个对象有吗？它的字段是什么？",
    "上面提到的这些采购对象里，哪个字段最多？",
    "按刚才的口径，把采购链路上的关键对象再列一遍",
    "采购发票和销售发票 sales_invoice 在建模上有什么差别？",
]


def _post(url: str, body: dict, token: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Admin-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def drive(
    *,
    domain_id: str,
    questions: list[str],
    base_url: str,
    token: str,
    timeout: int,
) -> dict:
    history: list[dict] = []
    conversation_id: str | None = None
    turns: list[dict] = []

    for i, question in enumerate(questions, start=1):
        sent_chars = sum(len(m.get("content") or "") for m in history)
        body = {"domain_id": domain_id, "question": question, "history": history}
        if conversation_id:
            body["conversation_id"] = conversation_id
        started = time.time()
        try:
            data = _post(f"{base_url}/api/chat-bi/ask", body, token, timeout)
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            print(f"[{i:02d}] HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
            break
        conversation_id = data.get("conversation_id") or conversation_id
        answer = data.get("answer") or ""
        refused = bool(data.get("grounding_refused"))
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        turns.append(
            {
                "turn": i,
                "history_chars_sent": sent_chars,
                "answer_chars": len(answer),
                "refused": refused,
                "seconds": round(time.time() - started, 1),
            }
        )
        print(
            f"[{i:02d}] history_in={sent_chars:>6}  answer={len(answer):>5}  "
            f"{'REFUSED' if refused else 'ok':>7}  {turns[-1]['seconds']:>5}s  {question[:28]}"
        )

    return {
        "conversation_id": conversation_id,
        "turns": turns,
        "final_history_chars": sum(len(m.get("content") or "") for m in history),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="驱动一条长会话（V5 T2 compaction 调参用）")
    ap.add_argument("--domain-id", required=True)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--token", default="dev-admin-token-change-me")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--questions", help="每行一个问句的文件；缺省用内置采购链路序列")
    ap.add_argument("--json", action="store_true", help="末尾输出机器可读汇总")
    args = ap.parse_args()

    questions = DEFAULT_QUESTIONS
    if args.questions:
        with open(args.questions, encoding="utf-8") as fh:
            questions = [ln.strip() for ln in fh if ln.strip()]

    result = drive(
        domain_id=args.domain_id,
        questions=questions,
        base_url=args.base_url.rstrip("/"),
        token=args.token,
        timeout=args.timeout,
    )
    print(
        f"\nconversation_id={result['conversation_id']}  "
        f"最终 history={result['final_history_chars']} 字符  "
        f"（用 scripts/summarize_agent_traces.py 读指标）"
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
