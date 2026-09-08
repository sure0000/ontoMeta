"""智能关系补充：把「键族 + LLM 判定」落成待人工确认的候选。

一次推断的全过程：

    DataHub bundle（走 datahub_bundle_cache，首次分钟级、之后秒回）
        → key_family.build_key_families   机械闸门 + 聚族（4968 字段 → 12 族）
        → key_family_verdict.judge_families  LLM 判真伪 + 命名 + 谓词（一次请求）
        → RelationCandidateFamily            落库，state=proposed
        → （P3）画布确认 →（P3/P4）展开成两两关系并落地

**只落候选，不写任何外部系统**：与代码包扫描的 preview/apply 分离同构。这一步跑完
DataHub 那边一个字节都没变。

**人工表态优先于机器**：重跑推断时，已经 confirmed/rejected 的族按 ``family_id`` 保留
原表态，只更新机器那部分（判定、成员、置信度）。机器不覆盖人的结论——这是本仓
既有的三级字段权威口径（见 ONTOLOGY_LIFECYCLE_REDESIGN）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import datahub as dh
from app.models import (
    ACTIVE_INFERENCE_STATUSES,
    DomainContext,
    RelationCandidateFamily,
    RelationInferenceTask,
)
from app.services import (
    datahub_bundle_cache,
    key_family,
    key_family_verdict,
    lineage_inventory,
    observed_joins,
)
from app.services.key_family import KeyColumn, KeyFamily
from app.services.observed_joins import ObservedJoin
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.relation_inference")

#: 人已经表过态的状态——重跑推断不覆盖。
DECIDED_STATES = frozenset({"confirmed", "rejected", "applied"})


def _utc_naive() -> datetime:
    """写进 naive DateTime 列的时间戳：UTC，去掉 tzinfo（与 lineage_package 同一口径）。"""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class InferenceResult:
    run_id: str
    families: list[RelationCandidateFamily]
    cached_bundle: bool
    total_fields: int
    total_tables: int
    dropped: dict[str, int]


def members_payload(family: KeyFamily) -> str:
    """成员列表落库。**带 distinct 与 rows**——基数是按它们算的，不能只存列名。"""
    return json.dumps(
        [
            {
                "table": member.table,
                "column": member.column,
                "distinct": member.distinct,
                "rows": member.rows,
                "distinct_ratio": round(member.distinct_ratio, 4),
                "near_unique": member.near_unique,
            }
            for member in family.members
        ],
        ensure_ascii=False,
    )


def _adjust_wide_family_verdict(
    family: KeyFamily, verdict: key_family_verdict.FamilyVerdict, total_tables: int
) -> tuple[float, str]:
    """对覆盖域过大的族降置信，并把风险写进人工复核理由。"""
    if total_tables <= 0:
        return verdict.confidence, verdict.reason
    ratio = len(family.tables) / total_tables
    if ratio <= key_family.WIDE_FAMILY_RATIO:
        return verdict.confidence, verdict.reason
    confidence = round(
        verdict.confidence * key_family.WIDE_FAMILY_CONFIDENCE_FACTOR, 4
    )
    note = (
        f"该族覆盖域内 {len(family.tables)}/{total_tables} 张表（{ratio:.0%}），"
        "覆盖范围过大，已下调置信度并保留人工确认"
    )
    reason = f"{verdict.reason}；{note}" if verdict.reason else note
    return confidence, reason


def load_members(row: RelationCandidateFamily) -> list[KeyColumn]:
    """候选行里的成员 → ``KeyColumn``，供展开两两关系时算基数与方向。"""
    members: list[KeyColumn] = []
    for item in json.loads(row.members_json or "[]"):
        members.append(
            KeyColumn(
                table=item["table"],
                column=item["column"],
                data_type=None,
                distinct=int(item.get("distinct") or 0),
                rows=int(item.get("rows") or 0),
                samples=(),
            )
        )
    return members


def expand_pairs(row: RelationCandidateFamily) -> list[dict]:
    """把一个族展开成两两关系（含算出来的基数与方向）。

    **在展开这一步才算两两**：候选按族存，人也按族审；展开只在需要落地/预览时做。
    同一张表内的两列不配对（那是自关联，不是表间关系）。
    """
    members = load_members(row)
    pairs: list[dict] = []
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            if left.table == right.table:
                continue
            source, target = key_family.orient_reference(left, right)
            pairs.append(
                {
                    "source_table": source.table,
                    "source_column": source.column,
                    "target_table": target.table,
                    "target_column": target.column,
                    "cardinality": key_family.cardinality_between(source, target),
                    # 两端都不唯一 = 经由共享实体的多对多，按桥表语义记；
                    # 有主端则是常规外键式引用。
                    "structure_type": (
                        "bridge_table"
                        if not (source.near_unique or target.near_unique)
                        else "foreign_key"
                    ),
                }
            )
    return pairs


def _existing_by_family(
    db: Session, domain_id: str
) -> dict[str, RelationCandidateFamily]:
    rows = db.execute(
        select(RelationCandidateFamily).where(
            RelationCandidateFamily.domain_context_id == domain_id
        )
    ).scalars()
    return {row.family_id: row for row in rows}


async def infer(
    db: Session,
    domain_id: str,
    *,
    refresh: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> InferenceResult:
    """跑一次推断并落候选。只写本地库，不碰 DataHub。

    ``progress(百分比, 说明)`` 由任务包装层传入——LLM 那一步实测数百秒，
    界面上不给交代人会以为卡死了。
    """
    def report(percent: int, message: str) -> None:
        if progress is not None:
            progress(percent, message)

    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise ValueError("数据域不存在")

    report(10, "正在取 DataHub 元数据…")
    bundle, cached = await datahub_bundle_cache.fetch(db, domain, refresh=refresh)

    report(30, f"正在聚类键族（{len(bundle.datasets)} 张表）…")
    families, stats = key_family.build_key_families(bundle.datasets)
    if not families:
        return InferenceResult(
            run_id=str(uuid.uuid4()),
            families=[],
            cached_bundle=cached,
            total_fields=sum(len(ds.fields) for ds in bundle.datasets),
            total_tables=len(bundle.datasets),
            dropped=stats.as_dict(),
        )

    report(
        45,
        f"正在请 LLM 判定 {len(families)} 个键族（整域一次长生成，通常需要几分钟）…",
    )
    runtime = SettingsService().get_llm_runtime(db)
    verdicts = await key_family_verdict.judge_families(families, runtime)

    report(85, "正在落候选…")
    run_id = str(uuid.uuid4())
    existing = _existing_by_family(db, domain_id)
    rows: list[RelationCandidateFamily] = []

    for family, verdict in zip(families, verdicts, strict=True):
        row = existing.get(family.id)
        if row is None:
            row = RelationCandidateFamily(
                domain_context_id=domain_id, family_id=family.id
            )
            db.add(row)
        elif row.state not in DECIDED_STATES:
            # 未表态的候选可以整行刷新；已表态的只更新机器那部分（见下），state 不动。
            row.state = "proposed"

        row.run_id = run_id
        row.value_shape = family.value_shape
        row.sample_values_json = json.dumps(
            list(family.sample_values), ensure_ascii=False
        )
        row.members_json = members_payload(family)
        row.table_count = len(family.tables)
        row.column_count = len(family.members)
        row.verdict = verdict.verdict
        row.entity_name = verdict.entity_name or None
        row.key_name = verdict.key_name or None
        row.predicate = verdict.predicate or None
        row.confidence, adjusted_reason = _adjust_wide_family_verdict(
            family, verdict, len(bundle.datasets)
        )
        row.reason = adjusted_reason or None
        rows.append(row)

    # 本轮没再出现的族：机器已经不认它了，但人的表态要留着——只清未表态的。
    current = {family.id for family in families}
    for family_id, row in existing.items():
        if family_id not in current and row.state not in DECIDED_STATES:
            db.delete(row)

    db.commit()
    for row in rows:
        db.refresh(row)

    logger.info(
        "域 %s 推断完成：%d 个族（实体键 %d / 码表 %d / 非键 %d）",
        domain.name,
        len(rows),
        sum(r.verdict == key_family_verdict.VERDICT_ENTITY_KEY for r in rows),
        sum(r.verdict == key_family_verdict.VERDICT_DIMENSION_CODE for r in rows),
        sum(r.verdict == key_family_verdict.VERDICT_NOT_A_KEY for r in rows),
    )
    return InferenceResult(
        run_id=run_id,
        families=rows,
        cached_bundle=cached,
        total_fields=sum(len(ds.fields) for ds in bundle.datasets),
        total_tables=len(bundle.datasets),
        dropped=stats.as_dict(),
    )


# --------------------------------------------------------------------------- 异步任务


class InferenceAlreadyRunning(RuntimeError):
    """同一个域已有推断在跑。再起一个只会两轮都写同一批候选，互相覆盖。"""


def _set_task(
    db: Session,
    task_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    error: str | None = None,
    run_id: str | None = None,
    summary: dict | None = None,
) -> None:
    task = db.get(RelationInferenceTask, task_id)
    if task is None:
        return
    if status is not None:
        task.status = status
    if progress is not None:
        task.progress = progress
    if message is not None:
        task.message = message
    if error is not None:
        task.error_summary = error
    if run_id is not None:
        task.run_id = run_id
    if summary is not None:
        task.summary_json = json.dumps(summary, ensure_ascii=False)
    db.commit()


def start_inference(db: Session, domain_id: str, *, refresh: bool = False) -> RelationInferenceTask:
    """建任务并在后台跑。**同域串行**：已有在跑的直接抛，不排队。

    调用方拿到 task_id 后轮询 :func:`get_task`。
    """
    if db.get(DomainContext, domain_id) is None:
        raise ValueError("数据域不存在")

    running = db.execute(
        select(RelationInferenceTask).where(
            RelationInferenceTask.domain_context_id == domain_id,
            RelationInferenceTask.status.in_(list(ACTIVE_INFERENCE_STATUSES)),
        )
    ).scalars().first()
    if running is not None:
        raise InferenceAlreadyRunning(
            f"该数据域已有推断在进行中（任务 {running.id}），请等它跑完"
        )

    task = RelationInferenceTask(
        domain_context_id=domain_id, status="queued", progress=0, message="排队中"
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    asyncio.create_task(_run_inference_task(task.id, domain_id, refresh))
    return task


async def _run_inference_task(task_id: str, domain_id: str, refresh: bool) -> None:
    """后台执行体。**自开会话**——请求那个 session 在响应返回后就关了。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _set_task(db, task_id, status="running", progress=5, message="正在取元数据…")
        result = await infer(
            db, domain_id, refresh=refresh, progress=lambda p, m: _set_task(db, task_id, progress=p, message=m)
        )
        summary = {
            "families": len(result.families),
            "entity_key": sum(
                r.verdict == key_family_verdict.VERDICT_ENTITY_KEY for r in result.families
            ),
            "dimension_code": sum(
                r.verdict == key_family_verdict.VERDICT_DIMENSION_CODE
                for r in result.families
            ),
            "not_a_key": sum(
                r.verdict == key_family_verdict.VERDICT_NOT_A_KEY for r in result.families
            ),
            "tables": result.total_tables,
            "fields": result.total_fields,
            "dropped": result.dropped,
        }
        _set_task(
            db,
            task_id,
            status="succeeded",
            progress=100,
            message=f"完成：{len(result.families)} 个键族",
            run_id=result.run_id,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — 失败要如实写进任务，不能只在日志里
        logger.exception("关系推断任务失败 task_id=%s domain_id=%s", task_id, domain_id)
        _set_task(
            db,
            task_id,
            status="failed",
            message="推断失败",
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
    finally:
        db.close()


def get_task(db: Session, task_id: str) -> RelationInferenceTask | None:
    return db.get(RelationInferenceTask, task_id)


def latest_task(db: Session, domain_id: str) -> RelationInferenceTask | None:
    """该域最近一次推断任务——页面一进来就靠它判断「是不是还在跑」。"""
    return (
        db.execute(
            select(RelationInferenceTask)
            .where(RelationInferenceTask.domain_context_id == domain_id)
            .order_by(RelationInferenceTask.created_at.desc())
        )
        .scalars()
        .first()
    )


def recover_stale_inference_tasks() -> int:
    """进程启动时把残留的 queued/running 任务标记为失败。

    推断跑在 API 进程里（见 ``models/relation_inference_task`` 的说明），
    热重载或异常退出会把它打断，留下永远不动的「进行中」——那会把同域的下一次
    推断永久挡在 :class:`InferenceAlreadyRunning` 外面。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        stale = (
            db.execute(
                select(RelationInferenceTask).where(
                    RelationInferenceTask.status.in_(list(ACTIVE_INFERENCE_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        for task in stale:
            task.status = "failed"
            task.message = "服务进程重启，推断已中断"
            task.error_summary = "进程重启（开发热重载或异常退出），请重新触发。"
        if stale:
            db.commit()
            logger.warning("回收了 %d 个中断的关系推断任务", len(stale))
        return len(stale)
    finally:
        db.close()


# --------------------------------------------------------------------------- 查询与表态


def list_candidates(
    db: Session, domain_id: str, *, verdict: str | None = None, state: str | None = None
) -> list[RelationCandidateFamily]:
    """按置信度降序列候选（同置信度按成员数降序——大族先审，收益高）。"""
    stmt = select(RelationCandidateFamily).where(
        RelationCandidateFamily.domain_context_id == domain_id
    )
    if verdict:
        stmt = stmt.where(RelationCandidateFamily.verdict == verdict)
    if state:
        stmt = stmt.where(RelationCandidateFamily.state == state)
    rows = list(db.execute(stmt).scalars())
    rows.sort(key=lambda r: (-(r.confidence or 0), -(r.column_count or 0)))
    return rows


def confirmed_joins(db: Session, domain_id: str) -> list[ObservedJoin]:
    """已确认的候选 → 本体证据要的关联形态。

    **这就是「落地」本身**：方案 §5.6 选的落点 (c) 是「本地关联关系表接进
    evidence_builder」，而 ``evidence_builder`` 已经有一条吃 ``ObservedJoin`` 的路
    （P0 为代码包关联键铺的）。所以人一按「确认」，下次生成草稿就会带上这批关系——
    不需要再有一个单独的 apply 动作。``applied`` 状态留给「写回 DataHub」（P4）。

    ``dimension_code`` 的族也算数：它连接的是码表，同样是真实的引用关系。
    ``not_a_key`` 不会出现在这里——它连确认都不允许。
    """
    rows = db.execute(
        select(RelationCandidateFamily).where(
            RelationCandidateFamily.domain_context_id == domain_id,
            RelationCandidateFamily.state.in_(("confirmed", "applied")),
        )
    ).scalars()

    joins: list[ObservedJoin] = []
    for row in rows:
        label = row.key_name or row.entity_name or row.value_shape
        for pair in expand_pairs(row):
            joins.append(
                ObservedJoin(
                    left_table=pair["source_table"],
                    left_column=pair["source_column"],
                    right_table=pair["target_table"],
                    right_column=pair["target_column"],
                    source_file=f"智能关系补充·{label}",
                    origin=observed_joins.ORIGIN_CONFIRMED_INFERENCE,
                    confidence=observed_joins.CONFIRMED_INFERENCE_CONFIDENCE,
                )
            )
    return joins


async def apply_confirmed_to_datahub(
    db: Session,
    domain_id: str,
    *,
    candidate_ids: list[str] | None = None,
) -> dict:
    """将已确认键族写回 DataHub ``schemaMetadata.foreignKeys``。

    写回按源 dataset 合并，避免同一张表因多个键族产生重复 REST 请求。任何一组
    aspect 写回失败都会逐约束记录错误，候选保持 ``confirmed``，下次可安全重试；
    只有该候选的全部约束成功才转为 ``applied``。
    """
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise ValueError("数据域不存在")

    stmt = select(RelationCandidateFamily).where(
        RelationCandidateFamily.domain_context_id == domain_id,
        RelationCandidateFamily.state == "confirmed",
    )
    if candidate_ids is not None:
        if not candidate_ids:
            return {"attempted": 0, "applied": 0, "failed": 0, "candidates_applied": 0, "failures": []}
        stmt = stmt.where(RelationCandidateFamily.id.in_(candidate_ids))
    rows = list(db.execute(stmt).scalars())
    if candidate_ids is not None and len(rows) != len(set(candidate_ids)):
        raise ValueError("存在不属于本域或未确认的候选")
    if not rows:
        return {"attempted": 0, "applied": 0, "failed": 0, "candidates_applied": 0, "failures": []}

    inventory = await lineage_inventory.get_inventory(db, domain_id)
    groups: dict[str, list[tuple[RelationCandidateFamily, dict, dict[str, str]]]] = {}
    candidate_total: dict[str, int] = {row.id: 0 for row in rows}
    candidate_applied: dict[str, int] = {row.id: 0 for row in rows}
    failures: list[dict[str, str]] = []

    for row in rows:
        for pair in expand_pairs(row):
            candidate_total[row.id] += 1
            source_urn = inventory.resolve(pair["source_table"])
            target_urn = inventory.resolve(pair["target_table"])
            if not source_urn or not target_urn:
                failures.append(
                    {
                        "candidate_id": row.id,
                        "source_table": pair["source_table"],
                        "target_table": pair["target_table"],
                        "error": "候选两端无法对齐 DataHub URN",
                    }
                )
                continue
            digest = hashlib.sha1(
                f"{row.family_id}:{pair['source_table']}:{pair['source_column']}:{target_urn}:{pair['target_column']}".encode()
            ).hexdigest()[:10]
            constraint = {
                "name": f"ontometa_{row.family_id}_{digest}",
                "source_field": pair["source_column"],
                "target_field": pair["target_column"],
                "target_dataset": target_urn,
            }
            groups.setdefault(source_urn, []).append((row, pair, constraint))

    connector = dh.DataHubConnector(SettingsService().get_datahub_runtime(db))
    try:
        for source_urn, items in groups.items():
            constraints = [item[2] for item in items]
            try:
                await dh.add_foreign_key_constraints(connector, source_urn, constraints)
            except dh.DataHubWriteError as exc:
                for row, pair, _ in items:
                    failures.append(
                        {
                            "candidate_id": row.id,
                            "source_table": pair["source_table"],
                            "target_table": pair["target_table"],
                            "error": str(exc.cause),
                        }
                    )
                continue
            for row, _, _ in items:
                candidate_applied[row.id] += 1
    finally:
        await connector.aclose()

    failed_ids = {item["candidate_id"] for item in failures}
    candidates_applied = 0
    for row in rows:
        if row.id not in failed_ids and candidate_total[row.id] == candidate_applied[row.id]:
            row.state = "applied"
            candidates_applied += 1
    db.commit()
    applied = sum(candidate_applied.values())
    return {
        "attempted": sum(candidate_total.values()),
        "applied": applied,
        "failed": sum(candidate_total.values()) - applied,
        "candidates_applied": candidates_applied,
        "failures": failures,
    }


def decide(
    db: Session, candidate_id: str, *, state: str, operator: str | None = None
) -> RelationCandidateFamily:
    """人工表态。只接受 confirmed / rejected——applied 由落地流程自己写。"""
    if state not in ("confirmed", "rejected"):
        raise ValueError("表态只能是 confirmed 或 rejected")
    row = db.get(RelationCandidateFamily, candidate_id)
    if row is None:
        raise ValueError("候选不存在")
    if row.verdict == key_family_verdict.VERDICT_NOT_A_KEY and state == "confirmed":
        # 判成「不是键」的族没有可确认的东西；要用它得先重新推断改判。
        raise ValueError("该族被判为 not_a_key，没有可确认的关系")
    row.state = state
    row.decided_by = operator
    row.decided_at = _utc_naive()
    db.commit()
    db.refresh(row)
    return row
