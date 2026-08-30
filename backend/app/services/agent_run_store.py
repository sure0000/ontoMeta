"""Persisted Data Agent run envelopes backed by ``ChatBiMessage`` payloads.

The assistant message is the durable unit of one Agent run. Its message id is
also the run id, while the existing payload remains the artifact body. This
keeps conversation deletion and retention semantics in one place and avoids a
second table that could drift from messages or ``GovernanceArtifact``.

Only compact, user-visible projections enter the artifact manifest and future
prompt context. Full result-store rows remain run-local by design.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


_MAX_HISTORY_ARTIFACT_CHARS = 4_000
_MAX_ERROR_CHARS = 500
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|dsn|connection[_-]?json)"
)


def _iso(value: datetime | str | None) -> str:
    if isinstance(value, str):
        return value
    return (value or datetime.now(timezone.utc)).isoformat()


def sanitize_run_error(error: Exception | str) -> str:
    """Keep failure diagnostics useful without persisting common credentials."""
    text = str(error).strip() or type(error).__name__
    text = _URL_CREDENTIAL.sub(r"\1***:***@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=***", text)
    return text[:_MAX_ERROR_CHARS]


def _redact(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        semantic_key = str(value.get("key") or value.get("label") or "")
        return {
            str(k): (
                "***"
                if str(k) == "value" and _SENSITIVE_KEY.search(semantic_key)
                else _redact(v, key=str(k))
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _artifact(
    run_id: str,
    kind: str,
    index: int,
    *,
    label: str,
    payload_path: str,
    snapshot: dict[str, Any] | None = None,
    source: str | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"{run_id}:{kind}:{index}",
        "kind": kind,
        "label": label,
        "payload_path": payload_path,
    }
    if snapshot:
        item["snapshot"] = snapshot
    if source:
        item["source"] = source
    if as_of is not None:
        item["as_of"] = as_of
    return item


def build_artifact_manifest(payload: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    """Index the durable, user-visible outputs of a completed Agent turn."""
    artifacts: list[dict[str, Any]] = []

    sql = str(payload.get("suggested_sql") or "").strip()
    if sql:
        artifacts.append(_artifact(
            run_id,
            "sql",
            0,
            label="已验证查询 SQL",
            payload_path="suggested_sql",
            snapshot={"sql": sql},
        ))

    data_result = payload.get("data_result")
    if isinstance(data_result, dict):
        columns = [
            str(column.get("name") or column.get("key") or "")
            if isinstance(column, dict) else str(column)
            for column in (data_result.get("columns") or [])
        ]
        rows = data_result.get("rows") or []
        artifacts.append(_artifact(
            run_id,
            "data_result",
            0,
            label=f"查询结果（{len(rows)} 行）",
            payload_path="data_result",
            snapshot={
                "columns": [name for name in columns if name],
                "visible_row_count": len(rows),
                "truncated": bool(data_result.get("truncated")),
            },
        ))

    for index, record in enumerate(payload.get("ops_records") or []):
        if not isinstance(record, dict):
            continue
        family = str(record.get("family") or "record")
        snapshot = {
            key: record.get(key)
            for key in (
                "family", "subject", "facts", "note", "candidates", "total",
                "observed_at", "as_of",
            )
            if key in record
        }
        artifacts.append(_artifact(
            run_id,
            "ops_record",
            index,
            label=f"运行记录：{record.get('subject') or family}",
            payload_path=f"ops_records[{index}]",
            snapshot=_redact(snapshot),
            source=str(record.get("source") or "") or None,
            as_of=record.get("as_of"),
        ))

    for kind, key in (
        ("object_ref", "referenced_objects"),
        ("logic_ref", "referenced_logics"),
    ):
        for index, ref in enumerate(payload.get(key) or []):
            if not isinstance(ref, dict):
                continue
            artifacts.append(_artifact(
                run_id,
                kind,
                index,
                label=str(ref.get("display_name") or ref.get("name") or ref.get("id") or kind),
                payload_path=f"{key}[{index}]",
                snapshot={
                    field: ref.get(field)
                    for field in ("id", "name", "display_name")
                    if ref.get(field) is not None
                },
            ))

    collection_kinds = (
        ("task_status", "task_statuses"),
        ("draft_proposal", "draft_proposals"),
        ("action_proposal", "action_proposals"),
        ("pipeline_proposal", "pipeline_proposals"),
        ("app_proposal", "app_proposals"),
        ("onboard_proposal", "onboard_proposals"),
    )
    for kind, key in collection_kinds:
        for index, value in enumerate(payload.get(key) or []):
            if not isinstance(value, dict):
                continue
            label = str(
                value.get("name")
                or value.get("title")
                or value.get("subject")
                or value.get("kind")
                or kind
            )
            snapshot = {
                field: value.get(field)
                for field in ("id", "artifact_id", "name", "title", "kind", "status")
                if value.get(field) is not None
            }
            artifacts.append(_artifact(
                run_id,
                kind,
                index,
                label=label,
                payload_path=f"{key}[{index}]",
                snapshot=snapshot,
            ))

    lineage = payload.get("lineage")
    if isinstance(lineage, dict):
        artifacts.append(_artifact(
            run_id,
            "lineage",
            0,
            label="血缘子图",
            payload_path="lineage",
            snapshot={
                "node_count": len(lineage.get("nodes") or []),
                "edge_count": len(lineage.get("edges") or []),
            },
        ))

    return artifacts


def run_status(payload: dict[str, Any]) -> str:
    if payload.get("grounding_refused"):
        return "refused"
    if payload.get("clarification") or payload.get("form_request"):
        return "waiting_input"
    return "succeeded"


def attach_agent_run(
    payload: dict[str, Any],
    *,
    run_id: str,
    question: str,
    started_at: datetime | str,
    finished_at: datetime | str | None = None,
    intent: str | None = None,
    status: str | None = None,
    error: Exception | str | None = None,
) -> dict[str, Any]:
    """Return a payload carrying a durable run envelope and artifact index."""
    persisted = dict(payload)
    final_status = status or run_status(persisted)
    grounded = bool(
        persisted.get("_grounded")
        or persisted.get("referenced_objects")
        or persisted.get("referenced_logics")
        or persisted.get("data_result")
        or persisted.get("ops_records")
        or any(
            isinstance(step, dict) and step.get("status") == "succeeded"
            for step in (persisted.get("steps") or [])
        )
    )
    run = {
        "id": run_id,
        "status": final_status,
        "question": question,
        "intent": intent,
        "skill": persisted.get("skill"),
        "grounded": grounded,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
    }
    if error is not None:
        run["error"] = sanitize_run_error(error)
        run["grounded"] = False
    persisted["agent_run"] = run
    persisted["agent_artifacts"] = build_artifact_manifest(persisted, run_id)
    return persisted


def failed_run_payload(
    *,
    run_id: str,
    question: str,
    started_at: datetime | str,
    error: Exception | str,
    intent: str | None = None,
    status: str = "failed",
) -> dict[str, Any]:
    message = sanitize_run_error(error)
    return attach_agent_run(
        {
            "answer": f"回答失败：{message}",
            "suggested_sql": None,
            "referenced_objects": [],
            "referenced_logics": [],
            "steps": [],
            "data_result": None,
            "grounding_refused": False,
            "used_mock": False,
        },
        run_id=run_id,
        question=question,
        started_at=started_at,
        intent=intent,
        status=status,
        error=message,
    )


def _artifact_history_note(payload: dict[str, Any]) -> str:
    artifacts = payload.get("agent_artifacts") or []
    if not artifacts:
        return ""
    safe = [
        {
            key: item.get(key)
            for key in ("id", "kind", "label", "source", "as_of", "snapshot")
            if item.get(key) is not None
        }
        for item in artifacts
        if isinstance(item, dict)
    ]
    if not safe:
        return ""
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > _MAX_HISTORY_ARTIFACT_CHARS:
        encoded = encoded[:_MAX_HISTORY_ARTIFACT_CHARS] + "…"
    return (
        "\n\n【已持久化运行制品】以下是该轮已经展示给用户的结构化输出索引。"
        "SQL 与引用实体可用于延续本轮；运行状态/落点等动态事实必须重新调用权威 reader 核实，"
        "不得仅凭历史快照声称仍是当前状态：\n"
        + encoded
    )


def build_persisted_history(messages: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Project stored messages into model history with compact artifact context."""
    history: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if role == "assistant" and isinstance(message.get("payload"), dict):
            content += _artifact_history_note(message["payload"])
        history.append({"role": role, "content": content})
    return history


__all__ = [
    "attach_agent_run",
    "build_artifact_manifest",
    "build_persisted_history",
    "failed_run_payload",
    "run_status",
    "sanitize_run_error",
]
