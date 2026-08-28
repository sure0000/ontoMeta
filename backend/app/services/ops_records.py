"""运行记录读模型注册表（Data Agent V6 P1）：把「发生过什么」接到 Agent 工具面上。

系统把运行记录记得很全——20+ 张表、40+ 个只读 REST 端点、十几个写好的读模型——
但 Data Agent 一个都读不到：用户在界面上看得见的东西，在对话里问不出来。缺的不是记录，
是读侧接线。本模块就是那层接线。

**它自己不查库、不做判定，只调既有服务并把结果装进统一信封。**
一行判定逻辑都不重写——第二份口径就是下一个 bug。落点状态归 ``object_landing``、
制品状态归 ``agent_pipeline``，这里只做投影。

信封里有两样东西是运维答案的命门，缺一不可：

- ``source``：这份事实取自哪个权威层。执行门槛权威是 ``GovernanceArtifact.status``，
  流程权威是 ``ModelingCase.stage``，观察/审计层是决策账本 + 变更日志。不标明出处就会
  答出两个互相矛盾的「真相」——典型是制品的 ``confirmed_by``：``agent_pipeline.edit()``
  改 spec 时会把它清空，所以「当初谁拍的板」**只能**查决策账本，不能查制品。
- ``as_of`` / ``observed_at``：前者是这条记录自己的时点（上次搬数成功、任务执行完成），
  后者是本次读取的时点。两者必须分开——「三天前落的数」和「我刚读到的状态」是两回事，
  合成一个字段就会把陈旧事实说成新鲜的。

注册表按「问题族」组织（见 ``docs/DATA_AGENT_V6_OPERATIONAL_RECALL_PLAN.md`` §3）。
``landing`` / ``task_run`` 回答物理落点与单任务执行，``pipeline`` / ``decision`` /
``ontology_version`` / ``standard`` 分别回读任务链、六环决策、本体发布版本与治理规约。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.services.object_landing import (
    FAILED,
    LANDED,
    NOT_LANDED,
    REGISTERED,
    SCHEMA_READY,
    STALE,
    SYNCING,
    bulk_logic_landings,
    bulk_object_landings,
)

# 状态的中文说法。**只是显示层**——状态集本身从 object_landing 导入，
# 这里不新增也不改写任何状态，避免出现第二份判定口径。
LANDING_STATE_LABELS: dict[str, str] = {
    NOT_LANDED: "未落地（本体里有这个对象，但没有任何落点登记）",
    REGISTERED: "已登记落点（起草了任务/契约，表未必建、数肯定没搬）",
    SCHEMA_READY: "表已建好，还没搬过数",
    SYNCING: "搬运/加工进行中",
    LANDED: "已落地可用",
    STALE: "落过，但上游变更后未重跑",
    FAILED: "最近一次落地失败",
}

# 制品状态的中文说法。同理，只是显示层。
ARTIFACT_STATUS_LABELS: dict[str, str] = {
    "drafted": "已起草（未确认）",
    "validated": "已校验（未确认）",
    "confirmed": "已确认（未执行）",
    "executing": "执行中",
    "succeeded": "执行成功",
    "failed": "执行失败",
}


@dataclass(frozen=True)
class RecordAnswer:
    """一族运行记录的一次读取结果。

    ``facts`` 是单值事实（这个对象落在哪张表），``items`` 是列表型事实（历史记录/清单）；
    两者都是 ``{"key", "label", "value"}`` / 自由字典的投影，**不含判定**。
    """

    family: str
    subject: str | None = None
    facts: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    # 记录自身的权威时点（上次成功搬数 / 任务执行完成）。没有就是 None——
    # 绝不用读取时间兜底，那会把「从没跑成功过」说成「刚刚还是好的」。
    as_of: datetime | None = None
    # 本次读取的时点。永远有值，答案里要说清「截至我读到的这一刻」。
    observed_at: datetime | None = None
    source: str = ""
    truncated: bool = False
    # 读到了但内容为空（如未登记落点）时，给模型一句可直接引用的说明，避免它自己编。
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "family": self.family,
            "subject": self.subject,
            "facts": _json_value(self.facts),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "observed_at": (
                self.observed_at.isoformat() if self.observed_at else None
            ),
            "source": self.source,
        }
        if self.items:
            out["items"] = _json_value(self.items)
        if self.truncated:
            out["truncated"] = True
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class RecordFamily:
    """一个问题族：问什么、由谁回答、返回里哪些字段是「事实名」。"""

    key: str
    display: str
    # 一句话，进工具 description 供模型选族。
    answers: str
    reader: Callable[[Session, dict], RecordAnswer]
    # 这些 key 的值是**真实存在的具名事实**（物理表名/任务名/DAG id），
    # 必须登记进 FactLedger，否则答案复述它们会被 F4 断言校验判成幻觉、整条被拒。
    # 这是 V6 的 F0 前置约束，``propose_*`` 工具当年就栽在这上面。
    ledger_fields: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fact(key: str, label: str, value: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "value": _json_value(value)}


def _json_value(value: Any) -> Any:
    """把权威服务返回值投影成稳定、可 JSON 化的读模型值。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _limit(params: dict, default: int = 5) -> int:
    try:
        value = int(params.get("limit") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 20))


def _limit_of(params: dict, default: int = 5) -> int:
    """兼容尚未接线的后续 reader；所有列表族共享同一上限。"""
    return _limit(params, default)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _domain_id_of(db: Session, ontology_id: str | None) -> str | None:
    """本体 → 数据域。草稿/未建模等族的既有服务按 ``domain_id`` 收参，
    而 Agent 手上只有本体作用域；一域一本体（``uq_ontology_domain_context``）
    使这步映射是确定的。"""
    if not ontology_id:
        return None
    from app.models.ontology import Ontology  # noqa: PLC0415

    ontology = db.get(Ontology, ontology_id)
    return ontology.domain_context_id if ontology else None


