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
from app.services.draft_checkpoint import DraftCheckpointStore
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

        llm_runtime = self.settings_service.get_llm_runtime(db)
        chunk_runtime = self.settings_service.get_draft_generation_runtime(db)
        return OntologyDraftGenerator(
            llm_runtime,
            object_chunk_concurrency=chunk_runtime.object_chunk_concurrency,
            relation_chunk_concurrency=chunk_runtime.relation_chunk_concurrency,
        )

    def _draft_generator_instance(self):
        """在无长事务上下文时创建草稿生成器。"""
        from app.database import SessionLocal
        from app.services.draft_generator import OntologyDraftGenerator

        with SessionLocal() as db:
            llm_runtime = self.settings_service.get_llm_runtime(db)
            chunk_runtime = self.settings_service.get_draft_generation_runtime(db)
        return OntologyDraftGenerator(
            llm_runtime,
            object_chunk_concurrency=chunk_runtime.object_chunk_concurrency,
            relation_chunk_concurrency=chunk_runtime.relation_chunk_concurrency,
        )

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

    @staticmethod
    def _ensure_no_conflicting_task(db: Session, domain_id: str, scope: str) -> None:
        """按范围检测冲突任务：``full`` 会整体重建草稿本体，与任何范围的进行中
        任务都冲突；``objects``/``relations`` 只与同范围或 ``full`` 的进行中
        任务冲突，二者之间互不阻塞，可并行执行。"""
        query = db.query(DraftGenerationTask).filter(
            DraftGenerationTask.domain_context_id == domain_id,
            DraftGenerationTask.status.in_(list(ACTIVE_STATUSES)),
        )
        if scope != "full":
            query = query.filter(
                or_(
                    DraftGenerationTask.scope == "full",
                    DraftGenerationTask.scope == scope,
                )
            )
        active = query.first()
        if active:
            raise DraftGenerationAlreadyRunning(active.id)

    @staticmethod
    def _get_draft_ontology(db: Session, domain_id: str) -> Ontology | None:
        return (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .order_by(Ontology.created_at.desc())
            .first()
        )

    def start_draft_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        self._ensure_no_conflicting_task(db, domain_id, "full")

        # 全新生成：清空该域历史分块检查点，避免复用过期结果(重试才续跑)。
        DraftCheckpointStore(domain_id).clear()

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            scope="full",
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
            scope=task.scope,
        )

        return progress

    def start_object_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        """仅生成业务对象：可与 ``start_relation_generation`` 并行执行。"""
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        self._ensure_no_conflicting_task(db, domain_id, "objects")

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            scope="objects",
            status="queued",
            progress=0,
            message="已入队，等待执行名额…",
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
            scope=task.scope,
        )

    def start_relation_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        """仅生成业务关系：需已有草稿本体且已含业务对象，可与
        ``start_object_generation`` 并行执行(关系按 source_dataset_urn 回链
        已入库对象，不依赖同一次运行内的对象命名)。"""
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        ontology = self._get_draft_ontology(db, domain_id)
        has_objects = (
            ontology is not None
            and db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id)
            .first()
            is not None
        )
        if not has_objects:
            raise ValueError("尚无业务对象，请先生成业务对象后再生成业务关系")

        self._ensure_no_conflicting_task(db, domain_id, "relations")

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            scope="relations",
            status="queued",
            progress=0,
            message="已入队，等待执行名额…",
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
            scope=task.scope,
        )

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

        scope = failed.scope or "full"
        self._ensure_no_conflicting_task(db, domain_id, scope)

        if scope == "full":
            dup = self.report_duplicate_drafts(db, domain_id)
            message = "已入队重试，等待执行名额…"
            if dup.draft_count > 1:
                message = (
                    f"已入队重试；检测到 {dup.draft_count} 个草稿本体，"
                    "执行时将自动去重保留最新生成结果"
                )
        else:
            message = "已入队重试，等待执行名额…"

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            scope=scope,
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
            scope=task.scope,
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

            async def _on_chunk_progress(done: int, total: int) -> None:
                # 分块生成阶段映射到 55~78% 进度区间。
                if total <= 0:
                    return
                progress = 55 + int(23 * done / total)
                await self._update_task_progress(
                    task_id, progress, f"正在分块生成本体草稿... ({done}/{total})"
                )

            phase_start = time.perf_counter()
            checkpoint = DraftCheckpointStore(domain_id)
            draft = await self._draft_generator_instance().generate(
                evidence, progress_cb=_on_chunk_progress, checkpoint=checkpoint
            )
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
            # 成功落库后清空检查点(已无续跑需要);清理失败不影响任务成功。
            try:
                checkpoint.clear()
            except Exception:
                logger.warning(
                    "清理草稿检查点失败 domain_id=%s", domain_id, exc_info=True
                )
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

    async def _run_object_generation(self, domain_id: str, task_id: str) -> None:
        """仅生成业务对象+属性：与 ``_run_relation_generation`` 完全独立，可
        并行执行——只 upsert 已有草稿本体的对象/属性，不触碰其关系。"""
        from app.database import SessionLocal

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
                    datahub_domain_id, include_logic_evidences=False
                )
            finally:
                await connector.aclose()
            self._ensure_not_cancelled(task_id)
            logger.info(
                "object_generation phase=datahub task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 30, "正在组装证据包...")
            evidence = self.evidence_builder.build(bundle, include_business_logics=False)
            self._ensure_not_cancelled(task_id)

            await self._update_task_progress(task_id, 45, "正在生成业务对象...")

            async def _on_chunk_progress(done: int, total: int) -> None:
                if total <= 0:
                    return
                progress = 45 + int(40 * done / total)
                await self._update_task_progress(
                    task_id, progress, f"正在分块生成业务对象... ({done}/{total})"
                )

            phase_start = time.perf_counter()
            checkpoint = DraftCheckpointStore(domain_id)
            object_types, properties = await self._draft_generator_instance().generate_object_types(
                evidence, progress_cb=_on_chunk_progress, checkpoint=checkpoint
            )
            self._ensure_not_cancelled(task_id)
            logger.info(
                "object_generation phase=llm task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 90, "正在持久化业务对象...")

            db = SessionLocal()
            try:
                self._ensure_not_cancelled(task_id)
                ontology = self._get_draft_ontology(db, domain_id)
                if ontology is None:
                    ontology = Ontology(
                        domain_context_id=domain_id,
                        status=OntologyStatus.DRAFT.value,
                        generated_by="llm",
                    )
                    db.add(ontology)
                    db.flush()

                self.persistence.upsert_objects(db, ontology, object_types, properties)
                _log_change(
                    db,
                    "ontology",
                    ontology.id,
                    "generate_objects",
                    summary=f"LLM 生成业务对象（{len(object_types)} 个）",
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = f"已生成 {len(object_types)} 个业务对象"
                    db.commit()
            finally:
                db.close()
        except DraftGenerationCancelled:
            logger.info("Object generation cancelled for task %s", task_id)
        except asyncio.CancelledError:
            logger.info("Object generation asyncio task cancelled for %s", task_id)
            raise
        except Exception as exc:
            if self._is_task_cancelled(task_id):
                return
            logger.exception("Object generation failed for task %s: %s", task_id, exc)
            self._mark_task_failed(task_id, str(exc))

    async def _run_relation_generation(self, domain_id: str, task_id: str) -> None:
        """仅生成业务关系：与 ``_run_object_generation`` 完全独立，可并行执行——
        按 source_dataset_urn 回链已入库对象，不依赖本次运行的对象命名，
        只 upsert 已有草稿本体的关系，不触碰其对象/属性。"""
        from app.database import SessionLocal

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

            ontology = self._get_draft_ontology(db, domain_id)
            if ontology is None:
                task.status = "failed"
                task.message = "尚无草稿本体，请先生成业务对象"
                db.commit()
                return
            ontology_id = ontology.id
            object_urn_to_id = {
                obj.source_ref: obj.id
                for obj in db.query(ObjectType)
                .filter(ObjectType.ontology_id == ontology_id)
                .all()
                if obj.source_ref
            }
            if not object_urn_to_id:
                task.status = "failed"
                task.message = "当前草稿本体尚无业务对象，请先生成业务对象"
                db.commit()
                return
        finally:
            db.close()

        try:
            self._ensure_not_cancelled(task_id)
            await self._update_task_progress(task_id, 5, "正在从 DataHub 拉取元数据...")

            phase_start = time.perf_counter()
            connector = self._datahub_connector()
            try:
                bundle = await connector.fetch_domain_bundle(
                    datahub_domain_id, include_logic_evidences=False
                )
            finally:
                await connector.aclose()
            self._ensure_not_cancelled(task_id)
            logger.info(
                "relation_generation phase=datahub task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 30, "正在组装证据包...")
            evidence = self.evidence_builder.build(bundle, include_business_logics=False)
            self._ensure_not_cancelled(task_id)

            object_id_by_candidate = {
                ot.candidate_name: object_urn_to_id[ot.source_dataset_urn]
                for ot in evidence.object_types
                if ot.source_dataset_urn in object_urn_to_id
            }

            await self._update_task_progress(task_id, 45, "正在生成业务关系...")

            async def _on_chunk_progress(done: int, total: int) -> None:
                if total <= 0:
                    return
                progress = 45 + int(40 * done / total)
                await self._update_task_progress(
                    task_id, progress, f"正在分块生成业务关系... ({done}/{total})"
                )

            phase_start = time.perf_counter()
            checkpoint = DraftCheckpointStore(domain_id)
            relation_types = await self._draft_generator_instance().generate_relations(
                evidence, progress_cb=_on_chunk_progress, checkpoint=checkpoint
            )
            self._ensure_not_cancelled(task_id)
            logger.info(
                "relation_generation phase=llm task_id=%s domain_id=%s elapsed_ms=%.1f",
                task_id,
                domain_id,
                (time.perf_counter() - phase_start) * 1000,
            )

            await self._update_task_progress(task_id, 90, "正在持久化业务关系...")

            db = SessionLocal()
            try:
                self._ensure_not_cancelled(task_id)
                ontology = db.get(Ontology, ontology_id)
                if ontology is None:
                    task = db.get(DraftGenerationTask, task_id)
                    if task is not None:
                        task.status = "failed"
                        task.message = "草稿本体已被删除，请重新生成业务对象"
                        db.commit()
                    return

                written = self.persistence.upsert_relations(
                    db, ontology, relation_types, object_id_by_candidate
                )
                _log_change(
                    db,
                    "ontology",
                    ontology.id,
                    "generate_relations",
                    summary=f"LLM 生成业务关系（{written} 条）",
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = f"已生成 {written} 条业务关系"
                    db.commit()
            finally:
                db.close()
        except DraftGenerationCancelled:
            logger.info("Relation generation cancelled for task %s", task_id)
        except asyncio.CancelledError:
            logger.info("Relation generation asyncio task cancelled for %s", task_id)
            raise
        except Exception as exc:
            if self._is_task_cancelled(task_id):
                return
            logger.exception("Relation generation failed for task %s: %s", task_id, exc)
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

    def get_progress(
        self, db: Session, domain_id: str, scope: str | None = None
    ) -> DraftProgressOut | None:
        """返回该域最新任务的进度；传入 ``scope`` 时只看该范围的最新任务，
        便于「生成业务对象」「生成业务关系」两个独立按钮各自轮询自己的任务。"""
        query = db.query(DraftGenerationTask).filter(
            DraftGenerationTask.domain_context_id == domain_id
        )
        if scope is not None:
            query = query.filter(DraftGenerationTask.scope == scope)
        task = query.order_by(DraftGenerationTask.created_at.desc()).first()
        if not task:
            return None
        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
            ontology_id=task.ontology_id,
            scope=task.scope,
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
