"""MCP 工具共用的会话与结果工具。

会话：工具在请求线程里自己开/关 Session（MCP 没有 FastAPI 的依赖注入），
统一走这里的 ``session()``，而不是各文件 ``next(get_db())`` 再手写 finally。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal


def host_confirmation_gate(db, auth: Any, supplied: Any, digest: str, *, action: str) -> Any | None:
    """Require an explicit local-host user confirmation for non-task writes.

    MCP proposals are safe to preview from any client.  Writes that have no
    GovernanceArtifact to carry the per-item approval (lineage and logic
    publication) use the same local stdio host assertion as task execution.
    Remote clients must use the Web confirmation surface.
    """
    from app.services.settings_service import SettingsService

    runtime = SettingsService().get_mcp_runtime(db)
    if not runtime.mcp_allow_stdio_interactive_approval:
        from . import ToolResult

        return ToolResult(
            success=False,
            error=f"本部署未启用本机 MCP 宿主交互确认；请在 Web 中由人确认后执行{action}",
            metadata={"gate": "host_interactive_approval_disabled"},
        )
    if not auth.is_local_mcp or not auth.principal_id:
        from . import ToolResult

        return ToolResult(
            success=False,
            error=f"{action}只接受本机 stdio 的真实 Principal 宿主确认，远程 HTTP 不能自带批准",
            metadata={"gate": "host_interactive_approval_not_allowed"},
        )
    if (
        not isinstance(supplied, dict)
        or supplied.get("approved") is not True
        or supplied.get("channel") != "ask_user_question"
    ):
        from . import ToolResult

        return ToolResult(
            success=False,
            error="必须先由宿主 ask_user_question 得到 approved=true，再传回确认摘要",
            metadata={"gate": "host_interactive_approval_invalid"},
        )
    if str(supplied.get("digest") or "").strip() != digest:
        from . import ToolResult

        return ToolResult(
            success=False,
            error="预览内容已经变化，旧确认摘要失效；请重新展示并确认",
            data={"current_digest": digest},
            metadata={"gate": "host_interactive_approval_stale"},
        )
    return None


@contextmanager
def session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def dump(model: Any) -> Any:
    """Pydantic 读模型 → 可 JSON 序列化的普通结构。

    工具复用 ``OntologyQueryService`` 等既有服务层的返回值，那些是 Pydantic 模型；
    ``mode="json"`` 让日期/枚举在这里就落成字符串，而不是留给 ``ToolResult`` 的
    ``default=str`` 兜底（兜底会把 ``None`` 之外的一切都变成字符串，类型信息丢失）。
    """
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if isinstance(model, list):
        return [dump(item) for item in model]
    return model


def as_int(value: Any, default: int, *, low: int, high: int) -> int:
    """把入参里的数字读成范围内的整数。

    MCP 客户端理应按 input_schema 校验类型，但工具不能把「理应」当前提——一个
    ``limit: "全部"`` 不该炸成一条没有指向性的 500 式错误。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def loads(raw: str | None, fallback: Any = None) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def artifact_approval_digest(artifact: Any) -> str:
    """Bind an interactive approval to the exact reviewed task payload."""
    payload = {
        "id": artifact.id,
        "kind": artifact.kind,
        "ontology_id": artifact.ontology_id,
        "spec": loads(artifact.spec_json, {}),
        "validation": loads(artifact.validation_report_json, {}),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