def _need_ontology(family: str, ontology_id: str | None) -> RecordAnswer | None:
    if ontology_id:
        return None
    return RecordAnswer(
        family=family,
        observed_at=_now(),
        note="这一族记录按数据域组织，需要先确定当前数据域（本体作用域）。",
    )


# --------------------------------------------------------------------------- landing


def read_landing(db: Session, params: dict) -> RecordAnswer:
    """A 族：这个对象/口径落到哪张物理表了、什么状态。

    委托 ``object_landing.bulk_object_landings`` / ``bulk_logic_landings``——
    两套落点登记（同步走 ``IngestionContract``、物化/清洗走 ``WarehouseObjectProjection``）
    并存是历史事实，那边已经做了「契约优先、Projection 补位」的合并，这里不再造第三份。

    ``params``：``object_id`` 或 ``logic_id``（二选一）、``subject``（主体显示名，由调用方解析）。
    """
    subject = params.get("subject") or None
    object_id = str(params.get("object_id") or "").strip()
    logic_id = str(params.get("logic_id") or "").strip()

    if logic_id:
        landing = bulk_logic_landings(db, [logic_id]).get(logic_id)
        source = "WarehouseLogicProjection（口径的 ADS 物化）"
        if landing is None:
            return RecordAnswer(
                family="landing",
                subject=subject,
                facts=[_fact("state", "落点状态", NOT_LANDED)],
                observed_at=_now(),
                source=source,
                note="这条口径没有任何 ADS 落点登记：没有指标任务把它算成表。",
            )
        return RecordAnswer(
            family="landing",
            subject=subject,
            facts=[
                _fact("state", "落点状态", landing.state),
                _fact("state_text", "状态说明", LANDING_STATE_LABELS.get(landing.state, landing.state)),
                _fact("serving_table", "ADS 表", landing.serving_table),
                _fact("status", "物化状态", landing.status),
                _fact("queryable", "现在能查吗", landing.queryable),
            ],
            as_of=landing.last_success_at,
            observed_at=_now(),
            source=source,
        )

    if not object_id:
        return RecordAnswer(
            family="landing",
            subject=subject,
            observed_at=_now(),
            source="",
            note="没给主体：需要 object_id 或 logic_id。",
        )

    landing = bulk_object_landings(db, [object_id]).get(object_id)
    source = "IngestionContract（同步）+ WarehouseObjectProjection（物化/清洗）"
    if landing is None:
        # 「未登记」是个真答案，不是错误——正是它挡住了「照命名规则拼一个表名」的编造。
        return RecordAnswer(
            family="landing",
            subject=subject,
            facts=[
                _fact("state", "落点状态", NOT_LANDED),
                _fact("state_text", "状态说明", LANDING_STATE_LABELS[NOT_LANDED]),
            ],
            observed_at=_now(),
            source=source,
            note=(
                "这个对象没有任何落点登记：既没有同步契约，也没有物化投影。"
                "它在数仓里还没有对应的物理表——不要按命名规则推测表名。"
            ),
        )

    return RecordAnswer(
        family="landing",
        subject=subject,
        facts=[
            _fact("state", "落点状态", landing.state),
            _fact("state_text", "状态说明", LANDING_STATE_LABELS.get(landing.state, landing.state)),
            _fact("ods_table", "ODS 表", landing.ods_table),
            _fact("ods_status", "同步状态", landing.ods_status),
            _fact("ods_mode", "装载模式", landing.ods_mode),
            _fact("serving_table", "服务层表", landing.serving_table),
            _fact("serving_layer", "服务层", landing.serving_layer),
            _fact("serving_status", "加工状态", landing.serving_status),
            _fact("schema_status", "建表状态", landing.schema_status),
            _fact("queryable", "现在能查吗", landing.queryable),
            _fact(
                "materialization_artifact_id",
                "物化任务 id",
                landing.materialization_artifact_id,
            ),
        ],
        as_of=landing.last_success_at,
        observed_at=_now(),
        source=source,
    )


# --------------------------------------------------------------------------- task_run


def _artifact_facts(artifact: Any) -> list[dict[str, Any]]:
    from app.services.agent_pipeline import _receipt_failure  # noqa: PLC0415

    status = artifact.status
    facts = [
        _fact("artifact_id", "任务 id", artifact.id),
        _fact("name", "任务名", artifact.name),
        _fact("kind", "任务类型", artifact.kind),
        _fact("status", "执行状态", status),
        _fact("status_text", "状态说明", ARTIFACT_STATUS_LABELS.get(status, status)),
        _fact("is_high_risk", "高风险", artifact.is_high_risk),
        _fact(
            "executed_at",
            "执行时间",
            artifact.executed_at.isoformat() if artifact.executed_at else None,
        ),
    ]
    # _receipt_failure 收的是**解析后的 dict**，不是 JSON 串——传串进去它会静默返回
    # None，于是失败任务一律显示成没有失败原因。
    receipt: Any = None
    if artifact.execution_receipt_json:
        try:
            receipt = json.loads(artifact.execution_receipt_json)
        except (TypeError, ValueError):
            receipt = None
    failure = _receipt_failure(receipt)
    if failure:
        facts.append(_fact("failure", "失败原因", failure))
    # confirmed_by/at 只能答「最近一次确认」，不能答「当初谁拍的板」：edit() 改 spec
    # 时会把这两个字段清空。审计口径在决策账本（F 族，P2 接入）。
    if artifact.confirmed_by:
        facts.append(
            _fact("confirmed_by", "最近一次确认人（改过 spec 会被清空，审计请查决策账本）",
                  artifact.confirmed_by)
        )
    return facts


