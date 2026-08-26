"""Data Agent 人工决策留痕的读写服务。

**独立模块而不并进 chat_bi.py**：本模块要被 ``api/agents.py``（制品确认/执行）调用，
塞进对话服务会让写侧治理流水线反向依赖 Chat BI——方向错了。这里只依赖 models，谁都能调。

三条硬约束（改动前先读）：

1. **只记录，不授权**。执行门槛的唯一权威是 ``GovernanceArtifact.status``。
   账本里有 execute 记录也不能让一条未确认的制品被执行。
2. **写失败绝不影响主链路**。``record_decision`` 整体吞异常并 rollback，返回 None。
   照 ``chat_bi.record_domain_memory`` 的先例——留痕是增强，不是主流程。
3. **凭据绝不入库**。``OnboardProposalBlock`` 对用户白纸黑字承诺过"连接信息由你自己填、
   助手不经手也不留存"，账本不得成为凭据泄漏的新出口。见 ``_redact``。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent import GovernanceArtifact
from app.models.chat_bi import ChatBiConversation, ChatBiConversationTask
from app.models.chat_bi_ledger import (
    NODE_SEQUENCE,
    ChatBiDecisionRecord,
    DecisionNode,
    DecisionOutcome,
)

logger = logging.getLogger("ontometa.chat_bi_ledger")

# 键名命中即脱敏。宁可多脱一个也不能漏一个——账本是长期留存的。
_SECRET_HINTS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "credential", "dsn",
    "access_key", "secret_key", "private_key", "api_key", "auth",
)

# 单份 JSON 的上限。防止有人把 data_result.rows 整个塞进 chosen 撑爆库。
_JSON_CAP = 16_384

_VALID_NODES = {n.value for n in DecisionNode}
_VALID_OUTCOMES = {o.value for o in DecisionOutcome}


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return any(hint in low for hint in _SECRET_HINTS)


def _redact(value: Any, _depth: int = 0) -> Any:
    """递归剔除疑似凭据的键。深度上限防环。"""
    if _depth > 6:
        return "***"
    if isinstance(value, dict):
        return {
            k: ("***" if _is_secret_key(k) else _redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, _depth + 1) for v in value[:200]]
    return value


def _dumps_capped(value: Any | None) -> str | None:
    """脱敏后序列化；超限则截断为一条可读的占位，不抛。"""
    if value is None:
        return None
    try:
        text = json.dumps(_redact(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return json.dumps({"_unserializable": str(exc)}, ensure_ascii=False)
    if len(text) > _JSON_CAP:
        return json.dumps(
            {"_truncated": True, "_bytes": len(text), "_head": text[:2000]},
            ensure_ascii=False,
        )
    return text


def _loads(text: str | None) -> Any | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _diff_keys(proposed: Any, chosen: Any) -> list[str]:
    """人相对机器基线改动了哪些**顶层**键。

    只做顶层：``context`` 本身是扁平的，而深度 diff 的展示成本远大于价值。
    两边不都是 dict 时无法做键级对比——返回空列表，由 outcome 另行判定。
    """
    if not isinstance(proposed, dict) or not isinstance(chosen, dict):
        return []
    changed = {k for k in set(proposed) | set(chosen) if proposed.get(k) != chosen.get(k)}
    return sorted(changed)


def record_decision(
    db: Session,
    *,
    conversation_id: str,
    node: str,
    outcome: str | None = None,
    stage: str | None = None,
    trigger: str | None = None,
    message_id: str | None = None,
    block_id: str | None = None,
    summary: str | None = None,
    proposed: Any | None = None,
    chosen: Any | None = None,
    ref_kind: str | None = None,
    ref_id: str | None = None,
    subject_id: str | None = None,
    subject_role: str | None = None,
    dedup_key: str | None = None,
) -> str | None:
    """记一笔人工决策。返回记录 id；去重命中返回既有 id；**任何失败返回 None 且不抛**。

    ``outcome`` 留空时按 proposed/chosen 的差异自动判定（改过=modified，否则=accepted）——
    避免每个调用点各写一份判定逻辑，也避免前端算错。

    ``node``/``outcome`` 取值非法时**归一而不拒绝**：上游是 UI 事件，宁可记糊一条
    也不要丢一条。
    """
    try:
        # 会话必须存在。**不能靠 FK 兜底**：SQLite 默认不启用外键约束，
        # 靠 FK 的话一条指向不存在会话的记录会被静默写进去，变成任何闭环视图都
        # 看不到、却会污染跨会话追踪页的孤儿行。显式查一次，便宜且跨库行为一致。
        if not db.get(ChatBiConversation, conversation_id):
            logger.info("record_decision skipped: conversation %s not found", conversation_id)
            return None

        node_value = node if node in _VALID_NODES else DecisionNode.OTHER.value
        if node_value != node:
            logger.info("decision node normalized: %s -> other", node)

        overridden = _diff_keys(proposed, chosen)
        if outcome is None:
            # 只有两边都给了才谈得上"改没改"；否则默认视为接受。
            outcome_value = (
                DecisionOutcome.MODIFIED.value
                if overridden
                else DecisionOutcome.ACCEPTED.value
            )
        else:
            outcome_value = (
                outcome if outcome in _VALID_OUTCOMES else DecisionOutcome.ACCEPTED.value
            )

        if dedup_key:
            existing = (
                db.query(ChatBiDecisionRecord)
                .filter(ChatBiDecisionRecord.dedup_key == dedup_key)
                .first()
            )
            if existing:
                return existing.id

        next_seq = (
            db.query(func.coalesce(func.max(ChatBiDecisionRecord.seq), 0))
            .filter(ChatBiDecisionRecord.conversation_id == conversation_id)
            .scalar()
            or 0
        ) + 1

        row = ChatBiDecisionRecord(
            conversation_id=conversation_id,
            message_id=message_id,
            block_id=block_id,
            seq=next_seq,
            node=node_value,
            stage=stage,
            trigger=trigger,
            outcome=outcome_value,
            subject_id=subject_id,
            subject_role=subject_role,
            summary=(summary or None),
            proposed_json=_dumps_capped(proposed),
            chosen_json=_dumps_capped(chosen),
            overridden_fields=(
                json.dumps(overridden, ensure_ascii=False) if overridden else None
            ),
            ref_kind=ref_kind,
            ref_id=ref_id,
            dedup_key=dedup_key,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except IntegrityError:
        # dedup_key 竞态：另一个请求抢先插了同一条。回滚后重查取既有 id。
        db.rollback()
        try:
            if dedup_key:
                existing = (
                    db.query(ChatBiDecisionRecord)
                    .filter(ChatBiDecisionRecord.dedup_key == dedup_key)
                    .first()
                )
                if existing:
                    return existing.id
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.info("record_decision dedup re-read failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — 留痕是增强，绝不能拖垮确认动作本身
        db.rollback()
        logger.info("record_decision failed: %s", exc)
        return None


def safe_record(db: Session, **kwargs: Any) -> str | None:
    """``record_decision`` 的调用点护栏。

    ``record_decision`` 自身已吞掉内部异常，但**调用点仍需这层**：若该函数本身出了
    意料之外的问题（被 monkeypatch、导入期损坏、参数不匹配、库连接在此刻挂掉），
    异常照样会顺着调用栈冲进用户正在做的确认动作里，把一次成功的确认变成 500。

    留痕是观察层，绝不能有任何一条路径让它拖垮主链路——故在此再兜一次底。
    """
    try:
        return record_decision(db, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 兜底层，任何异常都不得外泄
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.info("safe_record swallowed: %s", exc)
        return None


def _to_dict(row: ChatBiDecisionRecord) -> dict:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "message_id": row.message_id,
        "block_id": row.block_id,
        "seq": row.seq,
        "node": row.node,
        "stage": row.stage,
        "trigger": row.trigger,
        "outcome": row.outcome,
        "subject_id": row.subject_id,
        "subject_role": row.subject_role,
        "summary": row.summary,
        "proposed": _loads(row.proposed_json),
        "chosen": _loads(row.chosen_json),
        "overridden_fields": _loads(row.overridden_fields) or [],
        "ref_kind": row.ref_kind,
        "ref_id": row.ref_id,
        "created_at": row.created_at,
    }


def list_decisions(db: Session, conversation_id: str) -> list[dict]:
    """本会话的决策时间线（最早在前）。"""
    rows = (
        db.query(ChatBiDecisionRecord)
        .filter(ChatBiDecisionRecord.conversation_id == conversation_id)
        .order_by(ChatBiDecisionRecord.created_at.asc(), ChatBiDecisionRecord.seq.asc())
        .all()
    )
    return [_to_dict(r) for r in rows]


def build_closure(
    db: Session, conversation_id: str, *, include_records: bool = True
) -> dict:
    """闭环总结：恒六环 + 悬挂项告警 + 完整时间线。

    **未到达的环标灰而不隐藏**——"哪一环没走"正是管理要看的东西。
    闭环状态由记录聚合推导、不独立维护（同 PipelineStatus 的取舍：两处状态迟早分叉）。

    ``include_records=False`` 给出不含时间线的轻量摘要，供只要六环聚合的调用方。
    """
    records = list_decisions(db, conversation_id)
    by_node: dict[str, list[dict]] = {}
    for rec in records:
        by_node.setdefault(rec["node"], []).append(rec)

    nodes: list[dict] = []
    for node_value, label in NODE_SEQUENCE:
        items = by_node.get(node_value, [])
        latest = items[-1] if items else None
        nodes.append(
            {
                "node": node_value,
                "label": label,
                "reached": bool(items),
                "latest_outcome": latest["outcome"] if latest else None,
                "latest_at": latest["created_at"] if latest else None,
                "summary": latest["summary"] if latest else None,
                "count": len(items),
            }
        )

    return {
        "conversation_id": conversation_id,
        "nodes": nodes,
        "reached_count": sum(1 for n in nodes if n["reached"]),
        "total_count": len(NODE_SEQUENCE),
        "dangling": _detect_dangling(by_node),
        "tasks": _conversation_tasks(db, conversation_id),
        "records": records if include_records else [],
    }


def _conversation_tasks(db: Session, conversation_id: str) -> list[dict]:
    """本会话催生的任务（治理制品），最近的在前。

    闭环卡靠这份清单给出**重新进入某一环的入口**。此前后三环只能从"刚提交完那一下"
    弹出的抽屉里确认，制品 id 只活在组件的 useState 里——人不小心关掉窗口（或刷新页面），
    这条任务就在对话里彻底失联，方案/执行/结果三环再也走不到。而 (会话, 制品) 的关联
    本来就落了库（``draft-confirmed`` 建完草稿即 ``link_conversation_task``），缺的只是
    读回来的通道。

    照本模块的既有姿态：读失败给空列表，绝不连累闭环本身。
    """
    try:
        rows = (
            db.query(ChatBiConversationTask, GovernanceArtifact)
            .outerjoin(
                GovernanceArtifact,
                GovernanceArtifact.id == ChatBiConversationTask.artifact_id,
            )
            .filter(ChatBiConversationTask.conversation_id == conversation_id)
            .order_by(ChatBiConversationTask.created_at.desc())
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("conversation tasks lookup failed: %s", exc)
        return []
    tasks: list[dict] = []
    seen: set[str] = set()
    for link, artifact in rows:
        # 同一条制品可能被关联多次（重复提交/任务链推进），只留最近的一条。
        if link.artifact_id in seen:
            continue
        seen.add(link.artifact_id)
        tasks.append(
            {
                "artifact_id": link.artifact_id,
                "kind": (artifact.kind if artifact else None) or link.kind,
                # 制品已被删除时退回意图文本：给个能认出来的说法，好过一串 uuid。
                "name": (artifact.name if artifact else None) or link.intent or link.artifact_id,
                "status": artifact.status if artifact else None,
            }
        )
    return tasks


def _detect_dangling(by_node: dict[str, list[dict]]) -> list[str]:
    """悬挂项：走了一半没走完的环。这是"可管理"最直接的兑现。"""
    issues: list[str] = []
    plans = by_node.get(DecisionNode.PLAN.value, [])
    execs = by_node.get(DecisionNode.EXECUTE.value, [])
    results = by_node.get(DecisionNode.RESULT.value, [])

    planned_refs = {r["ref_id"] for r in plans if r["ref_id"]}
    executed_refs = {r["ref_id"] for r in execs if r["ref_id"]}

    for ref in sorted(planned_refs - executed_refs):
        issues.append(f"已确认执行方案但尚未执行（制品 {ref}）")
    # "未确认不得执行"是治理硬不变量。账本里出现无对应 plan 的 execute，
    # 要么是绕过了对话入口（工单直接起草，正常），要么是真有问题——一律呈现给人判断。
    for ref in sorted(executed_refs - planned_refs):
        issues.append(f"存在未经本会话方案确认的执行记录（制品 {ref}）")
    if execs and not results:
        issues.append("任务已执行但结果尚未确认")
    return issues


def search_decisions(
    db: Session,
    *,
    node: str | None = None,
    outcome: str | None = None,
    ref_kind: str | None = None,
    subject_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """跨会话查询，供决策追踪页。

    结果附带 ``conversation_title``：追踪页列的是跨会话的记录，只给一串 uuid
    等于逼人逐条点进去才知道在看什么。一次 outer join 换掉 N 次往返。
    """
    q = db.query(ChatBiDecisionRecord, ChatBiConversation.title).outerjoin(
        ChatBiConversation,
        ChatBiConversation.id == ChatBiDecisionRecord.conversation_id,
    )
    if node:
        q = q.filter(ChatBiDecisionRecord.node == node)
    if outcome:
        q = q.filter(ChatBiDecisionRecord.outcome == outcome)
    if ref_kind:
        q = q.filter(ChatBiDecisionRecord.ref_kind == ref_kind)
    if subject_id:
        q = q.filter(ChatBiDecisionRecord.subject_id == subject_id)
    if since:
        q = q.filter(ChatBiDecisionRecord.created_at >= since)
    if until:
        q = q.filter(ChatBiDecisionRecord.created_at <= until)
    rows = (
        q.order_by(ChatBiDecisionRecord.created_at.desc())
        .limit(max(1, min(limit, 1000)))
        .all()
    )
    return [{**_to_dict(row), "conversation_title": title} for row, title in rows]


# ---------------- 任务确认之旅（六环）----------------
#
# 「一个数据任务要人分别确认哪几件事」是**一份**定义，不能各处各写一份：表单向导画前三
# 环、制品抽屉画后三环、服务端按前三环放行——三处一旦分叉，用户会看到「向导说 3/6、
# 抽屉说 2/3、后端说还差一环」这种没人解释得清的状态。故在此定义一份，前端经 API 拿到
# 同一份，后端门禁也用同一份。

# 前三环在对话内的表单向导里逐环确认；后三环在制品抽屉里逐环确认（dry-run 出来才谈得上
# 确认执行方案）。phase 就是「这一环在哪儿确认」，前端据此决定哪几步是当前可填的。
FORM_CONFIRMATION_NODES: tuple[str, ...] = (
    DecisionNode.REQUIREMENT.value,
    DecisionNode.ONTOLOGY.value,
    DecisionNode.DATA.value,
)
ARTIFACT_CONFIRMATION_NODES: tuple[str, ...] = (
    DecisionNode.PLAN.value,
    DecisionNode.EXECUTE.value,
    DecisionNode.RESULT.value,
)

# 人真的表过态才算这一环走到了。rejected/skipped 不算——那是「没确认」的两种说法。
CONFIRMED_OUTCOMES: frozenset[str] = frozenset(
    {DecisionOutcome.ACCEPTED.value, DecisionOutcome.MODIFIED.value}
)

_NODE_LABEL: dict[str, str] = dict(NODE_SEQUENCE)


def task_journey_steps(
    *, ontology_label: str = "本体/口径", data_label: str = "数据落点"
) -> list[dict[str, str]]:
    """一个数据任务的六环确认之旅（给前端画同一条进度条）。

    前三环的标题按任务类型定制（同步确认的是"同步本体"、物化确认的是"物化范围"），
    后三环对四类任务是同一件事，故文案固定。
    """
    return [
        {"node": DecisionNode.REQUIREMENT.value, "phase": "form",
         "title": "确认任务需求", "description": "确认任务目标；可修改系统理解的需求"},
        {"node": DecisionNode.ONTOLOGY.value, "phase": "form",
         "title": f"确认{ontology_label}", "description": "只从当前本体和形式化口径的真实候选中选择"},
        {"node": DecisionNode.DATA.value, "phase": "form",
         "title": "确认数据与参数", "description": f"确认{data_label}"},
        {"node": DecisionNode.PLAN.value, "phase": "artifact",
         "title": "确认执行方案", "description": "校验与 dry-run 后，在任务详情里核对执行方案再确认"},
        {"node": DecisionNode.EXECUTE.value, "phase": "artifact",
         "title": "执行任务", "description": "确认过的方案才可执行；执行由你在任务详情里发起"},
        {"node": DecisionNode.RESULT.value, "phase": "artifact",
         "title": "确认执行结果", "description": "跑完后核对回执与实际落库结果，给出成功/失败反馈"},
    ]


def task_confirmations(
    db: Session, conversation_id: str, confirmation_id: str
) -> dict[str, dict]:
    """取本次任务（按 ``confirmation_id`` 隔离）各环的**最新**确认记录。

    同一会话可能连续建多条任务，故必须按表单发的 ``confirmation_id`` 隔离，
    不能拿上一条任务的确认给这一条放行。
    """
    if not conversation_id or not confirmation_id:
        return {}
    latest: dict[str, dict] = {}
    for record in list_decisions(db, conversation_id):
        chosen = record.get("chosen") or {}
        if (
            isinstance(chosen, dict)
            and chosen.get("task_confirmation_id") == confirmation_id
        ):
            latest[record["node"]] = record
    return latest


def missing_task_confirmations(
    db: Session,
    conversation_id: str,
    confirmation_id: str,
    *,
    nodes: tuple[str, ...] = FORM_CONFIRMATION_NODES,
) -> list[str]:
    """还差哪几环没确认（按给定环序返回，便于原样展示给用户）。"""
    latest = task_confirmations(db, conversation_id, confirmation_id)
    return [
        node
        for node in nodes
        if (latest.get(node) or {}).get("outcome") not in CONFIRMED_OUTCOMES
    ]


def node_label(node: str) -> str:
    return _NODE_LABEL.get(node, node)


def resolve_conversation_for_artifact(db: Session, artifact_id: str) -> str | None:
    """制品 → 催生它的会话。

    反查 ``chat_bi_conversation_tasks``——那是既有的唯一 (会话, 制品) 接缝。
    制品也可能从工单表单直接起草（无会话），此时返回 None，调用方跳过留痕即可。
    """
    try:
        row = (
            db.query(ChatBiConversationTask)
            .filter(ChatBiConversationTask.artifact_id == artifact_id)
            .order_by(ChatBiConversationTask.created_at.desc())
            .first()
        )
        return row.conversation_id if row else None
    except Exception as exc:  # noqa: BLE001
        logger.info("resolve_conversation_for_artifact failed: %s", exc)
        return None


__all__ = [
    "record_decision",
    "safe_record",
    "list_decisions",
    "build_closure",
    "search_decisions",
    "resolve_conversation_for_artifact",
    "FORM_CONFIRMATION_NODES",
    "ARTIFACT_CONFIRMATION_NODES",
    "CONFIRMED_OUTCOMES",
    "task_journey_steps",
    "task_confirmations",
    "missing_task_confirmations",
    "node_label",
]
