"""大结果离场存储（V4 O2）：把 run_sql 的整张结果表**移出模型上下文**，用句柄引用。

**为什么存在**：data-agent 的结果是**大且结构化**的——`run_sql` 默认取到 100 行，
过去整份 `{columns, rows}` 经 `compact_tool_result` 字符截断后塞进 `messages`，被后续每一轮
反复 prefill；而字符截断几乎必然把 JSON 截在半行、丢列，模型看到的是残表。

对齐 pi 的「off-context artifact + 句柄引用」范式（docs 里 read 工具 truncate + 按需再取）：
- **全量行**存进进程内 per-run store（`RunResultStore`，随本次问答生灭，不跨请求）。
- **回给模型**的只是紧凑引用：列名 + 样例 N 行 + 总行数 + `result_handle`。
- 模型要更多行时调 `read_result(handle, offset, limit)` **分页取**——只在需要时把那几行调进上下文。
- 前端渲染 / `analyze_result` / `render_chart` 仍从 run-local 全量副本取，保真度不变。

**不变式**：store 是**运行内**的（per ask），绝不做跨请求缓存——否则又变成一个什么都往里塞、
还得考虑失效与串号的全局态。它只为「让整张表不进上下文」这一件事存在。
"""

from __future__ import annotations

from typing import Any


class RunResultStore:
    """单次问答内的结果暂存。handle → {columns, rows}。随 ask 生灭。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def put(self, columns: list, rows: list) -> str:
        self._seq += 1
        handle = f"rs_{self._seq}"
        self._data[handle] = {"columns": list(columns or []), "rows": list(rows or [])}
        return handle

    def get(self, handle: str) -> dict[str, Any] | None:
        return self._data.get(handle)

    def page(self, handle: str, *, offset: int, limit: int) -> dict[str, Any]:
        """按 handle 分页取行。越界/未知句柄返回带 error 的合法 dict，不抛异常。"""
        entry = self._data.get(handle)
        if entry is None:
            return {"error": f"未知结果句柄「{handle}」，可用句柄见最近一次 run_sql 返回的 result_handle。"}
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        rows = entry["rows"]
        window = rows[offset : offset + limit]
        return {
            "handle": handle,
            "columns": entry["columns"],
            "rows": window,
            "offset": offset,
            "returned": len(window),
            "total": len(rows),
            "has_more": offset + len(window) < len(rows),
        }


def project_run_sql_for_model(
    result: dict[str, Any], store: "RunResultStore", *, sample_rows: int
) -> dict[str, Any]:
    """把 run_sql 的完整结果投影成**回给模型的紧凑引用**，并把全量行寄存进 store。

    仅当结果确有执行且带 rows 时离场；否则原样返回（建议 SQL / 报错等本就不大）。
    返回的 dict 用样例行替换全量行，附 `result_handle` 与省略提示。
    """
    if not isinstance(result, dict) or not result.get("executed"):
        return result
    rows = result.get("rows")
    if not isinstance(rows, list):
        return result

    handle = store.put(result.get("columns") or [], rows)
    n = max(0, int(sample_rows))
    sample = rows[:n]
    omitted = len(rows) - len(sample)

    ref = {k: v for k, v in result.items() if k != "rows"}
    ref["result_handle"] = handle
    ref["sample_rows"] = sample
    ref["row_count"] = result.get("row_count", len(rows))
    if omitted > 0:
        ref["rows_omitted"] = omitted
        ref["result_note"] = (
            f"结果共 {len(rows)} 行，上方 sample_rows 仅前 {len(sample)} 行示例；"
            f"其余 {omitted} 行未进上下文。需要更多行请调 "
            f"read_result(handle=\"{handle}\", offset=…, limit=…)，勿臆造未见行。"
        )
    else:
        ref["result_note"] = f"结果共 {len(rows)} 行，已在 sample_rows 中全部给出。"
    return ref


__all__ = ["RunResultStore", "project_run_sql_for_model"]