def read_task_run(db: Session, params: dict) -> RecordAnswer:
    """B 族：那个任务跑完了吗、失败在哪。

    委托 ``agent_pipeline``——它的 ``get``/``list_artifacts`` 在读时会对账 Airflow DagRun
    并回写终态，所以制品状态本身就是「读时最新」的，这里不再自己去问 Airflow。

    ``params``：``artifact_id``（查单个）或 ``ontology_id``/``kind``/``limit``（列最近若干）。
    """
    from app.services.agent_pipeline import AgentPipelineService  # noqa: PLC0415

    pipeline = AgentPipelineService()
    source = "GovernanceArtifact.status（执行门槛权威，读时已对账 Airflow DagRun）"
    artifact_id = str(params.get("artifact_id") or "").strip()
    ontology_id = str(params.get("ontology_id") or "").strip() or None
    scope = str(params.get("scope") or "ontology").strip()

    if artifact_id:
        artifact = pipeline.get(db, artifact_id)
        if artifact is None:
            return RecordAnswer(
                family="task_run",
                observed_at=_now(),
                source=source,
                note=f"没有 id 为 {artifact_id} 的任务制品。",
            )
        if scope != "all" and ontology_id and artifact.ontology_id and artifact.ontology_id != ontology_id:
            return RecordAnswer(
                family="task_run",
                observed_at=_now(),
                source=source,
                note=f"任务制品 {artifact_id} 不属于当前数据域。",
            )
        return RecordAnswer(
            family="task_run",
            subject=artifact.name,
            facts=_artifact_facts(artifact),
            as_of=artifact.executed_at,
            observed_at=_now(),
            source=source,
        )

    limit = _limit(params)
    requested_ids = params.get("artifact_ids")
    requested_kind = str(params.get("kind") or "").strip() or None
    if isinstance(requested_ids, (list, tuple, set)):
        rows = [
            artifact
            for raw_id in requested_ids
            if (artifact := pipeline.get(db, str(raw_id))) is not None
            and (scope == "all" or not ontology_id or not artifact.ontology_id or artifact.ontology_id == ontology_id)
            and (not requested_kind or artifact.kind == requested_kind)
        ]
    else:
        rows = pipeline.list_artifacts(
            db,
            ontology_id=(None if scope == "all" else ontology_id),
            kind=requested_kind,
        )
    shown = rows[:limit]
    items = [
        {f["key"]: f["value"] for f in _artifact_facts(a)}
        for a in shown
    ]
    executed = [a.executed_at for a in shown if a.executed_at]
    return RecordAnswer(
        family="task_run",
        items=items,
        as_of=max(executed) if executed else None,
        observed_at=_now(),
        source=source,
        truncated=len(rows) > len(shown),
        note=None if items else "这个范围里没有任何数据任务。",
    )


# --------------------------------------------------------------------------- pipeline


def _pipeline_item(detail: dict[str, Any], *, include_steps: bool = False) -> dict[str, Any]:
    steps = detail.get("steps") or []
    item: dict[str, Any] = {
        "pipeline_id": detail.get("id"),
        "name": detail.get("name"),
        "status": detail.get("status"),
        "step_count": len(steps),
        "succeeded_step_count": sum(
            1 for step in steps if step.get("artifact_status") == "succeeded"
        ),
        "next_step_index": detail.get("next_step_index"),
        "next_blocked_reason": detail.get("next_blocked_reason"),
        "schedule_cron": detail.get("schedule_cron"),
        "compiled_dag_id": detail.get("compiled_dag_id"),
        "compiled_at": detail.get("compiled_at"),
        "created_at": detail.get("created_at"),
        "updated_at": detail.get("updated_at"),
    }
    if include_steps:
        item["steps"] = steps
    return _json_value(item)


def read_pipeline(db: Session, params: dict) -> RecordAnswer:
    """C 族：整条任务链做到哪一步、卡在哪里、各步是什么状态。"""
    from app.services.task_pipeline import TaskPipelineService  # noqa: PLC0415

    service = TaskPipelineService()
    source = "TaskPipelineService.detail（链状态由逐步治理制品状态实时聚合）"
    pipeline_id = str(params.get("pipeline_id") or "").strip()
    ontology_id = str(params.get("ontology_id") or "").strip() or None
    scope = str(params.get("scope") or "ontology").strip()

    if pipeline_id:
        try:
            detail = service.detail(db, pipeline_id)
        except LookupError:
            return RecordAnswer(
                family="pipeline",
                observed_at=_now(),
                source=source,
                note=f"没有 id 为 {pipeline_id} 的任务链。",
            )
        if (
            scope != "all"
            and ontology_id
            and detail.get("ontology_id")
            and detail["ontology_id"] != ontology_id
        ):
            return RecordAnswer(
                family="pipeline",
                observed_at=_now(),
                source=source,
                note=f"任务链 {pipeline_id} 不属于当前数据域。",
            )
        item = _pipeline_item(detail, include_steps=True)
        facts = [
            _fact("pipeline_id", "任务链 id", item["pipeline_id"]),
            _fact("name", "任务链名称", item["name"]),
            _fact("status", "整体状态", item["status"]),
            _fact("step_count", "步骤数", item["step_count"]),
            _fact("succeeded_step_count", "已成功步骤数", item["succeeded_step_count"]),
            _fact("next_step_index", "下一步序号（从 0 开始）", item["next_step_index"]),
            _fact("next_blocked_reason", "下一步阻塞原因", item["next_blocked_reason"]),
            _fact("schedule_cron", "调度周期", item["schedule_cron"]),
            _fact("compiled_dag_id", "已编译 DAG id", item["compiled_dag_id"]),
            _fact("compiled_at", "DAG 编译时间", item["compiled_at"]),
        ]
        return RecordAnswer(
            family="pipeline",
            subject=str(item.get("name") or pipeline_id),
            facts=facts,
            items=item.get("steps") or [],
            as_of=detail.get("updated_at"),
            observed_at=_now(),
            source=source,
        )

    limit = _limit(params)
    rows = service.list_pipelines(
        db,
        ontology_id=None if scope == "all" else ontology_id,
        limit=limit + 1,
    )
    details = [service.detail(db, row.id) for row in rows]
    shown = details[:limit]
    updated = [item.get("updated_at") for item in shown if item.get("updated_at")]
    return RecordAnswer(
        family="pipeline",
        items=[_pipeline_item(item) for item in shown],
        as_of=max(updated) if updated else None,
        observed_at=_now(),
        source=source,
        truncated=len(details) > len(shown),
        note=None if shown else "这个范围里没有任何任务链。",
    )


