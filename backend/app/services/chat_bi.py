"""智能问数（ChatBI）服务：基于已发布本体知识 + LLM 回答业务问题。

流程：
1. 根据数据域解析出已发布本体（无则返回引导性提示）。
2. 组装「本体知识包」：对象、字段、关系、业务逻辑。
3. 调用 LLM 生成结构化回答（JSON：answer / suggested_sql / referenced_*）。
4. Mock 模式下基于关键词匹配规则生成示例性回答，保证链路可体验。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, joinedload

import sqlparse

from app.config import settings as env_settings

from app.models import (
    BusinessLogic,
    ChatBiConversation,
    ChatBiMessage,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.common import log_change, make_async_http_client
from app.services.query import OntologyQueryService
from app.services.settings_service import SettingsService
from app.services.ontology_projection import build_projection
from app.services.sql_soundness import SqlRejection, prove_sql_sound
from app.services.agent_grounding import FactLedger
from app.services.answer_verifier import verify_answer

logger = logging.getLogger("ontometa.chat_bi")

_AGG_KEYWORDS = ("多少", "总数", "总量", "合计", "汇总", "统计", "count", "sum", "avg", "平均")
_TIME_KEYWORDS = ("最近", "近", "今日", "昨天", "本月", "上月", "近 7 天", "近7天", "近 30 天", "近30天")
_FILTER_KEYWORDS = ("按", "where", "筛选", "条件", "等于", "大于", "小于")


# ---------------------------------------------------------------------------
# Data Agent（工具编排）运行常量 —— 均衡档（见 DATA_AGENT_REDESIGN.md §9.1）
# ---------------------------------------------------------------------------
_AGENT_MAX_STEPS = 6            # Agent 循环步数上限，超出强制收尾作答
_RUN_SQL_LIMIT = 100           # run_sql 默认返回行上限
_TOOL_RESULT_MAX_CHARS = 8000  # 单个工具结果回灌前的截断阈值
_SQL_TIMEOUT_SECONDS = 15      # run_sql 语句超时（execute_sql 既有能力）
_SEARCH_LIMIT = 8              # 检索类工具默认返回条数

_AGENT_SYSTEM_PROMPT = (
    "你是企业数据问答助手（Data Agent），基于**已发布本体**回答业务问题。\n"
    "你可以多步调用工具来检索本体（业务对象/字段/关系/业务逻辑）并执行只读 SQL，"
    "像分析师一样先查清口径再作答。\n\n"
    "工作纪律：\n"
    "0. 【铁律·数据来源】只能基于工具**实际返回**的数据作答：对象/字段/关系/口径/数值/统计"
    "一律**只能来自工具结果**，工具没返回的内容绝不可编造或凭常识补充；"
    "所有内容**仅限已发布(published)本体**，不得提及或杜撰未发布的草稿。\n"
    "1. 先用 search_objects / search_logics / search_relations 找到相关实体，"
    "再用 get_object / get_logic 拿字段与口径细节；不要臆造对象名或字段名。"
    "检索关键词优先用**中文**（本体以中文命名，英文词多半命不中）。\n"
    "1b. 概览/列举类问题（如“有哪些对象/本体”）**先调用 get_domain_overview**，"
    "严格基于它返回的已发布对象总数与清单作答，不要逐个猜关键词穷举、更不要编造数量或对象名。\n"
    "2. 若问题需要具体数值/明细，用 get_object 拿到真实字段后写 SQL；"
    "**只要你写了查询 SQL，就必须通过 run_sql 提交**（哪怕只为取数/校验），"
    "不要仅在正文里贴 SQL 而不调用 run_sql。表名/字段名必须来自本体。\n"
    "3. run_sql 只读：只能 SELECT。若返回“无可执行数据源”，就把该查询作为建议 SQL 给出，"
    "并说明未实际执行。\n"
    "4. 用中文、Markdown 作答：先给口径解读，再给结论/建议；有数据时用表格或要点呈现。\n"
    "5. 若本体中确实找不到相关对象/逻辑，如实说明无法基于当前本体回答，不要编造。\n"
    "6. 不要在正文里堆砌调试信息；工具调用过程会单独展示给用户。"
)

# OpenAI 原生 function-calling 工具（自建 GLM 实测支持）。
# 检索类直呼 OntologyQueryService（带 q 关键词 + limit），避免 MCP 目录“仅按域全返”导致的巨结果。
_AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_objects",
            "description": "按关键词检索当前数据域的已发布业务对象，返回候选列表（含 id/名称/字段数）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（对象名/含义）"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_object",
            "description": "获取单个业务对象的完整定义：字段（name/显示名/类型/语义）、进出关系、绑定的业务逻辑。",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string", "description": "业务对象 id（来自 search_objects）"},
                },
                "required": ["object_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_relations",
            "description": "按关键词检索业务关系（两个对象之间的关联），返回关系列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logics",
            "description": "按关键词检索业务逻辑/指标口径（如 GMV、活跃客户），返回口径列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logic",
            "description": "获取单个业务逻辑/指标的完整口径：表达式、绑定的对象与字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "logic_id": {"type": "string", "description": "业务逻辑 id（来自 search_logics）"},
                },
                "required": ["logic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_domain_overview",
            "description": (
                "获取当前数据域【已发布本体】的概览：已发布业务对象与关系的总数，并列举已发布对象名。"
                "回答“有哪些对象/本体”这类概览问题时首选此工具。**仅含已发布内容，不含未发布草稿**。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "在当前数据域的物理数据源上执行**只读 SELECT**，返回列与真实数据行。"
                "表名/字段名必须使用本体标识符。若无可执行数据源会返回提示，此时改为给出建议 SQL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句"},
                    "limit": {"type": "integer", "description": f"返回行上限，默认 {_RUN_SQL_LIMIT}"},
                },
                "required": ["sql"],
            },
        },
    },
]


def _format_sql(sql: str | None) -> str | None:
    """使用 sqlparse 美化 SQL；失败时原样返回。"""
    if not sql or not sql.strip():
        return sql
    try:
        formatted = sqlparse.format(
            sql,
            reindent=True,
            keyword_case="upper",
            strip_comments=False,
            use_space_around_operators=True,
        )
        return formatted.rstrip()
    except Exception:
        return sql


@dataclass
class _ObjectSnapshot:
    id: str
    name: str
    display_name: str
    description: str | None
    properties: list[Property]


class ChatBiService:
    """以本体知识为上下文，调用 LLM 回答业务提问。"""

    def __init__(self) -> None:
        self.query_service = OntologyQueryService()
        self.settings_service = SettingsService()

    # ------------------------------------------------------------------ public

    async def ask(
        self,
        db: Session,
        *,
        domain_id: str,
        question: str,
        history: list[dict] | None = None,
    ) -> dict:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")

        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            return {
                "domain_id": domain_id,
                "domain_name": domain.name,
                "ontology_id": None,
                "answer": (
                    f"「{domain.name}」当前还没有已发布的本体。"
                    "请先在「本体建模」中完成草稿编辑并发布，"
                    "Data Agent 会基于已发布本体的对象、字段、关系与业务逻辑进行解读。"
                ),
                "suggested_sql": None,
                "referenced_objects": [],
                "referenced_logics": [],
                "used_mock": True,
            }

        snapshots = self._load_ontology_snapshot(db, ontology.id)
        relations = self._load_relations(db, ontology.id)
        logics = self._load_logics(db, ontology.id)

        # 关键词命中：用于 Mock 兜底的接地判定，也作为 Agent 的检索种子提示
        grounded_objects = self._match_objects(question, snapshots)
        grounded_logics = self._match_logics(question, logics)

        runtime = self.settings_service.get_llm_runtime(db)
        use_mock = not runtime.api_key

        # 名称 -> 实体 索引用全量本体：把 LLM/工具输出的名称/伪 id 归一到真实 id 供前端跳转
        resolver = _ReferenceResolver(
            objects=snapshots, relations=relations, logics=logics
        )

        if use_mock:
            # Mock 路径：保持原有关键词接地 —— 无命中直接拒绝，避免编造
            if not grounded_objects and not grounded_logics:
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )
            payload = self._mock_answer(
                question=question,
                snapshots=snapshots,
                relations=relations,
                logics=logics,
                matched_objects=grounded_objects,
                matched_logics=grounded_logics,
            )
            payload = resolver.resolve_payload(payload)
            payload = self._enforce_grounded_refs(
                payload,
                grounded_objects=grounded_objects,
                grounded_logics=grounded_logics,
                resolver=resolver,
            )
            if not payload.get("referenced_objects") and not payload.get("referenced_logics"):
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )
        else:
            # Agent 路径：LLM 自主多步调用工具检索/跑数；refs/sql/data_result 从工具轨迹收割。
            # 不再一次性灌全量本体 —— 上下文由分页工具按需拉取，结构上根治 413。
            try:
                payload = await self._run_agent_loop(
                    db,
                    runtime=runtime,
                    domain=domain,
                    ontology=ontology,
                    question=question,
                    history=history or [],
                    resolver=resolver,
                    seed_objects=grounded_objects,
                    seed_logics=grounded_logics,
                )
            except Exception as exc:
                logger.exception("ChatBI agent loop failed: %s", exc)
                payload = self._mock_answer(
                    question=question,
                    snapshots=snapshots,
                    relations=relations,
                    logics=logics,
                    matched_objects=grounded_objects,
                    matched_logics=grounded_logics,
                )
                payload.setdefault("steps", [])
                payload["answer"] = (
                    f"> {self._friendly_llm_error(exc)}\n\n{payload['answer']}"
                )
            payload = resolver.resolve_payload(payload)
            # Agent 接地判定：只要 Agent 真正命中过本体数据（检索有结果 / 读到对象逻辑 /
            # 概览 / 跑出数据）就视为接地，即便未产出具体引用（如“有哪些对象”这类概览问题）。
            # F4：校验失败（_unverified 非空）优先拒答，不被 referenced_* 兜底覆盖。
            unverified = payload.get("_unverified") or []
            if unverified:
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                    reasons=unverified,
                )
            grounded = bool(
                payload.pop("_grounded", False)
                or payload.get("referenced_objects")
                or payload.get("referenced_logics")
                or payload.get("data_result")
            )
            if not grounded:
                return self._ungrounded_refusal(
                    domain_id=domain_id,
                    domain_name=domain.name,
                    ontology_id=ontology.id,
                    question=question,
                )

        if payload.get("suggested_sql"):
            payload["suggested_sql"] = _format_sql(payload["suggested_sql"])

        payload.update(
            {
                "domain_id": domain_id,
                "domain_name": domain.name,
                "ontology_id": ontology.id,
                "used_mock": use_mock,
            }
        )
        return payload

    async def ask_stream(
        self,
        db: Session,
        *,
        domain_id: str,
        question: str,
        history: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """ask() 的流式版：yield step_start / step_done / token / done。

        done.payload 与 ask() 返回结构一致；透传工具步骤与逐字答案，供 SSE 端点包装。
        Mock / 无本体 / 未接地等无过程可流的情况直接产出终态 done。
        """
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")

        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            yield {
                "type": "done",
                "payload": {
                    "domain_id": domain_id,
                    "domain_name": domain.name,
                    "ontology_id": None,
                    "answer": (
                        f"「{domain.name}」当前还没有已发布的本体。"
                        "请先在「本体建模」中完成草稿编辑并发布，"
                        "Data Agent 会基于已发布本体的对象、字段、关系与业务逻辑进行解读。"
                    ),
                    "suggested_sql": None,
                    "referenced_objects": [],
                    "referenced_logics": [],
                    "used_mock": True,
                },
            }
            return

        snapshots = self._load_ontology_snapshot(db, ontology.id)
        relations = self._load_relations(db, ontology.id)
        logics = self._load_logics(db, ontology.id)
        grounded_objects = self._match_objects(question, snapshots)
        grounded_logics = self._match_logics(question, logics)
        runtime = self.settings_service.get_llm_runtime(db)
        use_mock = not runtime.api_key
        resolver = _ReferenceResolver(objects=snapshots, relations=relations, logics=logics)

        if use_mock:
            # Mock 无过程可流：直接产出终态 done（逻辑与 ask() 的 mock 分支一致）
            if not grounded_objects and not grounded_logics:
                yield {"type": "done", "payload": self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)}
                return
            payload = self._mock_answer(
                question=question, snapshots=snapshots, relations=relations, logics=logics,
                matched_objects=grounded_objects, matched_logics=grounded_logics)
            payload = resolver.resolve_payload(payload)
            payload = self._enforce_grounded_refs(
                payload, grounded_objects=grounded_objects, grounded_logics=grounded_logics, resolver=resolver)
            if not payload.get("referenced_objects") and not payload.get("referenced_logics"):
                yield {"type": "done", "payload": self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)}
                return
            if payload.get("suggested_sql"):
                payload["suggested_sql"] = _format_sql(payload["suggested_sql"])
            payload.update({"domain_id": domain_id, "domain_name": domain.name,
                            "ontology_id": ontology.id, "used_mock": True})
            yield {"type": "done", "payload": payload}
            return

        # Agent 流式路径：透传事件流，done 事件走与 ask() 相同的后处理
        payload: dict | None = None
        try:
            async for ev in self._stream_agent_events(
                db, runtime=runtime, domain=domain, ontology=ontology,
                question=question, history=history or [],
                seed_objects=grounded_objects, seed_logics=grounded_logics,
            ):
                if ev["type"] == "done":
                    payload = ev["payload"]
                else:
                    yield ev  # step_start / step_done / token 透传
        except Exception as exc:  # noqa: BLE001
            logger.exception("ChatBI agent stream failed: %s", exc)
            yield {"type": "error", "message": self._friendly_llm_error(exc)}
            payload = self._mock_answer(
                question=question, snapshots=snapshots, relations=relations, logics=logics,
                matched_objects=grounded_objects, matched_logics=grounded_logics)
            payload.setdefault("steps", [])
            payload["answer"] = f"> {self._friendly_llm_error(exc)}\n\n{payload['answer']}"

        payload = payload or {"answer": "（模型未返回回答）", "steps": []}
        payload = resolver.resolve_payload(payload)
        unverified = payload.get("_unverified") or []
        if unverified:
            payload = self._ungrounded_refusal(
                domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id,
                question=question, reasons=unverified)
        else:
            grounded = bool(
                payload.pop("_grounded", False)
                or payload.get("referenced_objects")
                or payload.get("referenced_logics")
                or payload.get("data_result")
            )
            if not grounded:
                payload = self._ungrounded_refusal(
                    domain_id=domain_id, domain_name=domain.name, ontology_id=ontology.id, question=question)
        if payload.get("suggested_sql"):
            payload["suggested_sql"] = _format_sql(payload["suggested_sql"])
        payload.update({"domain_id": domain_id, "domain_name": domain.name,
                        "ontology_id": ontology.id, "used_mock": use_mock})
        yield {"type": "done", "payload": payload}

    def suggest_questions(self, db: Session, domain_id: str) -> list[str]:
        """基于已发布本体生成若干示例提问，供前端首屏展示。"""
        ontology = self.query_service.get_published_ontology(db, domain_id)
        if not ontology:
            return [
                "当前数据域有哪些业务对象？",
                "近 7 天的订单量趋势如何？",
                "请帮我梳理支付与退款之间的业务关系。",
            ]
        snapshots = self._load_ontology_snapshot(db, ontology.id)
        logics = self._load_logics(db, ontology.id)

        suggestions: list[str] = []
        if snapshots:
            primary = snapshots[0]
            suggestions.append(f"「{primary.display_name}」包含哪些关键字段？")
            if len(snapshots) > 1:
                other = snapshots[1]
                suggestions.append(
                    f"「{primary.display_name}」和「{other.display_name}」之间有什么关系？"
                )
            if any(p.semantic_type == "amount" or "amount" in p.name for p in primary.properties):
                suggestions.append(f"最近 30 天「{primary.display_name}」的金额合计是多少？")
            else:
                suggestions.append(f"最近 30 天「{primary.display_name}」的记录数有多少？")
        for logic in logics[:2]:
            suggestions.append(f"请解释业务逻辑「{logic.display_name}」的口径。")
        if not suggestions:
            suggestions = ["当前数据域有哪些业务对象？"]
        return suggestions[:5]

    # ---------------------------------------------------------------- conversation

    def list_conversations(
        self,
        db: Session,
        domain_id: str,
        query: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        q = db.query(ChatBiConversation).filter(
            ChatBiConversation.domain_id == domain_id
        )
        if not include_archived:
            q = q.filter(ChatBiConversation.is_archived == False)  # noqa: E712
        if query:
            q = q.filter(ChatBiConversation.title.ilike(f"%{query}%"))
        q = q.order_by(
            desc(ChatBiConversation.is_pinned),
            desc(ChatBiConversation.updated_at),
        )
        conversations = q.all()

        # Bulk-fetch message counts
        conv_ids = [c.id for c in conversations]
        counts: dict[str, int] = {}
        previews: dict[str, str | None] = {}
        if conv_ids:
            count_rows = (
                db.query(
                    ChatBiMessage.conversation_id,
                    func.count(ChatBiMessage.id),
                )
                .filter(ChatBiMessage.conversation_id.in_(conv_ids))
                .group_by(ChatBiMessage.conversation_id)
                .all()
            )
            counts = {row[0]: row[1] for row in count_rows}

            # Last message preview per conversation
            preview_rows = (
                db.query(
                    ChatBiMessage.conversation_id,
                    ChatBiMessage.content,
                )
                .filter(ChatBiMessage.conversation_id.in_(conv_ids))
                .order_by(ChatBiMessage.conversation_id, desc(ChatBiMessage.created_at))
                .all()
            )
            seen: set[str] = set()
            for row in preview_rows:
                if row[0] not in seen:
                    seen.add(row[0])
                    preview = row[1][:100] if row[1] else None
                    previews[row[0]] = preview

        return [
            {
                "id": c.id,
                "domain_id": c.domain_id,
                "title": c.title,
                "category": c.category,
                "is_pinned": c.is_pinned,
                "is_archived": c.is_archived,
                "message_count": counts.get(c.id, 0),
                "last_message_preview": previews.get(c.id),
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in conversations
        ]

    def create_conversation(
        self,
        db: Session,
        domain_id: str,
        title: str | None = None,
        category: str | None = None,
    ) -> dict:
        conv = ChatBiConversation(
            domain_id=domain_id,
            title=title or "新对话",
            category=category,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return {
            "id": conv.id,
            "domain_id": conv.domain_id,
            "title": conv.title,
            "category": conv.category,
            "is_pinned": conv.is_pinned,
            "is_archived": conv.is_archived,
            "message_count": 0,
            "last_message_preview": None,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    def get_conversation(
        self, db: Session, conversation_id: str
    ) -> ChatBiConversation | None:
        return db.get(ChatBiConversation, conversation_id)

    _UNSET = object()

    def update_conversation(
        self,
        db: Session,
        conversation_id: str,
        title: str | None | object = _UNSET,
        category: str | None | object = _UNSET,
        is_pinned: bool | object = _UNSET,
        is_archived: bool | object = _UNSET,
    ) -> dict:
        conv = db.get(ChatBiConversation, conversation_id)
        if not conv:
            raise ValueError("对话不存在")
        if title is not self._UNSET:
            conv.title = title
        if category is not self._UNSET:
            conv.category = category
        if is_pinned is not self._UNSET:
            conv.is_pinned = is_pinned
        if is_archived is not self._UNSET:
            conv.is_archived = is_archived
        log_change(db, "chat_bi_conversation", conversation_id, "rename")
        db.commit()
        db.refresh(conv)
        return {
            "id": conv.id,
            "domain_id": conv.domain_id,
            "title": conv.title,
            "category": conv.category,
            "is_pinned": conv.is_pinned,
            "is_archived": conv.is_archived,
            "message_count": 0,
            "last_message_preview": None,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    def delete_conversation(self, db: Session, conversation_id: str) -> None:
        conv = db.get(ChatBiConversation, conversation_id)
        if not conv:
            raise ValueError("对话不存在")
        log_change(db, "chat_bi_conversation", conversation_id, "delete")
        db.delete(conv)
        db.commit()

    # ---------------------------------------------------------------- categories

    def list_categories(self, db: Session, domain_id: str) -> list[dict]:
        rows = (
            db.query(
                ChatBiConversation.category,
                func.count(ChatBiConversation.id),
            )
            .filter(ChatBiConversation.domain_id == domain_id)
            .group_by(ChatBiConversation.category)
            .all()
        )
        result: list[dict] = []
        for row in rows:
            cat_name = row[0] or "__uncategorized__"
            result.append({"name": cat_name, "conversation_count": row[1]})
        # Sort: uncategorized first, then alphabetically
        result.sort(key=lambda x: ("" if x["name"] == "__uncategorized__" else x["name"]))
        return result

    def rename_category(
        self, db: Session, domain_id: str, old_name: str, new_name: str
    ) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("分类名称不能为空")
        convs = (
            db.query(ChatBiConversation)
            .filter(
                ChatBiConversation.domain_id == domain_id,
                ChatBiConversation.category == old_name,
            )
            .all()
        )
        for conv in convs:
            conv.category = new_name
        db.commit()

    def delete_category(self, db: Session, domain_id: str, name: str) -> None:
        convs = (
            db.query(ChatBiConversation)
            .filter(
                ChatBiConversation.domain_id == domain_id,
                ChatBiConversation.category == name,
            )
            .all()
        )
        for conv in convs:
            conv.category = None
        db.commit()

    def save_message(
        self,
        db: Session,
        conversation_id: str,
        role: str,
        content: str,
        payload: dict | None = None,
    ) -> ChatBiMessage:
        msg = ChatBiMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            payload=json.dumps(payload) if payload else None,
        )
        db.add(msg)
        conv = db.get(ChatBiConversation, conversation_id)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(msg)
        return msg

    def get_messages(
        self, db: Session, conversation_id: str
    ) -> list[dict]:
        rows = (
            db.query(ChatBiMessage)
            .filter(ChatBiMessage.conversation_id == conversation_id)
            .order_by(ChatBiMessage.created_at)
            .all()
        )
        return [
            {
                "id": m.id,
                "conversation_id": m.conversation_id,
                "role": m.role,
                "content": m.content,
                "payload": json.loads(m.payload) if m.payload else None,
                "created_at": m.created_at,
            }
            for m in rows
        ]

    # ------------------------------------------------------------------ data

    def _load_ontology_snapshot(
        self, db: Session, ontology_id: str
    ) -> list[_ObjectSnapshot]:
        rows = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .order_by(ObjectType.display_name.asc())
            .all()
        )
        return [
            _ObjectSnapshot(
                id=o.id,
                name=o.name,
                display_name=o.display_name,
                description=o.description,
                properties=sorted(o.properties, key=lambda p: (not p.required, p.name)),
            )
            for o in rows
        ]

    def _load_relations(self, db: Session, ontology_id: str) -> list[RelationType]:
        return (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .order_by(RelationType.display_name.asc())
            .all()
        )

    def _load_logics(self, db: Session, ontology_id: str) -> list[BusinessLogic]:
        return (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .order_by(BusinessLogic.updated_at.desc())
            .all()
        )

    @staticmethod
    def _friendly_llm_error(exc: Exception) -> str:
        """把常见 LLM 失败(尤其是上下文/请求体过大)翻译成可读提示，替代笼统报错。"""
        text = str(exc).lower()
        status = getattr(exc, "status_code", None)
        size_signals = (
            "413",
            "request entity too large",
            "context length",
            "maximum context",
            "context_length_exceeded",
            "too many tokens",
            "string too long",
            "payload too large",
        )
        if status in (413, 422) or any(sig in text for sig in size_signals):
            return (
                "LLM 上下文过大：本体知识超出模型/网关的请求上限，已降级为规则匹配示例。"
                "请缩小提问范围，或联系管理员放宽端点的 body/上下文限制。"
            )
        return "LLM 调用失败，已降级为规则匹配示例。"

    # -------------------------------------------------------------- agent loop

    def _resolve_domain_data_source(self, db: Session):
        """为 run_sql 选一个可执行数据源。

        DataSource 目前无数据域绑定（schema 层缺 domain 外键），故策略：
        唯一可用源直接用；多个可用源取最近更新的一个（P1 实用取舍）；无可用源返回 None
        （run_sql 据此优雅降级为「仅建议 SQL」）。
        """
        from app.models import DataSource

        usable = [
            s
            for s in db.query(DataSource).all()
            if (s.dsn_secret_ref or "").strip() and s.kind != "mock"
        ]
        if not usable:
            return None
        if len(usable) > 1:
            usable.sort(key=lambda s: (s.updated_at or s.created_at), reverse=True)
        return usable[0]

    # --- 工具结果 compact 化：只保留 LLM 需要的字段，压住回灌体积 ---

    @staticmethod
    def _compact_object_summary(o: Any) -> dict:
        d = o.model_dump(mode="json") if hasattr(o, "model_dump") else dict(o)
        return {
            k: d.get(k)
            for k in ("id", "name", "display_name", "description", "property_count", "table_role")
        }

    @staticmethod
    def _compact_relation(r: Any) -> dict:
        d = r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r)
        return {
            k: d.get(k)
            for k in (
                "id", "name", "display_name", "source_object_name",
                "target_object_name", "cardinality", "description",
            )
        }

    def _compact_object_detail(self, detail: Any) -> dict:
        d = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else dict(detail)
        props = [
            {
                k: p.get(k)
                for k in ("name", "display_name", "data_type", "semantic_type", "required", "description")
            }
            for p in (d.get("properties") or [])
        ]
        rels = [
            self._compact_relation(r)
            for r in (d.get("outgoing_relations") or []) + (d.get("incoming_relations") or [])
        ]
        logics = [
            {"id": l.get("id"), "display_name": l.get("display_name"), "expression_summary": l.get("expression_summary")}
            for l in (d.get("business_logics") or [])
        ]
        return {
            "id": d.get("id"),
            "name": d.get("name"),
            "display_name": d.get("display_name"),
            "description": d.get("description"),
            "properties": props,
            "relations": rels,
            "business_logics": logics,
        }

    @staticmethod
    def _compact_logic_summary(l: Any) -> dict:
        d = l.model_dump(mode="json") if hasattr(l, "model_dump") else dict(l)
        return {
            k: d.get(k)
            for k in ("id", "name", "display_name", "logic_type", "expression_summary", "description")
        }

    def _compact_logic_detail(self, detail: Any) -> dict:
        d = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else dict(detail)
        base = self._compact_logic_summary(detail)
        base["expression_draft"] = d.get("expression_draft")
        base["related_objects"] = [
            {"id": o.get("id"), "display_name": o.get("display_name")}
            for o in (d.get("related_object_types") or [])
        ]
        base["related_properties"] = [
            {"display_name": p.get("display_name"), "name": p.get("name")}
            for p in (d.get("related_properties") or [])
        ]
        return base

    def _dispatch_run_sql(
        self, db: Session, *, args: dict, ontology_id: str | None = None
    ) -> tuple[Any, str, bool]:
        from app.services import data_app_executor

        sql = str(args.get("sql") or "").strip()
        if not sql:
            return {"error": "缺少 sql"}, "run_sql 缺少 sql", True
        try:
            limit = int(args.get("limit") or _RUN_SQL_LIMIT)
        except (TypeError, ValueError):
            limit = _RUN_SQL_LIMIT
        limit = max(1, min(limit, _RUN_SQL_LIMIT))

        ok, reason = data_app_executor.is_read_only(sql)
        if not ok:
            return {"executed": False, "error": f"仅允许只读 SELECT：{reason}", "sql": sql}, "被只读校验拒绝", True

        source = self._resolve_domain_data_source(db)
        mapping = _loads_payload(source.mapping_json) if source is not None else None

        # ★ SQL 语义证明（F3）：执行/建议前静态证明语义合法，不过则不放行。
        #   即便无可执行数据源（仅建议 SQL），也要证明——臆造字段/JOIN 与是否落库无关。
        rejection = self._prove_sql_or_reject(db, sql, ontology_id, source, mapping)
        if rejection is not None:
            return rejection

        if source is None:
            return (
                {"executed": False, "reason": "当前数据域未绑定可执行数据源，仅能给出建议 SQL", "sql": sql},
                "无可执行数据源",
                False,
            )
        try:
            columns, rows = data_app_executor.execute_sql(
                dsn=source.dsn_secret_ref,
                sql=sql,
                limit=limit,
                mapping=mapping or None,
                timeout_seconds=_SQL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            return {"executed": False, "error": str(exc)[:300], "sql": sql}, "SQL 执行失败", True
        return (
            {
                "executed": True,
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= limit,
            },
            f"返回 {len(rows)} 行",
            False,
        )

    def _prove_sql_or_reject(
        self, db: Session, sql: str, ontology_id: str | None, source, mapping
    ) -> tuple[Any, str, bool] | None:
        """SQL 语义证明门（F3）。返回拒绝三元组（阻断）或 None（放行）。

        受 ``settings.agent_soundness`` 开关：off=跳过；warn=只记日志不拦；on=拒绝执行。
        证明本身出错（解析器异常等）绝不误伤正常查询——降级为放行并记日志。
        """
        from app.config import settings as env_settings
        from app.services import data_app_executor

        mode = (getattr(env_settings, "agent_soundness", "on") or "on").lower()
        if mode == "off" or not ontology_id:
            return None
        try:
            proj = build_projection(db, ontology_id, mapping)
            dialect = (
                data_app_executor.backend_of(source.dsn_secret_ref)
                if source is not None
                else None
            )
            verdict = prove_sql_sound(sql, proj, dialect=dialect)
        except Exception as exc:  # noqa: BLE001 — 证明器故障不得拖垮问答
            logger.warning("sql soundness prover error, allowing SQL: %s", exc)
            return None
        if not isinstance(verdict, SqlRejection):
            return None
        if mode == "warn":
            logger.info("[soundness=warn] 本应拒答 SQL：%s | %s", verdict.code, verdict.message)
            return None
        # mode == "on"：拒绝执行。is_error=True → 不计入接地，避免用拒绝当命中。
        return (
            {"executed": False, "rejected": True, "sql": sql,
             "reason": verdict.message, "code": verdict.code},
            f"SQL 语义证明未通过：{verdict.code}",
            True,
        )

    # -------------------------------------------------------------- F4 断言级可靠性

    @staticmethod
    def _ledger_register(
        ledger: FactLedger, tool_name: str, result: Any, is_error: bool
    ) -> None:
        """把一次工具调用的**真实返回**登记进事实账本。失败/错误返回不入账。"""
        if is_error:
            return
        try:
            if tool_name == "search_objects" and isinstance(result, list):
                for o in result:
                    if isinstance(o, dict):
                        ledger.add_object_summary(o)
            elif tool_name == "get_object" and isinstance(result, dict):
                ledger.add_object_detail(result)
            elif tool_name == "search_relations" and isinstance(result, list):
                for r in result:
                    if isinstance(r, dict):
                        ledger.add_relation(r)
            elif tool_name == "search_logics" and isinstance(result, list):
                for l in result:
                    if isinstance(l, dict):
                        ledger.add_metric_summary(l)
            elif tool_name == "get_logic" and isinstance(result, dict):
                ledger.add_metric_summary(result)
            elif tool_name == "get_domain_overview" and isinstance(result, dict):
                for o in result.get("objects") or []:
                    if isinstance(o, dict):
                        ledger.add_object_summary(o)
            elif tool_name == "run_sql" and isinstance(result, dict) and result.get("executed"):
                ledger.add_cells(result.get("columns") or [], result.get("rows") or [])
        except Exception as exc:  # noqa: BLE001 — 登记失败不得拖垮问答
            logger.warning("fact ledger register failed for %s: %s", tool_name, exc)

    @staticmethod
    def _asks_number(question: str) -> bool:
        """问题是否在问具体数值/计量（决定数值断言是否要求 run_sql 凭证）。"""
        return any(k in (question or "") for k in _AGG_KEYWORDS)

    def _verify_answer(
        self, answer: str, ledger: FactLedger, question: str
    ) -> tuple[bool, list[str]]:
        """F4 校验入口。受 settings.agent_soundness 开关；off/warn 不拦，on 生效。

        返回 (ok, unverified)：ok=False 且 on 模式时，上层将拒答并展示 unverified。
        """
        from app.config import settings as env_settings

        mode = (getattr(env_settings, "agent_soundness", "on") or "on").lower()
        if mode == "off":
            return True, []
        # 数值严格模式：只要本轮真跑过数（有 cells）或问题在问量，就要求数值有凭证。
        strict = bool(ledger.cells) or self._asks_number(question)
        verdict = verify_answer(answer, ledger, strict_numbers=strict)
        if verdict.ok:
            return True, []
        if mode == "warn":
            logger.info("[soundness=warn] 答案含不可证断言：%s", "; ".join(verdict.unverified))
            return True, []  # warn 不拦
        return False, verdict.unverified

    def _dispatch_agent_tool(
        self, db: Session, *, domain_id: str, ontology_id: str, name: str, args: dict
    ) -> tuple[Any, str, bool]:
        """执行一个工具调用，返回 (结果对象, 人类可读摘要, 是否错误)。同步，供 to_thread 调用。"""
        qs = self.query_service
        try:
            if name == "search_objects":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_object_types(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_object_summary(o) for o in page.items]
                return items, f"命中 {len(items)} 个对象", False
            if name == "get_object":
                detail = qs.get_object_type(db, str(args.get("object_id") or ""))
                if not detail or detail.status != "published":
                    return {"error": "对象不存在或未发布"}, "对象未命中", True
                return self._compact_object_detail(detail), f"对象「{detail.display_name}」{len(detail.properties)} 字段", False
            if name == "search_relations":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_relation_types(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_relation(r) for r in page.items]
                return items, f"命中 {len(items)} 个关系", False
            if name == "search_logics":
                kw = str(args.get("keyword") or "").strip() or None
                page = qs.list_business_logics(
                    db, domain_context_id=domain_id, published_only=True, q=kw, limit=_SEARCH_LIMIT
                )
                items = [self._compact_logic_summary(l) for l in page.items]
                return items, f"命中 {len(items)} 个口径", False
            if name == "get_logic":
                detail = qs.get_business_logic(db, str(args.get("logic_id") or ""))
                if not detail or detail.status != "published":
                    return {"error": "业务逻辑不存在或未发布"}, "口径未命中", True
                return self._compact_logic_detail(detail), f"口径「{detail.display_name}」", False
            if name == "get_domain_overview":
                # 只统计/列举【已发布】对象与关系；grouped_graph 混入未发布草稿，不能用于概览
                obj_page = qs.list_object_types(
                    db, ontology_id=ontology_id, published_only=True, limit=100
                )
                rel_page = qs.list_relation_types(
                    db, ontology_id=ontology_id, published_only=True, limit=1
                )
                overview = {
                    "published_object_count": obj_page.total,
                    "published_relation_count": rel_page.total,
                    "objects": [
                        {"display_name": o.display_name, "name": o.name, "table_role": o.table_role}
                        for o in obj_page.items
                    ],
                    "note": "仅统计并列举【已发布(published)】的业务对象与关系；未发布的建模草稿不计入。",
                }
                return overview, f"{obj_page.total} 个已发布对象 / {rel_page.total} 个已发布关系", False
            if name == "run_sql":
                return self._dispatch_run_sql(db, args=args, ontology_id=ontology_id)
            return {"error": f"未知工具：{name}"}, "未知工具", True
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent tool %s failed: %s", name, exc)
            return {"error": str(exc)[:300]}, f"工具异常：{type(exc).__name__}", True

    @staticmethod
    def _extract_sql_from_text(text: str | None) -> str | None:
        """从答案正文里抽取 ```sql 围栏块（兜底：模型未走 run_sql 时不丢 SQL）。"""
        if not text:
            return None
        m = re.search(r"```sql\s+(.*?)```", text, re.S | re.I)
        if not m:
            m = re.search(r"```\s*(SELECT\b.*?)```", text, re.S | re.I)
        return m.group(1).strip() if m else None

    @staticmethod
    def _steps_to_caliber(
        steps: list[dict], referenced_objects: list[dict], referenced_logics: list[dict]
    ) -> list[dict]:
        """由工具轨迹派生口径拆解卡（决策点3：保留字段，内容由 steps 生成，而非逼模型吐 JSON）。"""
        obj_by_id = {o["id"]: o for o in referenced_objects if o.get("id")}
        logic_by_id = {l["id"]: l for l in referenced_logics if l.get("id")}
        items: list[dict] = []
        seen_obj: set[str] = set()
        seen_logic: set[str] = set()
        for s in steps:
            # 只保留成功步：失败的检索（如模型把英文名当 object_id 猜错）不应铸成口径卡
            if s.get("status") != "succeeded":
                continue
            tool = s.get("tool")
            a = s.get("arguments") or {}
            if tool == "get_object":
                ref = obj_by_id.get(a.get("object_id"))
                # 未解析到引用的对象卡是噪声；同一对象（失败后重试命中）只留一张
                if not ref or ref["id"] in seen_obj:
                    continue
                seen_obj.add(ref["id"])
                items.append({
                    "label": "对象口径",
                    "description": s.get("summary") or "",
                    "references": [{"kind": "object_type", **ref}],
                })
            elif tool == "get_logic":
                ref = logic_by_id.get(a.get("logic_id"))
                if not ref or ref["id"] in seen_logic:
                    continue
                seen_logic.add(ref["id"])
                items.append({
                    "label": "逻辑口径",
                    "description": s.get("summary") or "",
                    "references": [{"kind": "business_logic", **ref}],
                })
            elif tool == "run_sql":
                items.append({"label": "数据查询", "description": s.get("summary") or "", "references": []})
        return items

    async def _stream_final_answer(
        self, client: AsyncOpenAI, model: str, messages: list[dict], *, nudge: str | None = None
    ) -> AsyncIterator[str]:
        """最终作答轮：不带工具、stream=True 逐 token 产出（真·逐字流式）。"""
        msgs = messages if nudge is None else messages + [{"role": "user", "content": nudge}]
        stream = await client.chat.completions.create(model=model, messages=msgs, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta

    async def _stream_agent_events(
        self,
        db: Session,
        *,
        runtime: Any,
        domain: DomainContext,
        ontology: Ontology,
        question: str,
        history: list[dict],
        seed_objects: list[_ObjectSnapshot],
        seed_logics: list[BusinessLogic],
    ) -> AsyncIterator[dict]:
        """多步工具编排的事件流：yield step_start / step_done / token / done。

        流式与非流式共用此核心；`_run_agent_loop` 只是消费到 done 后聚合返回。
        接地判定 / 引用归一 / SQL 美化等收尾在 ask()/ask_stream() 的 done 后处理里做。
        """
        client = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.api_base_url,
            timeout=env_settings.llm_timeout_seconds,
            http_client=make_async_http_client(),
        )

        seed_lines: list[str] = []
        if seed_objects:
            seed_lines.append(
                "· 业务对象：" + "、".join(f"{o.display_name}({o.name})" for o in seed_objects[:5])
            )
        if seed_logics:
            seed_lines.append(
                "· 业务逻辑：" + "、".join(f"{l.display_name}({l.name})" for l in seed_logics[:5])
            )
        # 候选作为 system 弱提示，并明确其性质，避免模型把它当成"用户提到的对象"而答非所问
        seed_note = ""
        if seed_lines:
            seed_note = (
                "\n\n【系统预匹配候选】以下是系统按关键词猜测的可能相关实体，"
                "仅作为检索起点参考，**可能与本次问题无关**；若无关请忽略，"
                "**切勿声称用户提到了这些对象**、也不要在回答里节外生枝地回应它们：\n"
                + "\n".join(seed_lines)
            )

        messages: list[dict] = [
            {"role": "system", "content": f"{_AGENT_SYSTEM_PROMPT}\n\n当前数据域：{domain.name}{seed_note}"}
        ]
        for item in history[-6:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": str(content)})
        messages.append({"role": "user", "content": question})

        steps: list[dict] = []
        referenced_objects: list[dict] = []
        referenced_logics: list[dict] = []
        seen_obj: set[str] = set()
        seen_logic: set[str] = set()
        data_result: dict | None = None
        last_sql: str | None = None
        grounded_hit = False
        answer = ""
        ledger = FactLedger()  # F4：断言级凭证账本（只登记工具真实返回的事实）

        for _ in range(_AGENT_MAX_STEPS):
            resp = await client.chat.completions.create(
                model=runtime.model,
                messages=messages,
                tools=_AGENT_TOOL_SCHEMAS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                # 收敛：最终作答轮改流式逐字产出
                async for tok in self._stream_final_answer(client, runtime.model, messages):
                    answer += tok
                    yield {"type": "token", "delta": tok}
                answer = answer.strip()
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": t.id,
                            "type": "function",
                            "function": {"name": t.function.name, "arguments": t.function.arguments},
                        }
                        for t in tool_calls
                    ],
                }
            )
            # 工具间的模型自述：作为 thought 伪步穿插进轨迹（贴近 Claude Code 的"先说再做"），
            # 既实时流给前端、也落进 steps 持久化；_steps_to_caliber 因 tool 不匹配自动忽略它。
            thought = (msg.content or "").strip()
            if thought:
                t_idx = len(steps)
                steps.append({"index": t_idx, "kind": "thought", "tool": "", "text": thought})
                yield {"type": "thought", "index": t_idx, "text": thought}
            for t in tool_calls:
                tool_name = t.function.name
                try:
                    call_args = json.loads(t.function.arguments or "{}")
                    if not isinstance(call_args, dict):
                        call_args = {}
                except (json.JSONDecodeError, TypeError):
                    call_args = {}

                idx = len(steps)
                yield {"type": "step_start", "index": idx, "tool": tool_name, "arguments": call_args}

                result, summary, is_error = await asyncio.to_thread(
                    self._dispatch_agent_tool,
                    db,
                    domain_id=domain.id,
                    ontology_id=ontology.id,
                    name=tool_name,
                    args=call_args,
                )

                # 从工具轨迹收割结构化产物
                if not is_error and (
                    (tool_name.startswith("search_") and isinstance(result, list) and result)
                    or (tool_name in ("get_object", "get_logic", "get_domain_overview"))
                    or (tool_name == "run_sql" and isinstance(result, dict) and (result.get("executed") or result.get("sql")))
                ):
                    grounded_hit = True
                if isinstance(result, dict):
                    if tool_name == "get_object" and result.get("id") and result["id"] not in seen_obj:
                        seen_obj.add(result["id"])
                        referenced_objects.append(
                            {"id": result["id"], "name": result.get("name"), "display_name": result.get("display_name")}
                        )
                    elif tool_name == "get_logic" and result.get("id") and result["id"] not in seen_logic:
                        seen_logic.add(result["id"])
                        referenced_logics.append(
                            {"id": result["id"], "name": result.get("name"), "display_name": result.get("display_name")}
                        )
                    elif tool_name == "run_sql":
                        if result.get("sql"):
                            last_sql = result["sql"]
                        if result.get("executed"):
                            data_result = {
                                "columns": result.get("columns") or [],
                                "rows": result.get("rows") or [],
                                "truncated": bool(result.get("truncated")),
                            }

                # F4：把工具真实返回的结构化事实登记进账本（LLM 说的不入账）
                self._ledger_register(ledger, tool_name, result, is_error)

                step_status = "failed" if is_error else "succeeded"
                steps.append(
                    {"index": idx, "tool": tool_name, "arguments": call_args, "status": step_status, "summary": summary}
                )
                yield {"type": "step_done", "index": idx, "status": step_status, "summary": summary}

                result_text = json.dumps(result, ensure_ascii=False, default=str)
                if len(result_text) > _TOOL_RESULT_MAX_CHARS:
                    result_text = result_text[:_TOOL_RESULT_MAX_CHARS] + "…(结果过长已截断)"
                messages.append({"role": "tool", "tool_call_id": t.id, "content": result_text})
        else:
            # 步数耗尽仍未收敛：强制不带工具、流式收尾
            async for tok in self._stream_final_answer(
                client,
                runtime.model,
                messages,
                nudge="请基于以上工具结果直接给出最终回答，不要再调用工具。",
            ):
                answer += tok
                yield {"type": "token", "delta": tok}
            answer = answer.strip()

        # 兜底：模型若把 SQL 写进正文却没走 run_sql，从围栏块抽出，避免前端丢弃 SQL
        if not last_sql:
            last_sql = self._extract_sql_from_text(answer)

        # F4：断言级可靠性校验。答案里出现账本外的具名实体/未证实数值 → 判不可靠。
        # 受 settings.agent_soundness 开关：off 跳过；warn 仅记录不拦；on 生效。
        verify_ok, unverified = self._verify_answer(answer, ledger, question)
        grounded = grounded_hit and verify_ok

        yield {
            "type": "done",
            "payload": {
                "answer": answer or "（模型未返回回答）",
                "suggested_sql": last_sql,
                "caliber_decomposition": self._steps_to_caliber(steps, referenced_objects, referenced_logics),
                "referenced_objects": referenced_objects,
                "referenced_logics": referenced_logics,
                "steps": steps,
                "data_result": data_result,
                "_grounded": grounded,
                "_unverified": unverified,
            },
        }

    async def _run_agent_loop(
        self,
        db: Session,
        *,
        runtime: Any,
        domain: DomainContext,
        ontology: Ontology,
        question: str,
        history: list[dict],
        resolver: "_ReferenceResolver",
        seed_objects: list[_ObjectSnapshot],
        seed_logics: list[BusinessLogic],
    ) -> dict:
        """非流式包装：消费事件流、聚合 done.payload 返回（供 ask() agent 路径复用）。"""
        payload: dict | None = None
        async for ev in self._stream_agent_events(
            db,
            runtime=runtime,
            domain=domain,
            ontology=ontology,
            question=question,
            history=history,
            seed_objects=seed_objects,
            seed_logics=seed_logics,
        ):
            if ev["type"] == "done":
                payload = ev["payload"]
        return payload or {
            "answer": "（模型未返回回答）",
            "suggested_sql": None,
            "caliber_decomposition": [],
            "referenced_objects": [],
            "referenced_logics": [],
            "steps": [],
            "data_result": None,
            "_grounded": False,
        }

    # ------------------------------------------------------------------ mock

    def _mock_answer(
        self,
        *,
        question: str,
        snapshots: list[_ObjectSnapshot],
        relations: list[RelationType],
        logics: list[BusinessLogic],
        matched_objects: list[_ObjectSnapshot] | None = None,
        matched_logics: list[BusinessLogic] | None = None,
    ) -> dict:
        q_lower = question.lower()

        matched_objects = matched_objects if matched_objects is not None else self._match_objects(
            question, snapshots
        )
        if not matched_objects:
            # 调用方应已拦截无命中；此处兜底为空回答，避免回退到 snapshots[:1]
            return {
                "answer": "当前问题未能命中已发布本体中的对象或业务逻辑，无法给出基于本体的解读。",
                "suggested_sql": None,
                "caliber_decomposition": [],
                "referenced_objects": [],
                "referenced_logics": [],
            }
        primary = matched_objects[0]

        if matched_logics is None:
            matched_logics = self._match_logics(question, logics)
        matched_logics = matched_logics[:2]

        is_aggregation = any(k in q_lower for k in _AGG_KEYWORDS) or any(
            k in question for k in _AGG_KEYWORDS
        )
        time_window = self._detect_time_window(question)

        amount_prop = next(
            (p for p in primary.properties if "amount" in p.name or p.semantic_type == "amount"),
            None,
        )
        date_prop = next(
            (p for p in primary.properties if p.semantic_type == "date" or "date" in p.name),
            None,
        )

        # ---- answer text
        lines: list[str] = []
        lines.append(f"基于「{primary.display_name}」本体解读你的问题：")
        lines.append("")
        lines.append("**口径解读**")
        bullet_bits: list[str] = []
        bullet_bits.append(f"主对象：{primary.display_name}（`{primary.name}`）")
        if amount_prop:
            bullet_bits.append(f"度量字段：{amount_prop.display_name}（`{amount_prop.name}`）")
        if date_prop:
            bullet_bits.append(f"时间字段：{date_prop.display_name}（`{date_prop.name}`）")
        if time_window:
            bullet_bits.append(f"时间范围：{time_window}")
        if is_aggregation:
            bullet_bits.append("聚合方式：求和 / 计数")
        for b in bullet_bits:
            lines.append(f"- {b}")

        if matched_logics:
            lines.append("")
            lines.append("**关联业务逻辑**")
            for logic in matched_logics:
                summary = f" — {logic.expression_summary}" if logic.expression_summary else ""
                lines.append(f"- {logic.display_name}（`{logic.name}`）{summary}")
        elif logics:
            lines.append("")
            lines.append(
                f"> 当前本体共有 {len(logics)} 条业务逻辑，但未在问题中匹配到关键词，"
                "可在「业务逻辑」页确认口径。"
            )

        # ---- suggested SQL
        suggested_sql = self._build_mock_sql(
            primary=primary,
            amount_prop=amount_prop,
            date_prop=date_prop,
            is_aggregation=is_aggregation,
            time_window=time_window,
        )
        if suggested_sql:
            lines.append("")
            lines.append("**建议查询（基于本体语义，需映射到物理表后执行）**")
            lines.append("```sql")
            lines.append(suggested_sql)
            lines.append("```")

        lines.append("")
        lines.append(
            "_当前为 Mock 模式回答（未配置真实 LLM），可在「设置 → LLM 服务」中接入模型获得更智能的解读。_"
        )

        # ---- caliber decomposition
        caliber = self._build_mock_caliber(
            primary=primary,
            amount_prop=amount_prop,
            date_prop=date_prop,
            is_aggregation=is_aggregation,
            time_window=time_window,
            matched_objects=matched_objects,
            matched_logics=matched_logics,
        )

        return {
            "answer": "\n".join(lines),
            "suggested_sql": suggested_sql,
            "caliber_decomposition": caliber,
            "referenced_objects": [
                {
                    "id": o.id,
                    "name": o.name,
                    "display_name": o.display_name,
                }
                for o in matched_objects
            ],
            "referenced_logics": [
                {
                    "id": logic.id,
                    "name": logic.name,
                    "display_name": logic.display_name,
                }
                for logic in matched_logics
            ],
        }

    def _match_objects(
        self, question: str, snapshots: list[_ObjectSnapshot]
    ) -> list[_ObjectSnapshot]:
        tokens = self._tokens(question)
        if not tokens or not snapshots:
            return []
        scored: list[tuple[int, _ObjectSnapshot]] = []
        for o in snapshots:
            blob = f"{o.name} {o.display_name} {o.description or ''}".lower()
            score = sum(1 for t in tokens if t and t in blob)
            if score > 0:
                scored.append((score, o))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [o for _, o in scored[:3]]

    def _match_logics(
        self, question: str, logics: list[BusinessLogic]
    ) -> list[BusinessLogic]:
        tokens = self._tokens(question)
        if not tokens or not logics:
            return []
        matched = [
            logic
            for logic in logics
            if any(
                token
                and token
                in (logic.name + logic.display_name + (logic.description or "")).lower()
                for token in tokens
            )
        ]
        return matched[:3]

    @staticmethod
    def _ungrounded_refusal(
        *,
        domain_id: str,
        domain_name: str,
        ontology_id: str,
        question: str,
        reasons: list[str] | None = None,
    ) -> dict:
        q = (question or "").strip()
        preview = q if len(q) <= 80 else q[:80] + "…"
        if reasons:
            # F4：校验不通——拒答并逐条说明「哪句不可证」，而非笼统「无法回答」。
            bullet = "\n".join(f"  · {r}" for r in reasons)
            answer = (
                f"为避免给出不准确信息，未能基于「{domain_name}」已发布本体可靠回答该问题"
                + (f"（{preview}）" if preview else "")
                + "：以下结论无法由本体证实：\n"
                + bullet
                + "\n请换用本体中已有实体提问，或先补充/发布相关建模。"
            )
        else:
            answer = (
                f"无法基于「{domain_name}」已发布本体回答该问题"
                + (f"（{preview}）" if preview else "")
                + "：未检索到匹配的对象类型或业务逻辑。"
                "请换用本体中已有实体的名称提问，或先补充/发布相关建模。"
            )
        return {
            "domain_id": domain_id,
            "domain_name": domain_name,
            "ontology_id": ontology_id,
            "answer": answer,
            "suggested_sql": None,
            "caliber_decomposition": [],
            "referenced_objects": [],
            "referenced_logics": [],
            "used_mock": True,
            "grounding_refused": True,
        }

    @staticmethod
    def _enforce_grounded_refs(
        payload: dict,
        *,
        grounded_objects: list[_ObjectSnapshot],
        grounded_logics: list[BusinessLogic],
        resolver: "_ReferenceResolver",
    ) -> dict:
        """过滤无法落地的引用；若 LLM 未给引用则回填检索命中。"""
        cleaned_objs = [
            ref
            for ref in payload.get("referenced_objects") or []
            if isinstance(ref, dict) and ref.get("id") in resolver.obj_by_id
        ]
        if not cleaned_objs and grounded_objects:
            cleaned_objs = [
                {"id": o.id, "name": o.name, "display_name": o.display_name}
                for o in grounded_objects
            ]

        cleaned_logics = [
            ref
            for ref in payload.get("referenced_logics") or []
            if isinstance(ref, dict) and ref.get("id") in resolver.logic_by_id
        ]
        if not cleaned_logics and grounded_logics:
            cleaned_logics = [
                {
                    "id": logic.id,
                    "name": logic.name,
                    "display_name": logic.display_name,
                }
                for logic in grounded_logics
            ]

        payload["referenced_objects"] = cleaned_objs
        payload["referenced_logics"] = cleaned_logics
        return payload

    @staticmethod
    def _tokens(text: str) -> list[str]:
        # 简单中英文分词：英文按非字母数字切，中文按字符切。
        if not text:
            return []
        text = text.lower()
        alpha = re.findall(r"[a-z_][a-z0-9_]+", text)
        cjk = re.findall(r"[\u4e00-\u9fa5]", text)
        return alpha + cjk

    @staticmethod
    def _detect_time_window(question: str) -> str | None:
        if "近 7 天" in question or "近7天" in question or "最近一周" in question or "近一周" in question:
            return "近 7 天"
        if "近 30 天" in question or "近30天" in question or "最近一个月" in question or "近一个月" in question:
            return "近 30 天"
        if "今日" in question or "今天" in question:
            return "今日"
        if "本月" in question:
            return "本月"
        if "上月" in question:
            return "上月"
        if "最近" in question or "近" in question:
            return "近 7 天"
        return None

    @staticmethod
    def _build_mock_sql(
        *,
        primary: _ObjectSnapshot,
        amount_prop: Property | None,
        date_prop: Property | None,
        is_aggregation: bool,
        time_window: str | None,
    ) -> str | None:
        if not primary:
            return None
        select_parts: list[str] = []
        group_parts: list[str] = []
        where_parts: list[str] = []

        if date_prop:
            group_parts.append(date_prop.name)

        if is_aggregation:
            if amount_prop:
                select_parts.append(f"SUM({amount_prop.name}) AS total_{amount_prop.name}")
            select_parts.append(f"COUNT(*) AS record_count")
        else:
            select_parts.append(f"{primary.name}_id")
            for p in primary.properties[:4]:
                if p.name and p.name != f"{primary.name}_id":
                    select_parts.append(p.name)

        if date_prop and time_window:
            where_parts.append(f"{date_prop.name} >= DATE_SUB(CURDATE(), INTERVAL _N DAY)")
        # 替换占位 _N
        days_map = {
            "近 7 天": "7",
            "近 30 天": "30",
            "今日": "0",
            "本月": "0",
            "上月": "30",
        }
        days = days_map.get(time_window or "", "7") if time_window else None

        select_clause = ", ".join(select_parts) if select_parts else "*"
        sql_lines = [f"SELECT {select_clause}", f"FROM {primary.name}"]
        if group_parts and is_aggregation:
            sql_lines.append(f"GROUP BY {', '.join(group_parts)}")
        if where_parts:
            clause = "; ".join(where_parts)
            if days is not None:
                clause = clause.replace("_N", days)
            sql_lines.append(f"WHERE {clause}")
        sql_lines.append("LIMIT 100;")
        return "\n".join(sql_lines)

    def _build_mock_caliber(
        self,
        *,
        primary: _ObjectSnapshot,
        amount_prop: Property | None,
        date_prop: Property | None,
        is_aggregation: bool,
        time_window: str | None,
        matched_objects: list[_ObjectSnapshot],
        matched_logics: list[BusinessLogic],
    ) -> list[dict]:
        items: list[dict] = []

        items.append(
            {
                "label": "主对象",
                "description": f"查询主体为「{primary.display_name}」",
                "references": [
                    {
                        "kind": "object_type",
                        "id": primary.id,
                        "name": primary.name,
                        "display_name": primary.display_name,
                    }
                ],
            }
        )

        if amount_prop:
            items.append(
                {
                    "label": "度量字段",
                    "description": f"对「{amount_prop.display_name}」进行聚合",
                    "references": [
                        {
                            "kind": "property",
                            "id": amount_prop.id,
                            "name": amount_prop.name,
                            "display_name": amount_prop.display_name,
                        }
                    ],
                }
            )

        if date_prop:
            items.append(
                {
                    "label": "时间维度",
                    "description": f"按「{date_prop.display_name}」筛选时间范围",
                    "references": [
                        {
                            "kind": "property",
                            "id": date_prop.id,
                            "name": date_prop.name,
                            "display_name": date_prop.display_name,
                        }
                    ],
                }
            )

        if time_window:
            items.append(
                {
                    "label": "时间范围",
                    "description": f"统计窗口：{time_window}",
                    "references": [],
                }
            )

        if is_aggregation:
            items.append(
                {
                    "label": "聚合方式",
                    "description": "按主对象记录求和 / 计数",
                    "references": [],
                }
            )

        if matched_logics:
            items.append(
                {
                    "label": "关联业务逻辑",
                    "description": "回答依据以下业务逻辑口径",
                    "references": [
                        {
                            "kind": "business_logic",
                            "id": logic.id,
                            "name": logic.name,
                            "display_name": logic.display_name,
                        }
                        for logic in matched_logics
                    ],
                }
            )

        # 关联对象（除主对象外的命中对象）
        extra_objects = [o for o in matched_objects if o.id != primary.id]
        if extra_objects:
            items.append(
                {
                    "label": "关联对象",
                    "description": "问题中提到的其它业务对象",
                    "references": [
                        {
                            "kind": "object_type",
                            "id": o.id,
                            "name": o.name,
                            "display_name": o.display_name,
                        }
                        for o in extra_objects
                    ],
                }
            )

        return items



    # ---------- M4：把 suggested_sql 真正执行掉 ----------

    def execute_message_sql(
        self, db: Session, message_id: str, *, data_source_id: str, limit: int = 100
    ) -> dict:
        """执行某条回答的 ``suggested_sql``。

        在本体驱动的数仓里，这一步的准确性是架构保证的而非提示词保证的：
        物理表由本体生成，表名/列名与本体标识符天然一致，
        ``_LLM`` 那句「必须严格使用本体标识符」从祈使句变成了物理事实。

        安全：复用 ``data_app_executor`` 既有的只读校验与强制 LIMIT。
        注：执行权限应限制为 publisher 角色，但 RBAC 四层角色尚未产品化
        （见 README「安全模型（阶段性）」），当前仍由共享 Admin Token 兜底。
        """
        from app.models import DataSource
        from app.services import data_app_executor

        message = db.get(ChatBiMessage, message_id)
        if message is None:
            raise ValueError("消息不存在")
        payload = _loads_payload(message.payload)
        sql = (payload.get("suggested_sql") or "").strip()
        if not sql:
            raise ValueError("该消息没有可执行的 SQL")

        source = db.get(DataSource, data_source_id)
        if source is None:
            raise ValueError("数据源不存在")
        dsn = (source.dsn_secret_ref or "").strip()
        if not dsn or source.kind == "mock":
            raise ValueError("该数据源未配置连接串，无法执行")

        mapping = _loads_payload(source.mapping_json)
        columns, rows = data_app_executor.execute_sql(
            dsn=dsn, sql=sql, limit=limit, mapping=mapping or None
        )
        return {
            "message_id": message_id,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }


