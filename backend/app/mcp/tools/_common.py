"""MCP 工具共用的会话与结果工具。

会话：工具在请求线程里自己开/关 Session（MCP 没有 FastAPI 的依赖注入），
统一走这里的 ``session()``，而不是各文件 ``next(get_db())`` 再手写 finally。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy.orm import Session

from app.database import SessionLocal


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