# --------------------------------------------------------------------------- decision


def read_decision(db: Session, params: dict) -> RecordAnswer:
    """F 族：当前会话六环走到哪、谁确认过、是否存在悬挂确认。"""
    from app.services.chat_bi_ledger import build_closure  # noqa: PLC0415

    source = "ChatBiDecisionRecord（当前会话追加式决策账本）"
    conversation_id = str(params.get("conversation_id") or "").strip()
    if not conversation_id:
        return RecordAnswer(
            family="decision",
            observed_at=_now(),
            source=source,
            note="决策审计只能读取当前会话，需要 conversation_id。",
        )

    closure = build_closure(db, conversation_id)
    limit = _limit(params)
    records = closure.get("records") or []
    shown = records[-limit:]
    items = [
        {
            "id": record.get("id"),
            "seq": record.get("seq"),
            "node": record.get("node"),
            "stage": record.get("stage"),
            "outcome": record.get("outcome"),
            "subject_id": record.get("subject_id"),
            "subject_role": record.get("subject_role"),
            "summary": record.get("summary"),
            "overridden_fields": record.get("overridden_fields") or [],
            "ref_kind": record.get("ref_kind"),
            "ref_id": record.get("ref_id"),
            "created_at": record.get("created_at"),
        }
        for record in shown
    ]
    created = [record.get("created_at") for record in records if record.get("created_at")]
    return RecordAnswer(
        family="decision",
        subject=conversation_id,
        facts=[
            _fact("conversation_id", "会话 id", conversation_id),
            _fact("reached_count", "已到达环数", closure.get("reached_count", 0)),
            _fact("total_count", "六环总数", closure.get("total_count", 0)),
            _fact("nodes", "六环进度", closure.get("nodes") or []),
            _fact("dangling_count", "悬挂项数", len(closure.get("dangling") or [])),
            _fact("dangling", "悬挂项", closure.get("dangling") or []),
            _fact("task_count", "关联任务数", len(closure.get("tasks") or [])),
            _fact("decision_count", "决策记录数", len(records)),
        ],
        items=items,
        as_of=max(created) if created else None,
        observed_at=_now(),
        source=source,
        truncated=len(records) > len(shown),
        note=None if records else "当前会话还没有任何人工决策记录，六环均未到达。",
    )


# --------------------------------------------------------------------------- ontology_version


_VERSION_DIFF_SECTIONS: tuple[tuple[str, str], ...] = (
    ("object_types", "业务对象"),
    ("properties", "属性"),
    ("relation_types", "关系"),
    ("business_logics", "业务口径"),
)


def read_ontology_version(db: Session, params: dict) -> RecordAnswer:
    """G 族：当前本体发布到第几版、指定版本相对上一版改了什么。"""
    from app.services.ontology_query import OntologyQueryService  # noqa: PLC0415

    source = "OntologyQueryService（本体发布快照与版本差异）"
    ontology_id = str(params.get("ontology_id") or "").strip()
    if not ontology_id:
        return RecordAnswer(
            family="ontology_version",
            observed_at=_now(),
            source=source,
            note="本体版本只能在当前本体范围内读取，需要 ontology_id。",
        )

    service = OntologyQueryService()
    ontology = service.get_ontology(db, ontology_id)
    if ontology is None:
        return RecordAnswer(
            family="ontology_version",
            subject=ontology_id,
            observed_at=_now(),
            source=source,
            note=f"没有 id 为 {ontology_id} 的本体。",
        )

    raw_version = params.get("version")
    if raw_version not in (None, ""):
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            return RecordAnswer(
                family="ontology_version",
                subject=ontology_id,
                observed_at=_now(),
                source=source,
                note=f"版本号必须是整数，收到 {raw_version!r}。",
            )
        diff = service.get_version_diff(db, ontology_id, version)
        if diff is None:
            return RecordAnswer(
                family="ontology_version",
                subject=ontology_id,
                observed_at=_now(),
                source=source,
                note=f"本体 {ontology_id} 没有发布版本 v{version}。",
            )
        payload = _json_value(diff)
        changes: list[dict[str, Any]] = []
        for section_key, section_label in _VERSION_DIFF_SECTIONS:
            section = payload.get(section_key) or {}
            for change in ("added", "removed", "modified"):
                for value in section.get(change) or []:
                    changes.append(
                        {
                            "section": section_key,
                            "section_label": section_label,
                            "change": change,
                            "value": value,
                        }
                    )
        limit = _limit(params, default=20)
        shown = changes[:limit]
        return RecordAnswer(
            family="ontology_version",
            subject=ontology_id,
            facts=[
                _fact("current_version", "当前本体版本", ontology.version),
                _fact("status", "当前本体状态", ontology.status),
                _fact("version", "所查发布版本", payload.get("version")),
                _fact("previous_version", "上一发布版本", payload.get("previous_version")),
                _fact("diff_summary", "版本差异摘要", payload.get("diff_summary")),
                _fact("operator", "发布人", payload.get("operator")),
                _fact("change_count", "变更项数", len(changes)),
            ],
            items=shown,
            as_of=diff.created_at,
            observed_at=_now(),
            source=source,
            truncated=len(changes) > len(shown),
            note=None if changes else "这个版本有发布记录，但没有结构化差异项。",
        )

    limit = _limit(params)
    versions = service.list_versions(db, ontology_id)
    shown = versions[:limit]
    return RecordAnswer(
        family="ontology_version",
        subject=ontology_id,
        facts=[
            _fact("current_version", "当前本体版本", ontology.version),
            _fact("status", "当前本体状态", ontology.status),
            _fact("published_at", "最近发布时间", ontology.published_at),
            _fact("version_count", "已发布版本数", len(versions)),
        ],
        items=[_json_value(item) for item in shown],
        as_of=ontology.published_at,
        observed_at=_now(),
        source=source,
        truncated=len(versions) > len(shown),
        note=None if versions else "这个本体还没有发布版本记录。",
    )