class _ReferenceResolver:
    """将 LLM/Mock 输出中的 name/display_name 解析为真实实体 id，供前端跳转。

    LLM 经常返回伪造的 id（如 "payment"、""），因此一律以本体快照为准：
    优先按 name/display_name 命中真实实体后覆写 id；命中失败时保留原 id。
    """

    def __init__(
        self,
        *,
        objects: list[_ObjectSnapshot],
        relations: list[RelationType],
        logics: list[BusinessLogic],
    ) -> None:
        self.obj_by_key: dict[str, _ObjectSnapshot] = {}
        self.obj_by_id: dict[str, _ObjectSnapshot] = {}
        for o in objects:
            self.obj_by_id[o.id] = o
            for key in (o.name, o.display_name, o.name.lower(), o.display_name.lower()):
                if key:
                    self.obj_by_key.setdefault(key, o)
        self.logic_by_key: dict[str, BusinessLogic] = {}
        self.logic_by_id: dict[str, BusinessLogic] = {}
        for logic in logics:
            self.logic_by_id[logic.id] = logic
            for key in (logic.name, logic.display_name, logic.name.lower(), logic.display_name.lower()):
                if key:
                    self.logic_by_key.setdefault(key, logic)
        self.rel_by_key: dict[str, RelationType] = {}
        for rel in relations:
            for key in (rel.name, rel.display_name, rel.name.lower(), rel.display_name.lower()):
                if key:
                    self.rel_by_key.setdefault(key, rel)
        # property: (object_id, property_name) -> Property
        self.prop_by_obj_and_name: dict[tuple[str, str], Property] = {}
        self.prop_by_name: dict[str, Property] = {}
        for o in objects:
            for p in o.properties:
                self.prop_by_obj_and_name.setdefault((o.id, p.name.lower()), p)
                self.prop_by_name.setdefault(p.name.lower(), p)
                self.prop_by_name.setdefault(p.display_name.lower(), p)

    def resolve_payload(self, payload: dict) -> dict:
        payload["referenced_objects"] = [
            r
            for r in (
                self._resolve_obj(ref) for ref in payload.get("referenced_objects") or []
            )
            if r and r.get("id") in self.obj_by_id
        ]
        payload["referenced_logics"] = [
            r
            for r in (
                self._resolve_logic(ref)
                for ref in payload.get("referenced_logics") or []
            )
            if r and r.get("id") in self.logic_by_id
        ]
        payload["caliber_decomposition"] = [
            self._resolve_caliber_item(item)
            for item in payload.get("caliber_decomposition") or []
        ]
        return payload

    def _resolve_obj(self, ref: dict) -> dict:
        ref = dict(ref)
        snap = self._find(self.obj_by_key, ref)
        if snap:
            ref["id"] = snap.id
            ref.setdefault("name", snap.name)
            ref.setdefault("display_name", snap.display_name)
        return ref

    def _resolve_logic(self, ref: dict) -> dict:
        ref = dict(ref)
        logic = self._find(self.logic_by_key, ref)
        if logic:
            ref["id"] = logic.id
            ref.setdefault("name", logic.name)
            ref.setdefault("display_name", logic.display_name)
        return ref

    def _resolve_caliber_item(self, item: dict) -> dict:
        item = dict(item)
        refs = item.get("references") or []
        resolved: list[dict] = []
        for r in refs:
            r = dict(r)
            kind = r.get("kind") or "object_type"
            if kind == "object_type":
                resolved.append(self._resolve_obj(r))
            elif kind == "business_logic":
                resolved.append(self._resolve_logic(r))
            elif kind == "relation_type":
                rel = self._find(self.rel_by_key, r)
                if rel:
                    r["id"] = rel.id
                    r.setdefault("name", rel.name)
                    r.setdefault("display_name", rel.display_name)
                resolved.append(r)
            elif kind == "property":
                prop = self._find(self.prop_by_name, r)
                if prop:
                    r["id"] = prop.id
                    r.setdefault("name", prop.name)
                    r.setdefault("display_name", prop.display_name)
                resolved.append(r)
            else:
                resolved.append(r)
        item["references"] = resolved
        return item

    @staticmethod
    def _find(index: dict, ref: dict):
        if not ref:
            return None
        for key in (ref.get("name"), ref.get("display_name"), ref.get("id")):
            if not key:
                continue
            hit = index.get(key) or index.get(str(key).lower())
            if hit:
                return hit
        return None


def _loads_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
