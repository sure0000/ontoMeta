"""ChatBI 渲染块投影（V3 S0）。

把已填充的终态扁平 payload（answer / caliber_decomposition / suggested_sql /
data_result / steps / referenced_* / clarification / …）按当前展示顺序投影成一串
有类型的渲染块，供前端块注册表渲染，替代改造前写死的 JSX 阶梯。

纯函数、无副作用：在 API 层对每个终态 payload 双写 ``blocks``；前端有一份等价的
``answerToBlocks`` 兜底没有 blocks 的旧消息。**两处规则必须一致**——改这里记得同步
``frontend/src/pages/chat-bi/utils.ts``。

顺序对齐改造前 ``ChatBubble`` 的 && 阶梯：
steps → 拒答提示 → 澄清/正文 → 口径映射 → SQL → 结果表 → 引用 → mock 提示。
生成数据应用的动作条与错误态是交互/瞬态，不入块，仍由前端页面态承载。
"""

from __future__ import annotations

from typing import Any


def _mapping_variant(items: list[dict[str, Any]]) -> str:
    """口径卡形态：有口径展开轨迹 → 完整卡 ``caliber``；只有「命中本体」→ 内联 chip ``inline``。"""
    return "caliber" if items else "inline"


def _caliber_hits(
    caliber: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    logics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """「命中本体」——本次回答落到的口径/对象，汇成一行去重的可跳转引用。

    取代此前散落三处的重复：口径卡里的「对象口径」噪声项、每项内联 chip、以及底部独立的
    refs 块。顺序=口径展开引用的口径 → 命中对象 → 命中逻辑；按 (kind, id|name) 去重。
    """
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, ref: dict[str, Any]) -> None:
        key = (kind, str(ref.get("id") or ref.get("name") or ""))
        if key == (kind, "") or key in seen:
            return
        seen.add(key)
        hits.append({**ref, "kind": kind})

    for item in caliber:
        for ref in item.get("references") or []:
            _add((ref or {}).get("kind") or "business_logic", ref)
    for obj in objects:
        _add("object_type", obj)
    for logic in logics:
        _add("business_logic", logic)
    return hits


def answer_to_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把终态 payload 投影成渲染块序列。空块（对应字段未填充）自动略过。"""
    blocks: list[dict[str, Any]] = []
    counter = 0

    def _add(block: dict[str, Any]) -> None:
        nonlocal counter
        block["id"] = f"b{counter}"
        counter += 1
        blocks.append(block)

    if payload.get("steps"):
        _add({"type": "steps", "steps": payload["steps"]})

    if payload.get("grounding_refused"):
        _add({"type": "notice", "level": "warning", "variant": "refused"})

    clarification = payload.get("clarification")
    if clarification:
        _add({"type": "clarify", "clarification": clarification})
    else:
        # 拒答/概览等正文可能为空——answer 为空则不发 markdown 块。
        answer = payload.get("answer")
        if answer:
            _add({"type": "markdown", "content": answer})

    caliber = payload.get("caliber_decomposition") or []
    objects = payload.get("referenced_objects") or []
    logics = payload.get("referenced_logics") or []
    # 口径映射块：口径展开轨迹（items）+ 一行去重的「命中本体」（references）。
    # 「命中本体」已并入本块，故不再另发底部 refs 块——消除对象列举的重复。
    hits = _caliber_hits(caliber, objects, logics)
    if caliber or hits:
        _add({"type": "mapping", "variant": _mapping_variant(caliber),
              "items": caliber, "references": hits})

    if payload.get("suggested_sql"):
        _add({"type": "sql", "sql": payload["suggested_sql"]})

    data_result = payload.get("data_result") or {}
    if data_result.get("rows"):
        _add(
            {
                "type": "table",
                "columns": data_result.get("columns") or [],
                "rows": data_result.get("rows") or [],
                "truncated": bool(data_result.get("truncated")),
            }
        )

    # V3 S1：图表块（render_chart 产出）。自带数据行以便前端自足渲染，紧随结果表。
    for spec in payload.get("charts") or []:
        _add(
            {
                "type": "chart",
                "spec": spec,
                "columns": data_result.get("columns") or [],
                "rows": data_result.get("rows") or [],
            }
        )

    # V3 S2：血缘块（get_lineage 产出）。中心 + depth 跳邻域子图，前端画上下游图。
    lineage = payload.get("lineage")
    if lineage and (lineage.get("nodes")):
        _add(
            {
                "type": "lineage",
                "center_id": lineage.get("center_id"),
                "nodes": lineage.get("nodes") or [],
                "edges": lineage.get("edges") or [],
                "truncated": bool(lineage.get("truncated")),
            }
        )

    # V3 S3：建数提案块（propose_draft 产出）。纯提案 + 「去确认」创建载荷；不写库。
    for proposal in payload.get("draft_proposals") or []:
        _add({"type": "draft_proposal", "proposal": proposal})

    if payload.get("used_mock"):
        _add({"type": "notice", "level": "info", "variant": "mock"})

    return blocks