# --------------------------------------------------------------------------- standard


def read_standard(db: Session, params: dict) -> RecordAnswer:
    """H 族：当前生效治理规约、可用版本与发布历史。"""
    from app.services.governance_standard import GovernanceStandardService  # noqa: PLC0415

    service = GovernanceStandardService()
    active = service.get_active(db)
    available = service.available_versions()
    history = service.history(db)
    limit = _limit(params)
    shown = history[:limit]
    rules = active.all_rules()
    enforced = active.enforced_rules()
    active_record = next(
        (
            row
            for row in history
            if row.status == "published" and row.version == active.version
        ),
        None,
    )
    source = "GovernanceStandardService（代码规约注册表 + 发布审计记录）"
    return RecordAnswer(
        family="standard",
        subject=f"v{active.version}",
        facts=[
            _fact("active_version", "当前生效规约版本", active.version),
            _fact("available_versions", "代码已登记版本", available),
            _fact("rule_count", "条款数", len(rules)),
            _fact("enforced_rule_count", "强制执行条款数", len(enforced)),
            _fact(
                "enforced_rules",
                "强制执行条款",
                [
                    {
                        "code": rule.code,
                        "description": rule.description,
                        "severity": rule.severity,
                    }
                    for rule in enforced[:20]
                ],
            ),
        ],
        items=[
            {
                "id": row.id,
                "version": row.version,
                "status": row.status,
                "note": row.note,
                "activated_at": row.activated_at,
                "created_at": row.created_at,
            }
            for row in shown
        ],
        as_of=active_record.activated_at if active_record else None,
        observed_at=_now(),
        source=source,
        truncated=len(history) > len(shown),
        note=(
            None
            if history
            else "尚无规约发布记录，当前使用代码内置默认规约。"
        ),
    )


# --------------------------------------------------------------------------- draft_run


def read_draft_run(db: Session, params: dict) -> RecordAnswer:
    """E 族（生成侧）：上次草稿生成跑成了吗、跑到哪了。

    委托 ``draft_task_service.list_tasks``——按数据域组织，故先做本体→域映射。
    """
    from app.services.draft_task_service import DraftTaskService  # noqa: PLC0415

    ontology_id = str(params.get("ontology_id") or "").strip() or None
    if (miss := _need_ontology("draft_run", ontology_id)) is not None:
        return miss
    domain_id = _domain_id_of(db, ontology_id)
    source = "DraftGenerationTask（草稿生成任务表）"
    if not domain_id:
        return RecordAnswer(
            family="draft_run",
            observed_at=_now(),
            source=source,
            note="找不到这个本体所属的数据域。",
        )

    tasks = DraftTaskService().list_tasks(db, domain_id)
    if not tasks:
        return RecordAnswer(
            family="draft_run",
            observed_at=_now(),
            source=source,
            note="这个域没有任何草稿生成记录。",
        )

    limit = _limit_of(params)
    shown = tasks[:limit]
    latest = tasks[0]
    return RecordAnswer(
        family="draft_run",
        facts=[
            _fact("task_id", "最近一次生成 id", latest.id),
            _fact("status", "状态", latest.status),
            _fact("progress", "进度", latest.progress),
            _fact("scope", "生成范围", latest.scope),
            _fact("message", "说明", latest.message),
            _fact("error_summary", "失败摘要", latest.error_summary),
            _fact("evidence_count", "证据条数", latest.evidence_count),
            _fact("updated_at", "最后更新", _iso(latest.updated_at)),
        ],
        items=[
            {
                "task_id": t.id,
                "status": t.status,
                "progress": t.progress,
                "scope": t.scope,
                "error_summary": t.error_summary,
                "created_at": _iso(t.created_at),
            }
            for t in shown
        ],
        as_of=latest.updated_at,
        observed_at=_now(),
        source=source,
        truncated=len(tasks) > len(shown),
    )


# --------------------------------------------------------------------------- merge_report


