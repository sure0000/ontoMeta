"""配置驱动的外部工具（P4：免改代码扩展 Data Agent 能力）。

运维注册一个外部 HTTP 工具（名称 + 描述 + JSON-Schema 入参 + 端点 + 可选鉴权头），**启用**
即等于把它交给 Data Agent：启用的工具投影成 OpenAI 函数 schema 注入 agent 工具集（curated——
按域过滤 + 数量封顶，避免全量目录撑爆 prompt 复现 413），模型据描述自主调用，结果经通用 HTTP
executor 取回并封顶字符数。

安全边界：注册走管理鉴权；仅 http(s)；结果封顶；执行器**从不抛异常进 agent 循环**（失败即
返回 error 字典，模型据此换招或如实说明）。凭据（auth_header）写入后不回显。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChatBiExternalTool

logger = logging.getLogger(__name__)

# 工具名：小写字母开头的 snake_case，长度 2-64（与 OpenAI function name 习惯一致）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_ALLOWED_METHODS = {"GET", "POST"}
_EXTERNAL_TOOL_TIMEOUT = 20.0
# 单域可注入的外部工具上限（curated，防 prompt 膨胀）。
MAX_EXTERNAL_TOOLS = 8


class ExternalToolError(ValueError):
    """注册/配置错误（400 类）。"""


def _reserved_names() -> set[str]:
    """原生工具名——外部工具不得同名（延迟导入避免与 chat_bi 循环）。"""
    try:
        from app.services.chat_bi import _TOOL_BY_NAME  # noqa: PLC0415

        return set(_TOOL_BY_NAME.keys())
    except Exception:  # noqa: BLE001
        return set()


def _default_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def register_tool(
    db: Session,
    *,
    name: str,
    description: str,
    url: str,
    parameters: dict[str, Any] | None = None,
    method: str = "POST",
    auth_header: str | None = None,
    domain_id: str | None = None,
    display_name: str | None = None,
    result_max_chars: int = 4000,
) -> ChatBiExternalTool:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ExternalToolError("name 须为小写 snake_case（字母开头，2-64 位）")
    if name in _reserved_names():
        raise ExternalToolError(f"name「{name}」与原生工具冲突，请改名")
    if db.scalar(select(ChatBiExternalTool).where(ChatBiExternalTool.name == name)):
        raise ExternalToolError(f"name「{name}」已存在")
    if not (description or "").strip():
        raise ExternalToolError("description 必填（模型据此判断何时调用）")
    method = (method or "POST").upper()
    if method not in _ALLOWED_METHODS:
        raise ExternalToolError(f"method 仅支持 {'/'.join(sorted(_ALLOWED_METHODS))}")
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ExternalToolError("url 必须是 http(s) 绝对地址")
    if parameters is not None and not isinstance(parameters, dict):
        raise ExternalToolError("parameters 须为 JSON-Schema 对象")
    row = ChatBiExternalTool(
        name=name,
        display_name=(display_name or "").strip() or None,
        description=description.strip(),
        parameters_json=json.dumps(parameters or _default_parameters(), ensure_ascii=False),
        method=method,
        url=url,
        auth_header=(auth_header or "").strip() or None,
        enabled=True,
        domain_id=(domain_id or "").strip() or None,
        result_max_chars=max(200, min(int(result_max_chars or 4000), 20000)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_tools(db: Session, *, domain_id: str | None = None) -> list[ChatBiExternalTool]:
    """列出工具：domain_id 给定则返回该域 + 全局；否则返回全部。"""
    stmt = select(ChatBiExternalTool).order_by(ChatBiExternalTool.created_at.desc())
    rows = list(db.scalars(stmt))
    if domain_id is not None:
        rows = [t for t in rows if t.domain_id is None or t.domain_id == domain_id]
    return rows


def get_tool(db: Session, tool_id: str) -> ChatBiExternalTool | None:
    return db.get(ChatBiExternalTool, tool_id)


def delete_tool(db: Session, tool_id: str) -> bool:
    row = db.get(ChatBiExternalTool, tool_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def set_enabled(db: Session, tool_id: str, enabled: bool) -> ChatBiExternalTool | None:
    row = db.get(ChatBiExternalTool, tool_id)
    if row is None:
        return None
    row.enabled = bool(enabled)
    db.commit()
    db.refresh(row)
    return row


def _enabled_for_domain(db: Session, domain_id: str, *, cap: int = MAX_EXTERNAL_TOOLS) -> list[ChatBiExternalTool]:
    stmt = (
        select(ChatBiExternalTool)
        .where(ChatBiExternalTool.enabled.is_(True))
        .order_by(ChatBiExternalTool.created_at.asc())
    )
    rows = [t for t in db.scalars(stmt) if t.domain_id is None or t.domain_id == domain_id]
    return rows[:cap]


def tool_schemas_for_domain(db: Session, domain_id: str, *, cap: int = MAX_EXTERNAL_TOOLS) -> list[dict[str, Any]]:
    """启用 + 域可见的外部工具 → OpenAI 函数 schema（capped）。"""
    schemas: list[dict[str, Any]] = []
    for t in _enabled_for_domain(db, domain_id, cap=cap):
        try:
            params = json.loads(t.parameters_json) if t.parameters_json else _default_parameters()
        except (TypeError, json.JSONDecodeError):
            params = _default_parameters()
        if not isinstance(params, dict):
            params = _default_parameters()
        schemas.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": params,
            },
        })
    return schemas


def external_tool_names_for_domain(db: Session, domain_id: str, *, cap: int = MAX_EXTERNAL_TOOLS) -> set[str]:
    return {t.name for t in _enabled_for_domain(db, domain_id, cap=cap)}


def call_external_tool(
    db: Session,
    *,
    tool_name: str,
    domain_id: str,
    args: dict[str, Any],
    timeout: float = _EXTERNAL_TOOL_TIMEOUT,
) -> tuple[dict[str, Any], str, bool]:
    """调用一个外部工具。返回 (结果, 摘要, 是否错误)。**从不抛异常**——失败即 error 字典。"""
    row = db.scalar(
        select(ChatBiExternalTool).where(
            ChatBiExternalTool.name == tool_name,
            ChatBiExternalTool.enabled.is_(True),
        )
    )
    if row is None or not (row.domain_id is None or row.domain_id == domain_id):
        return {"error": f"外部工具「{tool_name}」不存在或未对本域启用"}, "外部工具未命中", True

    headers = {"Accept": "application/json"}
    if row.auth_header:
        headers["Authorization"] = row.auth_header
    payload = args if isinstance(args, dict) else {}
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            if row.method == "GET":
                resp = client.get(row.url, params=payload, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                resp = client.post(row.url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 — 网络/超时等一律降级为 error，不炸 agent 循环
        logger.info("external tool %s call failed: %s", tool_name, exc)
        return {"error": f"外部工具调用失败：{type(exc).__name__}"}, "外部工具异常", True

    cap = row.result_max_chars or 4000
    text = resp.text or ""
    try:
        body: Any = resp.json()
    except Exception:  # noqa: BLE001 — 非 JSON 响应按截断文本返回
        body = text[:cap]
    else:
        # JSON 也封顶：序列化后过长则退回截断文本，避免撑爆上下文
        if len(json.dumps(body, ensure_ascii=False)) > cap:
            body = json.dumps(body, ensure_ascii=False)[:cap]

    if resp.status_code >= 400:
        return (
            {"error": f"外部工具返回 {resp.status_code}", "body": body},
            f"外部工具 {tool_name}：HTTP {resp.status_code}",
            True,
        )
    return {"data": body}, f"外部工具 {tool_name} 返回", False
