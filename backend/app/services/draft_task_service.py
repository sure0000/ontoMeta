import asyncio
import logging
import time

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ChangeConfirmation,
    DomainContext,
    DraftEvidence,
    DraftGenerationTask,
    EntityChangeLog,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.schemas import (
    ChangeLogOut,
    DraftProgressOut,
    TaskRecordOut,
)
from app.services.common import log_change
from app.services.draft_generation_queue import ACTIVE_STATUSES

logger = logging.getLogger("ontometa.workspace")

# 持有后台草稿生成任务的强引用，避免 asyncio 在任务完成前将其 GC 回收。
_background_tasks: set = set()
_draft_async_tasks: dict[str, "asyncio.Task"] = {}


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    log_change(db, entity_type, entity_id, action, operator, summary)

class DraftGenerationAlreadyRunning(Exception):
    """同域已有进行中的草稿生成任务。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"该数据域已有生成任务进行中 (task_id={task_id})")


class DraftGenerationCancelled(Exception):
    """草稿生成任务已被用户停止。"""


class DraftTaskService:
    """草稿生成任务：排队、进度、取消与级联清理。"""

    def __init__(self) -> None:
        from app.services.evidence_builder import EvidenceBuilder
        from app.services.publish import DraftPersistenceService
        from app.services.settings_service import SettingsService

        self.settings_service = SettingsService()
        self.evidence_builder = EvidenceBuilder()
        self.persistence = DraftPersistenceService()

    def _datahub(self, db: Session):
        from app.connectors.datahub import DataHubConnector

        return DataHubConnector(self.settings_service.get_datahub_runtime(db))

    def _datahub_connector(self):
        """在无长事务上下文时创建 DataHub 连接器。"""
        from app.connectors.datahub import DataHubConnector
        from app.database import SessionLocal

        with SessionLocal() as db:
            runtime = self.settings_service.get_datahub_runtime(db)
        return DataHubConnector(runtime)

    def _draft_generator(self, db: Session):
        from app.services.draft_generator import OntologyDraftGenerator

        return OntologyDraftGenerator(self.settings_service.get_llm_runtime(db))

    def _draft_generator_instance(self):
        """在无长事务上下文时创建草稿生成器。"""
        from app.database import SessionLocal
        from app.services.draft_generator import OntologyDraftGenerator

        with SessionLocal() as db:
            runtime = self.settings_service.get_llm_runtime(db)
        return OntologyDraftGenerator(runtime)

    @staticmethod
    async def _update_task_progress(task_id: str, progress: int, message: str) -> None:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            task = db.get(DraftGenerationTask, task_id)
            if task is None or task.status == "cancelled":
                return
            task.progress = progress
            task.message = message
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _is_task_cancelled(task_id: str) -> bool:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            task = db.get(DraftGenerationTask, task_id)
            return task is not None and task.status == "cancelled"
        finally:
            db.close()

    @staticmethod
    def _ensure_not_cancelled(task_id: str) -> None:
        if DraftTaskService._is_task_cancelled(task_id):
            raise DraftGenerationCancelled()

    @staticmethod
    def _mark_task_failed(task_id: str, message: str) -> None:
        from app.database import SessionLocal

        if DraftTaskService._is_task_cancelled(task_id):
            return
        db = SessionLocal()
        try:
            task = db.get(DraftGenerationTask, task_id)
            if task is None:
                return
            task.status = "failed"
            task.message = message
            # 截断错误摘要，便于列表展示；完整信息仍在 message
            summary = message.strip().split("\n")[0]
            task.error_summary = summary[:500] if summary else "任务失败"
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _track_draft_task(task_id: str, asyncio_task: asyncio.Task) -> None:
        """持有草稿生成 asyncio 任务强引用，便于用户停止时 cancel。"""
        _draft_async_tasks[task_id] = asyncio_task
        _background_tasks.add(asyncio_task)

        def _done(t, *_args):
            _background_tasks.discard(t)
            _draft_async_tasks.pop(task_id, None)

        asyncio_task.add_done_callback(_done)

    @staticmethod
    def _cancel_draft_async_task(task_id: str) -> None:
        asyncio_task = _draft_async_tasks.get(task_id)
        if asyncio_task and not asyncio_task.done():
            asyncio_task.cancel()

    def start_draft_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        active = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.domain_context_id == domain_id,
                DraftGenerationTask.status.in_(list(ACTIVE_STATUSES)),
            )
            .first()
        )
        if active:
            raise DraftGenerationAlreadyRunning(active.id)

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            status="queued",
            progress=0,
            message="已入队，等待执行名额…",
        )
        dup = self.report_duplicate_drafts(db, domain_id)
        if dup.draft_count > 1:
            task.message = (
                f"已入队；检测到 {dup.draft_count} 个草稿本体，"
                "执行时将自动去重"
            )
        db.add(task)
        db.commit()
        db.refresh(task)

        progress = DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
        )

        return progress

    def stop_draft_generation(
        self, db: Session, domain_id: str, task_id: str
    ) -> TaskRecordOut:
        task = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.id == task_id,
                DraftGenerationTask.domain_context_id == domain_id,
            )
            .first()
        )
        if not task:
            raise ValueError("Task not found")
        if task.status not in ACTIVE_STATUSES:
            raise ValueError("仅排队中或进行中的任务可以停止")
        task.status = "cancelled"
        task.message = "用户已停止任务"
        _log_change(
            db,
            "task",
            task_id,
            "stop",
            summary="用户停止草稿生成",
        )
        db.commit()
        db.refresh(task)
        self._cancel_draft_async_task(task_id)
        return TaskRecordOut.model_validate(task)

    def retry_draft_generation(
        self, db: Session, domain_id: str, task_id: str
    ) -> DraftProgressOut:
        """重试失败任务：保留原任务错误摘要，新建排队任务。"""
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        failed = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.id == task_id,
                DraftGenerationTask.domain_context_id == domain_id,
            )
            .first()
        )
        if not failed:
            raise ValueError("Task not found")
        if failed.status != "failed":
            raise ValueError("仅失败任务可以重试")

        active = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.domain_context_id == domain_id,
                DraftGenerationTask.status.in_(list(ACTIVE_STATUSES)),
            )
            .first()
        )
        if active:
            raise DraftGenerationAlreadyRunning(active.id)

        dup = self.report_duplicate_drafts(db, domain_id)
        message = "已入队重试，等待执行名额…"
        if dup.draft_count > 1:
            message = (
                f"已入队重试；检测到 {dup.draft_count} 个草稿本体，"
                "执行时将自动去重保留最新生成结果"
            )

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            status="queued",
            progress=0,
            message=message,
        )
        db.add(task)
        _log_change(
            db,
            "task",
            task_id,
            "retry",
            summary=f"重试失败任务 → 新任务排队（原错误：{failed.error_summary or failed.message}）",
        )
        db.commit()
        db.refresh(task)
        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
        )

    def report_duplicate_drafts(self, db: Session, domain_id: str):
        from app.schemas.domain import DraftDuplicateReport

        drafts = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .order_by(Ontology.created_at.desc())
            .all()
        )
        ids = [o.id for o in drafts]
        count = len(ids)
        if count <= 1:
            msg = "当前无重复草稿" if count == 0 else "当前仅有 1 个草稿本体"
        else:
            msg = (
                f"检测到 {count} 个草稿本体；重新生成时将自动清理旧草稿并保留本次结果"
            )
        return DraftDuplicateReport(
            domain_id=domain_id,
            draft_count=count,
            draft_ontology_ids=ids,
            will_purge_on_regenerate=True,
            message=msg,
        )

    async def _run_draft_generation(self, domain_id: str, task_id: str) -> None:
        from app.database import SessionLocal

        datahub_domain_id: str | None = None

        db = SessionLocal()
        try:
            task = db.get(DraftGenerationTask, task_id)
            if not task:
                logger.exception("DraftGenerationTask %s not found", task_id)
                return
            domain = db.get(DomainContext, domain_id)
            if not domain:
                task.status = "failed"
                task.message = "数据域不存在"
                db.commit()
                return
            datahub_domain_id = domain.datahub_domain_id
        finally:
            db.close()

        try:
            self._ensure_not_cancelled(task_id)
            await self._update_task_progress(task_id, 5, "正在从 DataHub 拉取元数据...")

            phase_start = time.perf_counter()
            connector = self._datahub_connector()
            try:
                bundle = await connector.fetch_domain_bundle(
                    datahub_domain_id,
                    include_logic_evidences=False,
                )
            finally:
                await connector.aclose()
            self._ensure_not_cancelled(task_id)
            logger.info(
                "draft_generation phase=datahub task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 30, "正在组装证据包...")

            phase_start = time.perf_counter()
            evidence = self.evidence_builder.build(bundle, include_business_logics=False)
            self._ensure_not_cancelled(task_id)
            logger.info(
                "draft_generation phase=evidence task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 55, "正在生成本体草稿...")

            phase_start = time.perf_counter()
            draft = await self._draft_generator_instance().generate(evidence)
            self._ensure_not_cancelled(task_id)
            logger.info(
                "draft_generation phase=llm task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 80, "正在持久化草稿...")

            phase_start = time.perf_counter()
            db = SessionLocal()
            try:
                self._ensure_not_cancelled(task_id)
                dup = self.report_duplicate_drafts(db, domain_id)
                purged = self._purge_draft_ontologies(db, domain_id)
                if purged:
                    _log_change(
                        db,
                        "ontology",
                        domain_id,
                        "purge_draft",
                        summary=(
                            f"重新生成草稿前清理 {purged} 个旧草稿本体"
                            f"（去重检测：{dup.message}）"
                        ),
                    )

                ontology = Ontology(
                    domain_context_id=domain_id,
                    status=OntologyStatus.DRAFT.value,
                    generated_by="llm",
                )
                db.add(ontology)
                db.flush()

                self.persistence.save_draft(db, ontology, draft)
                _log_change(
                    db, "ontology", ontology.id, "generate_draft", summary="LLM 草稿生成"
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = "草稿生成完成"
                    db.commit()
            finally:
                db.close()
            logger.info(
                "draft_generation phase=persist task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )
        except DraftGenerationCancelled:
            logger.info("Draft generation cancelled for task %s", task_id)
        except asyncio.CancelledError:
            logger.info("Draft generation asyncio task cancelled for %s", task_id)
            raise
        except Exception as exc:
            if self._is_task_cancelled(task_id):
                return
            logger.exception("Draft generation failed for task %s: %s", task_id, exc)
            self._mark_task_failed(task_id, str(exc))

    def _purge_draft_ontologies(self, db: Session, domain_id: str) -> int:
        """删除同域所有 draft 状态本体及其关联数据，返回删除的本体数。

        重新生成草稿时调用，确保每个数据域同一时刻至多一个 draft 本体，
        避免工作区卡片"草稿 N"数字随历史草稿生成次数累加。
        in_review / published / archived 状态的本体不受影响。
        """
        drafts = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .all()
        )
        if not drafts:
            return 0
        return self._delete_ontologies_cascade(db, [o.id for o in drafts])

    def _delete_ontologies_cascade(self, db: Session, ontology_ids: list[str]) -> int:
        """按依赖顺序级联删除指定本体及其所有关联数据，返回删除的本体数。

        EntityChangeLog 通过 entity_id 字符串（非外键）引用本体，保留作为审计历史。
        """
        if not ontology_ids:
            return 0

        object_type_ids = [
            ot.id
            for ot in db.query(ObjectType)
            .filter(ObjectType.ontology_id.in_(ontology_ids))
            .all()
        ]
        property_ids = (
            [
                p.id
                for p in db.query(Property)
                .filter(Property.object_type_id.in_(object_type_ids))
                .all()
            ]
            if object_type_ids
            else []
        )
        business_logic_ids = [
            bl.id
            for bl in db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id.in_(ontology_ids))
            .all()
        ]

        if property_ids or business_logic_ids:
            db.query(BusinessLogicPropertyBinding).filter(
                or_(
                    BusinessLogicPropertyBinding.property_id.in_(property_ids),
                    BusinessLogicPropertyBinding.business_logic_id.in_(business_logic_ids),
                )
            ).delete(synchronize_session=False)

        if object_type_ids or business_logic_ids:
            db.query(BusinessLogicObjectBinding).filter(
                or_(
                    BusinessLogicObjectBinding.object_type_id.in_(object_type_ids),
                    BusinessLogicObjectBinding.business_logic_id.in_(business_logic_ids),
                )
            ).delete(synchronize_session=False)

        db.query(BusinessLogic).filter(
            BusinessLogic.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        if object_type_ids:
            db.query(Property).filter(
                Property.object_type_id.in_(object_type_ids)
            ).delete(synchronize_session=False)

        db.query(RelationType).filter(
            RelationType.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(ObjectType).filter(
            ObjectType.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(DraftEvidence).filter(
            DraftEvidence.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(ChangeConfirmation).filter(
            ChangeConfirmation.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(DraftGenerationTask).filter(
            DraftGenerationTask.ontology_id.in_(ontology_ids)
        ).update(
            {DraftGenerationTask.ontology_id: None},
            synchronize_session=False,
        )

        db.query(Ontology).filter(Ontology.id.in_(ontology_ids)).delete(
            synchronize_session=False
        )
        db.flush()
        return len(ontology_ids)

    def get_progress(self, db: Session, domain_id: str) -> DraftProgressOut | None:
        task = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.domain_context_id == domain_id)
            .order_by(DraftGenerationTask.created_at.desc())
            .first()
        )
        if not task:
            return None
        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
            ontology_id=task.ontology_id,
        )

    def list_tasks(self, db: Session, domain_id: str) -> list[TaskRecordOut]:
        tasks = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.domain_context_id == domain_id)
            .order_by(DraftGenerationTask.created_at.desc())
            .all()
        )
        result: list[TaskRecordOut] = []
        for task in tasks:
            item = TaskRecordOut.model_validate(task)
            if task.ontology_id:
                item.evidence_count = (
                    db.query(DraftEvidence)
                    .filter(DraftEvidence.ontology_id == task.ontology_id)
                    .count()
                )
            result.append(item)
        return result


    def get_task_logs(self, db: Session, domain_id: str, task_id: str) -> list[ChangeLogOut]:
        task = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.id == task_id,
                DraftGenerationTask.domain_context_id == domain_id,
            )
            .first()
        )
        if not task:
            raise ValueError("Task not found")

        logs: list[ChangeLogOut] = []
        task_records = (
            db.query(EntityChangeLog)
            .filter(EntityChangeLog.entity_id == task_id)
            .order_by(EntityChangeLog.created_at.asc())
            .all()
        )
        logs.extend(ChangeLogOut.model_validate(r) for r in task_records)
        if task.ontology_id:
            records = (
                db.query(EntityChangeLog)
                .filter(EntityChangeLog.entity_id == task.ontology_id)
                .order_by(EntityChangeLog.created_at.asc())
                .all()
            )
            logs.extend(ChangeLogOut.model_validate(r) for r in records)

        if task.message:
            logs.insert(
                0,
                ChangeLogOut(
                    id=f"task-{task.id}",
                    entity_type="task",
                    entity_id=task.id,
                    action=task.status,
                    change_summary=task.message,
                    created_at=task.updated_at,
                ),
            )
        return logs


def recover_stale_draft_tasks() -> int:
    """进程启动时：将遗留的 queued/running 标记为 failed，避免永久僵尸任务。

    策略说明（B5）：本进程内 Semaphore + asyncio 任务在重启后无法恢复，
    故采用 fail-on-restart，不自动 resume。用户可重新触发生成。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        stale = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.status.in_(list(ACTIVE_STATUSES)))
            .all()
        )
        if not stale:
            return 0
        for task in stale:
            task.status = "failed"
            task.message = "服务重启，任务已中断（请重新触发生成）"
        db.commit()
        logger.warning("Recovered %s stale draft generation task(s) → failed", len(stale))
        return len(stale)
    finally:
        db.close()