def read_merge_report(db: Session, params: dict) -> RecordAnswer:
    """E 族（合并侧）：上次重新生成到底改了什么。

    委托 ``provenance_service.get_merge_report``；不传 ``task_id`` 时取该域最近一次生成。
    """
    from app.services.draft_task_service import DraftTaskService  # noqa: PLC0415
    from app.services.provenance_service import ProvenanceService  # noqa: PLC0415

    ontology_id = str(params.get("ontology_id") or "").strip() or None
    if (miss := _need_ontology("merge_report", ontology_id)) is not None:
        return miss
    domain_id = _domain_id_of(db, ontology_id)
    source = "DraftGenerationTask.merge_report_json（生成时写死的合并报告）"
    if not domain_id:
        return RecordAnswer(
            family="merge_report",
            observed_at=_now(),
            source=source,
            note="找不到这个本体所属的数据域。",
        )

    task_id = str(params.get("task_id") or "").strip()
    as_of: datetime | None = None
    if not task_id:
        tasks = DraftTaskService().list_tasks(db, domain_id)
        if not tasks:
            return RecordAnswer(
                family="merge_report",
                observed_at=_now(),
                source=source,
                note="这个域没有任何草稿生成记录，也就没有合并报告。",
            )
        task_id = tasks[0].id
        as_of = tasks[0].updated_at

    try:
        report = ProvenanceService().get_merge_report(db, domain_id, task_id)
    except ValueError:
        return RecordAnswer(
            family="merge_report",
            observed_at=_now(),
            source=source,
            note=f"这个域下没有 id 为 {task_id} 的生成任务。",
        )

    facts = [_fact("task_id", "生成任务 id", report.task_id), _fact("scope", "生成范围", report.scope)]
    for key, label, section in (
        ("object_types", "业务对象", report.object_types),
        ("properties", "属性", report.properties),
        ("relation_types", "业务关系", report.relation_types),
        ("business_logics", "业务口径", report.business_logics),
    ):
        if section:
            facts.append(_fact(f"merge_{key}", f"{label}合并", section))
    if report.summary:
        facts.append(_fact("summary", "合并摘要", report.summary))
    return RecordAnswer(
        family="merge_report",
        facts=facts,
        as_of=as_of,
        observed_at=_now(),
        source=source,
        note=None if report.summary or facts[2:] else "这次生成没有留下合并报告（可能是首次生成）。",
    )


# --------------------------------------------------------------------------- conflict


def read_conflict(db: Session, params: dict) -> RecordAnswer:
    """E 族（复核侧）：还有哪些字段级冲突等人拍板。

    委托 ``provenance_service.list_conflicts``——冲突是「重新生成时机器与人工改动
    撞车」留下的三元组（base/ours/theirs），判定在那边，这里只投影。
    """
    from app.services.provenance_service import ProvenanceService  # noqa: PLC0415

    ontology_id = str(params.get("ontology_id") or "").strip() or None
    if (miss := _need_ontology("conflict", ontology_id)) is not None:
        return miss

    result = ProvenanceService().list_conflicts(db, ontology_id)
    source = "ProvenanceMixin.conflict_json（字段级冲突三元组）"
    if not result.total:
        return RecordAnswer(
            family="conflict",
            facts=[_fact("total", "待复核冲突数", 0)],
            observed_at=_now(),
            source=source,
            note="这个本体当前没有待复核的字段级冲突。",
        )

    limit = _limit_of(params, default=10)
    shown = result.items[:limit]
    return RecordAnswer(
        family="conflict",
        facts=[_fact("total", "待复核冲突数", result.total)],
        items=[
            {
                "entity_type": item.entity_type,
                "name": item.display_name or item.name,
                "field": item.field,
                "base": item.base,
                "ours": item.ours,
                "theirs": item.theirs,
            }
            for item in shown
        ],
        observed_at=_now(),
        source=source,
        truncated=result.total > len(shown),
    )


# --------------------------------------------------------------------------- datasource


def read_datasource(db: Session, params: dict) -> RecordAnswer:
    """C 族：这些库连得上吗、上次测通是什么时候。

    委托 ``data_app.list_data_sources`` + ``serialize_data_source``——连通性判定归
    ``test_data_source``（写 ``status``/``tested_at``），这里只回读它写下的结果，
    **不主动发起拨测**（只读族不该有副作用，拨测也可能是秒级阻塞）。
    """
    from app.services.data_app import DataAppService  # noqa: PLC0415

    service = DataAppService()
    rows = service.list_data_sources(db)
    source = "DataSource.status/tested_at（上次拨测结果，本次不重新拨测）"
    keyword = str(params.get("keyword") or "").strip().lower()
    if keyword:
        rows = [d for d in rows if keyword in (d.name or "").lower()]
    if not rows:
        return RecordAnswer(
            family="datasource",
            observed_at=_now(),
            source=source,
            note=("没有名称匹配的数据源。" if keyword else "还没有配置任何数据源。"),
        )

    limit = _limit_of(params, default=10)
    shown = rows[:limit]
    tested = [d.tested_at for d in shown if d.tested_at]
    items = []
    for ds in shown:
        serialized = service.serialize_data_source(ds)
        items.append(
            {
                "id": serialized.get("id"),
                "name": serialized.get("name"),
                "kind": serialized.get("kind"),
                "purpose": serialized.get("purpose"),
                "status": serialized.get("status"),
                "enabled": serialized.get("enabled"),
                "catalog_name": serialized.get("catalog_name"),
                "database": serialized.get("database"),
                "tested_at": _iso(serialized.get("tested_at")),
            }
        )
    facts = []
    if len(shown) == 1:
        facts = [_fact(k, k, v) for k, v in items[0].items()]
    return RecordAnswer(
        family="datasource",
        subject=items[0]["name"] if len(shown) == 1 else None,
        facts=facts,
        items=items if len(shown) > 1 else [],
        as_of=max(tested) if tested else None,
        observed_at=_now(),
        source=source,
        truncated=len(rows) > len(shown),
    )


# --------------------------------------------------------------------------- data_app


def read_data_app(db: Session, params: dict) -> RecordAnswer:
    """J 族：这个看板/数据表发了几版、当前发布的是哪版。

    委托 ``data_app.list_apps`` / ``list_versions``。
    """
    from app.services.data_app import DataAppService  # noqa: PLC0415

    ontology_id = str(params.get("ontology_id") or "").strip() or None
    domain_id = _domain_id_of(db, ontology_id)
    service = DataAppService()
    source = "DataApp / DataAppVersion（发布快照）"

    app_id = str(params.get("app_id") or "").strip()
    keyword = str(params.get("keyword") or "").strip().lower()
    apps = service.list_apps(db, domain_id=domain_id)
    if app_id:
        apps = [a for a in apps if a.id == app_id]
    elif keyword:
        apps = [a for a in apps if keyword in (a.name or "").lower()]

    if not apps:
        return RecordAnswer(
            family="data_app",
            observed_at=_now(),
            source=source,
            note="没有匹配的数据应用。",
        )

    if len(apps) == 1:
        app = apps[0]
        versions = service.list_versions(db, app.id)
        return RecordAnswer(
            family="data_app",
            subject=app.name,
            facts=[
                _fact("app_id", "应用 id", app.id),
                _fact("name", "应用名", app.name),
                _fact("app_type", "类型", app.app_type),
                _fact("status", "状态", app.status),
                _fact("current_version", "当前编辑版本", app.current_version),
                _fact("published_version", "已发布版本", app.published_version),
                _fact("published_at", "发布时间", _iso(app.published_at)),
                _fact("version_count", "累计发布版本数", len(versions)),
            ],
            items=[
                {
                    "version": v.version,
                    "diff_summary": v.diff_summary,
                    "operator": v.operator,
                    "created_at": _iso(v.created_at),
                }
                for v in versions[: _limit_of(params)]
            ],
            as_of=app.published_at,
            observed_at=_now(),
            source=source,
        )

    limit = _limit_of(params, default=10)
    shown = apps[:limit]
    published = [a.published_at for a in shown if a.published_at]
    return RecordAnswer(
        family="data_app",
        items=[
            {
                "app_id": a.id,
                "name": a.name,
                "app_type": a.app_type,
                "status": a.status,
                "published_version": a.published_version,
                "published_at": _iso(a.published_at),
            }
            for a in shown
        ],
        as_of=max(published) if published else None,
        observed_at=_now(),
        source=source,
        truncated=len(apps) > len(shown),
    )


# --------------------------------------------------------------------------- component


def read_component(db: Session, params: dict) -> RecordAnswer:
    """C 族（组件侧）：依赖组件装好了吗、部署失败在哪。

    委托 ``dependency_service.list_components`` + ``to_out``——**必须过 to_out**，
    它做了口令/密钥脱敏；直接读 ``connection_json`` 会把明文密码带进对话上下文。
    """
    from app.services.dependency_service import (  # noqa: PLC0415
        DependencyComponentService,
    )

    service = DependencyComponentService()
    rows = service.list_components(db)
    source = "DependencyComponent.deploy_status（上次部署结果）"
    key = str(params.get("component_key") or params.get("keyword") or "").strip().lower()
    if key:
        rows = [
            r
            for r in rows
            if key in (r.key or "").lower() or key in (r.name or "").lower()
        ]
    if not rows:
        return RecordAnswer(
            family="component",
            observed_at=_now(),
            source=source,
            note=("没有匹配的依赖组件。" if key else "还没有登记任何依赖组件。"),
        )

    limit = _limit_of(params, default=10)
    shown = rows[:limit]
    outs = [service.to_out(r) for r in shown]
    updated = [r.updated_at for r in shown if r.updated_at]
    items = [
        {
            "key": o.get("key"),
            "name": o.get("name"),
            "deploy_mode": o.get("deploy_mode"),
            "deploy_status": o.get("deploy_status"),
            "deploy_error": o.get("deploy_error"),
            "enabled": o.get("enabled"),
            "updated_at": _iso(o.get("updated_at")),
        }
        for o in outs
    ]
    facts = [_fact(k, k, v) for k, v in items[0].items()] if len(shown) == 1 else []
    return RecordAnswer(
        family="component",
        subject=items[0]["name"] if len(shown) == 1 else None,
        facts=facts,
        items=items if len(shown) > 1 else [],
        as_of=max(updated) if updated else None,
        observed_at=_now(),
        source=source,
    )


# --------------------------------------------------------------------------- migration


def read_migration(db: Session, params: dict) -> RecordAnswer:
    """K 族：生产割接到第几步、谁批的、观察窗还剩多久。

    委托 ``warehouse_migration.serialize``——步骤名与 next_step 都由那边算，
    证据行不可变（``WarehouseMigrationEvidence``），这里只投影。
    """
    from app.models.warehouse import WarehouseMigrationBatch  # noqa: PLC0415
    from app.services.warehouse_migration import (  # noqa: PLC0415
        WarehouseMigrationService,
    )

    source = "WarehouseMigrationBatch + WarehouseMigrationEvidence（不可变证据）"
    ontology_id = str(params.get("ontology_id") or "").strip() or None
    batch_id = str(params.get("batch_id") or "").strip()

    query = db.query(WarehouseMigrationBatch)
    if batch_id:
        query = query.filter(WarehouseMigrationBatch.id == batch_id)
    elif ontology_id:
        query = query.filter(WarehouseMigrationBatch.ontology_id == ontology_id)
    batches = query.order_by(WarehouseMigrationBatch.created_at.desc()).all()
    if not batches:
        return RecordAnswer(
            family="migration",
            observed_at=_now(),
            source=source,
            note="没有生产割接批次记录：这个本体还没走过割接流程。",
        )

    service = WarehouseMigrationService()
    latest = batches[0]
    data = service.serialize(db, latest)
    timeline = data.get("timeline") or []
    limit = _limit_of(params, default=10)
    shown_timeline = timeline[-limit:]
    return RecordAnswer(
        family="migration",
        subject=data.get("id"),
        facts=[
            _fact("batch_id", "批次 id", data.get("id")),
            _fact("status", "割接状态", data.get("status")),
            _fact("current_step", "当前步骤", data.get("current_step")),
            _fact("next_step", "下一步", data.get("next_step")),
            _fact("ontology_version", "割接的本体版本", data.get("ontology_version")),
            _fact("approver", "审批人", data.get("approver")),
            _fact("approved_by", "实际批准人", data.get("approved_by")),
            _fact("rollback_owner", "回滚责任人", data.get("rollback_owner")),
            _fact("cutover_at", "割接时间", _iso(data.get("cutover_at"))),
            _fact("observation_ends_at", "观察窗结束", _iso(data.get("observation_ends_at"))),
            _fact("blocked_reason", "阻塞原因", data.get("blocked_reason")),
            _fact("batch_count", "该范围内批次数", len(batches)),
        ],
        items=[
            {
                "step": row.get("step"),
                "name": row.get("name"),
                "attempt": row.get("attempt"),
                "status": row.get("status"),
                "recorded_by": row.get("recorded_by"),
                "recorded_at": _iso(row.get("recorded_at")),
            }
            for row in shown_timeline
        ],
        as_of=data.get("updated_at") if isinstance(data.get("updated_at"), datetime) else None,
        observed_at=_now(),
        source=source,
        truncated=len(timeline) > len(shown_timeline),
    )


# --------------------------------------------------------------------------- 注册表

REGISTRY: dict[str, RecordFamily] = {
    "landing": RecordFamily(
        key="landing",
        display="物理落点",
        answers="这个对象/口径落到哪张物理表了、表建了吗、数搬了吗、现在能不能查",
        reader=read_landing,
        ledger_fields=("subject", "ods_table", "serving_table", "materialization_artifact_id"),
    ),
    "task_run": RecordFamily(
        key="task_run",
        display="任务执行",
        answers="那个数据任务跑完了吗、卡在哪一步、失败原因是什么",
        reader=read_task_run,
        ledger_fields=("subject", "name", "artifact_id", "failure", "confirmed_by"),
    ),
    "pipeline": RecordFamily(
        key="pipeline",
        display="任务链",
        answers="整条任务链做到哪一步、每一步什么状态、为什么阻塞、调度或 DAG 是否已编译",
        reader=read_pipeline,
        ledger_fields=(
            "subject", "name", "pipeline_id", "next_blocked_reason", "compiled_dag_id",
            "artifact_id", "artifact_name",
        ),
    ),
    "decision": RecordFamily(
        key="decision",
        display="决策审计",
        answers="当前会话六环走到哪、谁确认过、是否有确认后未执行等悬挂项",
        reader=read_decision,
        ledger_fields=(
            "subject", "conversation_id", "subject_id", "subject_role", "summary", "ref_id",
        ),
    ),
    "ontology_version": RecordFamily(
        key="ontology_version",
        display="本体版本",
        answers="当前本体发布到第几版、有哪些发布版本、指定版本相对上一版改了什么",
        reader=read_ontology_version,
        ledger_fields=("subject", "diff_summary", "operator"),
    ),
    "standard": RecordFamily(
        key="standard",
        display="治理规约",
        answers="当前生效哪版治理规约、有哪些强制条款、历次发布记录是什么",
        reader=read_standard,
        ledger_fields=("subject", "active_version", "version", "note", "code", "description"),
    ),
}


def ledger_names(result: dict) -> list[str]:
    """从一次读取结果里挑出「事实名」，供 ``FactLedger.add_context_name`` 登记。

    F0 前置约束：不做这步，答案里复述的物理表名 / 任务名 / 制品 id 会被 F4 断言校验
    判成幻觉，**整条回答被拒**。走 ``RecordFamily.ledger_fields`` 声明，
    不在 chat_bi 里硬编字段名——加新族时只改注册表一处。
    """
    if not isinstance(result, dict):
        return []
    fam = REGISTRY.get(str(result.get("family") or ""))
    if fam is None:
        return []
    names: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in fam.ledger_fields and child is not None and not isinstance(
                    child, (dict, list, tuple, set)
                ):
                    names.append(str(child))
                _walk(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                _walk(child)

    for key in fam.ledger_fields:
        if result.get(key) is not None:  # subject 这类平铺在信封顶层
            names.append(str(result[key]))
    for fct in result.get("facts") or []:
        if isinstance(fct, dict) and fct.get("key") in fam.ledger_fields:
            if fct.get("value") is not None:
                names.append(str(fct["value"]))
        if isinstance(fct, dict):
            _walk(fct.get("value"))
    for item in result.get("items") or []:
        _walk(item)
    return [n for n in names if n]


def ledger_values(result: dict) -> list[Any]:
    """提取 facts/items 的原始叶子值，供 FactLedger 校验数值断言。

    ``ledger_names`` 解决具名实体，当前函数解决「共 6 环」「有 3 个版本」这类数值。
    只遍历权威 reader 的值，不登记 key/label，也不碰模型生成文本。
    """
    if not isinstance(result, dict) or str(result.get("family") or "") not in REGISTRY:
        return []

    values: list[Any] = []

    def _walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for child in value.values():
                _walk(child)
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                _walk(child)
            return
        if isinstance(value, (str, int, float, bool)):
            values.append(value)

    for fact in result.get("facts") or []:
        if isinstance(fact, dict):
            _walk(fact.get("value"))
    for item in result.get("items") or []:
        if isinstance(item, dict):
            for value in item.values():
                _walk(value)
    return values
