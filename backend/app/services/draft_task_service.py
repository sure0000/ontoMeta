import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError
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
    EvidenceBundle,
    TaskRecordOut,
)
from app.config import settings
from app.services.common import log_change
from app.services.draft_checkpoint import DraftCheckpointStore
from app.services.draft_generation_queue import ACTIVE_STATUSES
from app.services import ontology_workspace

logger = logging.getLogger("ontometa.workspace")

# 自动续跑任务的 message 哨兵：用于按域+范围计数已发生的自动续跑次数（无需加表列）。
_AUTO_RESUME_SENTINEL = "⟳自动续跑"

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
        from app.services.ontology_merge import OntologyMergeService
        from app.services.publish import DraftPersistenceService
        from app.services.settings_service import SettingsService

        self.settings_service = SettingsService()
        self.evidence_builder = EvidenceBuilder()
        self.persistence = DraftPersistenceService()
        self.merge = OntologyMergeService()

    @staticmethod
    def _store_merge_report(db: Session, task_id: str, report) -> str:
        """把合并变更报告落到任务上，返回一句话摘要。"""
        import json as _json

        data = report.to_dict()
        s = data["summary"]
        summary = (
            f"新增{s['added']}・更新{s['updated']}・保留{s['kept']}・"
            f"冲突{s['conflict']}・上游删除{s['removed']}"
        )
        task = db.get(DraftGenerationTask, task_id)
        if task is not None:
            task.merge_report_json = _json.dumps(data, ensure_ascii=False)
        return summary

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

    def _ensure_llm_ready(self, db: Session) -> None:
        """起草前先确认 LLM 可用——业务命名全靠它，没配就当场提示，别让用户等一轮
        任务失败才知道。

        只查「配没配」（Key + 模型），不做拨测：拨测在设置页有专门入口，起草入口
        再拨一次会把每次点击都拖成秒级等待。
        """
        from app.services.draft_generator import LlmNotConfiguredError

        runtime = self.settings_service.get_llm_runtime(db)
        if not (runtime.api_key or "").strip() or not (runtime.model or "").strip():
            raise LlmNotConfiguredError("业务命名")

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
    def _describe_exc(exc: BaseException) -> str:
        """把异常转成对用户可读、非空的失败原因。

        坑：``httpx.ReadTimeout`` 等超时异常的 ``str()`` 为空，直接落 ``str(exc)`` 会得到
        空 message → UI 显示"失败且无原因"。这里：网络类异常给中文可操作提示；其余在类名
        基础上补 ``str``（为空则仅类名），保证任何失败都能看到原因。完整堆栈另在
        ``.logs/draft-worker-<task_id>.log``。
        """
        from app.services.draft_generator import (
            LlmNotConfiguredError,
            LlmResponseFormatError,
            ObjectNamingIncompleteError,
        )

        # 命名类失败自带完整中文说明，原样透出，别再套一层类名。
        if isinstance(
            exc,
            (LlmNotConfiguredError, ObjectNamingIncompleteError, LlmResponseFormatError),
        ):
            return str(exc)
        try:
            import httpx

            if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
                return f"连接 DataHub 失败（{type(exc).__name__}）：请检查 DataHub 地址/网络/隧道"
            if isinstance(exc, httpx.TimeoutException):
                return (
                    f"DataHub 请求超时（{type(exc).__name__}）："
                    "响应过慢或隧道不稳定，请重试或检查 DataHub 连接"
                )
        except Exception:
            pass
        detail = str(exc).strip()
        name = type(exc).__name__
        return f"{name}: {detail}" if detail else name

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
            summary = (message or "").strip().split("\n")[0]
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
    def _reset_evidence_for_fresh_run(domain_id: str) -> None:
        """全新生成：清空该域的分块检查点与证据缓存，确保重新抓取最新上游元数据。

        三个范围（full/objects/relations）语义一致——此前只有 full 清、另外两个静默
        复用 TTL 内的磁盘缓存，同一个「生成」心智下藏着两种新鲜度，界面上还看不出来，
        用户无法回答「我这次拿的到底是不是最新元数据」。

        只有重试/自动续跑不清：那两条路正是要复用检查点与证据接着跑，跳过分钟级抓取。
        """
        from app.services import draft_evidence_cache

        DraftCheckpointStore(domain_id).clear()
        draft_evidence_cache.clear(domain_id)

    @staticmethod
    def _working_ontology(db: Session, domain_id: str) -> Ontology | None:
        """该域的工作本体——**不看 status**（一域一本体，见 ontology_workspace）。

        旧实现只认 ``status == draft``，发布后必然落空并新建空白行，人工修订基线随之
        失忆。这里按域取行，发布后再生成会继续合并进同一行。
        """
        return ontology_workspace.get_working_ontology(db, domain_id)

    def start_draft_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        self._ensure_llm_ready(db)
        self._ensure_no_conflicting_task(db, domain_id, "full")

        self._reset_evidence_for_fresh_run(domain_id)

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            scope="full",
            status="queued",
            progress=0,
            message="已入队，等待执行名额…",
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

        self._ensure_llm_ready(db)
        self._ensure_no_conflicting_task(db, domain_id, "objects")
        self._reset_evidence_for_fresh_run(domain_id)

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

        ontology = self._working_ontology(db, domain_id)
        has_objects = (
            ontology is not None
            and db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id)
            .first()
            is not None
        )
        if not has_objects:
            raise ValueError("尚无业务对象，请先生成业务对象后再生成业务关系")

        self._ensure_llm_ready(db)
        self._ensure_no_conflicting_task(db, domain_id, "relations")
        self._reset_evidence_for_fresh_run(domain_id)

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
        self._ensure_llm_ready(db)
        self._ensure_no_conflicting_task(db, domain_id, scope)

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

    @staticmethod
    def _is_transient_failure(exc: BaseException) -> bool:
        """判断失败是否为「连接抖动」类——值得自动续跑（复用检查点+证据缓存补缺）。

        覆盖：分块命名增强的 :class:`DraftEnrichmentError`（子块重试后仍失败）、OpenAI
        SDK 的连接/超时/5xx，以及 DataHub 侧的 httpx 传输错（``TransportError`` 是
        ConnectError/ReadError/超时/RemoteProtocolError 的共同基类）。解析错/4xx/逻辑
        错不在此列——那类重跑也不会好，直接失败等人工介入。
        """
        from app.services.draft_generator import DraftEnrichmentError

        return isinstance(
            exc,
            (
                DraftEnrichmentError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                httpx.TransportError,
            ),
        )

    def _maybe_auto_resume(
        self, domain_id: str, task_id: str, exc: BaseException
    ) -> bool:
        """连接抖动类失败时有界自动续跑：新建排队任务并拉起分离子进程，复用 checkpoint
        与 evidence 缓存只补缺失块。返回是否已调度续跑。

        次数按 message 哨兵计（本域+范围），达到 ``draft_auto_resume_max`` 即停，退回
        人工重试。任何调度异常都吞掉——自动续跑是尽力而为，绝不因它二次抛错。
        """
        max_resumes = settings.draft_auto_resume_max
        if max_resumes <= 0 or not self._is_transient_failure(exc):
            return False

        from app.database import SessionLocal
        from app.jobs.draft_worker import spawn_draft_worker

        try:
            with SessionLocal() as db:
                failed = db.get(DraftGenerationTask, task_id)
                if failed is None:
                    return False
                scope = failed.scope or "full"
                prior = (
                    db.query(DraftGenerationTask)
                    .filter(
                        DraftGenerationTask.domain_context_id == domain_id,
                        DraftGenerationTask.scope == scope,
                        DraftGenerationTask.message.like(f"{_AUTO_RESUME_SENTINEL}%"),
                    )
                    .count()
                )
                if prior >= max_resumes:
                    logger.warning(
                        "draft auto-resume exhausted domain=%s scope=%s (%d/%d)，退回人工重试",
                        domain_id,
                        scope,
                        prior,
                        max_resumes,
                    )
                    return False
                attempt = prior + 1
                resume = DraftGenerationTask(
                    domain_context_id=domain_id,
                    scope=scope,
                    status="queued",
                    progress=0,
                    message=(
                        f"{_AUTO_RESUME_SENTINEL} 第 {attempt}/{max_resumes} 次"
                        "（连接抖动，复用检查点+证据缓存续跑）…"
                    ),
                )
                db.add(resume)
                _log_change(
                    db,
                    "task",
                    task_id,
                    "auto-resume",
                    summary=f"连接抖动自动续跑 第{attempt}/{max_resumes}次 → 新任务 {resume.id}",
                )
                db.commit()
                resume_id = resume.id

            spawn_draft_worker(resume_id)
            logger.info(
                "draft auto-resume scheduled domain=%s scope=%s attempt=%d new_task=%s",
                domain_id,
                scope,
                attempt,
                resume_id,
            )
            return True
        except Exception:
            logger.warning(
                "draft auto-resume failed to schedule domain=%s", domain_id, exc_info=True
            )
            return False

    async def _load_or_fetch_evidence(
        self,
        domain_id: str,
        datahub_domain_id: str | None,
        task_id: str,
        *,
        log_prefix: str,
    ) -> EvidenceBundle:
        """取证据包：命中磁盘缓存则跳过 DataHub 的分钟级抓取，否则抓取+组装并回填缓存。

        缓存跨进程有效，是「失败后（自动/人工）续跑不必重跑 ~7 分钟 DataHub 抓取」的
        关键——三条生成流水线（full/objects/relations）抓取与组装参数一致，共享一份。
        """
        from app.services import draft_evidence_cache

        fingerprint = datahub_domain_id or "none"
        cached = draft_evidence_cache.load(domain_id, fingerprint)
        if cached is not None:
            await self._update_task_progress(
                task_id, 50, "命中证据缓存，跳过 DataHub 抓取..."
            )
            self._ensure_not_cancelled(task_id)
            return cached

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
            "%s phase=datahub task_id=%s domain_id=%s elapsed_ms=%.1f",
            log_prefix,
            task_id,
            domain_id,
            (time.perf_counter() - phase_start) * 1000,
        )

        await self._update_task_progress(task_id, 30, "正在组装证据包...")
        phase_start = time.perf_counter()
        evidence = self.evidence_builder.build(bundle, include_business_logics=False)
        self._ensure_not_cancelled(task_id)
        logger.info(
            "%s phase=evidence task_id=%s domain_id=%s elapsed_ms=%.1f",
            log_prefix,
            task_id,
            domain_id,
            (time.perf_counter() - phase_start) * 1000,
        )
        draft_evidence_cache.save(domain_id, fingerprint, evidence)
        return evidence

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

            evidence = await self._load_or_fetch_evidence(
                domain_id, datahub_domain_id, task_id, log_prefix="draft_generation"
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
                # 一域一本体：合并目标不看 status，发布过的行也继续合并进去
                # （已发布实体的结构性字段已在 publish 时钉住，机器改不动只提冲突）。
                ontology = ontology_workspace.get_or_create_working_ontology(
                    db, domain_id
                )
                self._purge_stale_draft_rows(db, ontology)

                report = self.merge.merge_full(db, ontology.id, draft, task_id)
                ontology.generated_at = datetime.now(timezone.utc)
                ontology.draft_revision = (ontology.draft_revision or 0) + 1
                summary = self._store_merge_report(db, task_id, report)
                _log_change(
                    db,
                    "ontology",
                    ontology.id,
                    "generate_draft",
                    summary=f"LLM 草稿生成合并（{summary}）",
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = f"草稿生成完成：{summary}"
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
            self._mark_task_failed(task_id, self._describe_exc(exc))
            self._maybe_auto_resume(domain_id, task_id, exc)

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

            evidence = await self._load_or_fetch_evidence(
                domain_id, datahub_domain_id, task_id, log_prefix="object_generation"
            )

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
                ontology = ontology_workspace.get_or_create_working_ontology(
                    db, domain_id
                )

                from app.services.ontology_merge import MergeReport

                report = MergeReport()
                self.merge.merge_objects(
                    db, ontology.id, object_types, properties, task_id, report,
                    handle_removal=False,
                )
                ontology.generated_at = datetime.now(timezone.utc)
                ontology.draft_revision = (ontology.draft_revision or 0) + 1
                summary = self._store_merge_report(db, task_id, report)
                _log_change(
                    db,
                    "ontology",
                    ontology.id,
                    "generate_objects",
                    summary=f"LLM 生成业务对象（{summary}）",
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = f"业务对象已生成：{summary}"
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
            self._mark_task_failed(task_id, self._describe_exc(exc))
            self._maybe_auto_resume(domain_id, task_id, exc)

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

            ontology = self._working_ontology(db, domain_id)
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

            evidence = await self._load_or_fetch_evidence(
                domain_id, datahub_domain_id, task_id, log_prefix="relation_generation"
            )

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

                from app.services.ontology_merge import MergeReport

                report = MergeReport()
                written = self.merge.merge_relations(
                    db,
                    ontology.id,
                    relation_types,
                    lambda candidate: object_id_by_candidate.get(candidate),
                    task_id,
                    report,
                    handle_removal=False,
                )
                ontology.generated_at = datetime.now(timezone.utc)
                ontology.draft_revision = (ontology.draft_revision or 0) + 1
                summary = self._store_merge_report(db, task_id, report)
                _log_change(
                    db,
                    "ontology",
                    ontology.id,
                    "generate_relations",
                    summary=f"LLM 生成业务关系（{summary}）",
                )

                task = db.get(DraftGenerationTask, task_id)
                if task is not None:
                    task.ontology_id = ontology.id
                    task.status = "succeeded"
                    task.progress = 100
                    task.message = f"业务关系已生成：{summary}（写入 {written} 条）"
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
            self._mark_task_failed(task_id, self._describe_exc(exc))
            self._maybe_auto_resume(domain_id, task_id, exc)

    @staticmethod
    def _has_manual_work(db: Session, ontology_id: str) -> bool:
        """该本体行里是否有人工痕迹（人工新建 / 钉住过字段 / 被人工修正）。"""
        for model in (ObjectType, RelationType):
            hit = (
                db.query(model.id)
                .filter(
                    model.ontology_id == ontology_id,
                    or_(
                        model.user_created.is_(True),
                        model.overridden_fields.isnot(None),
                        model.origin.in_(("manual", "machine_edited")),
                    ),
                )
                .first()
            )
            if hit is not None:
                return True
        return False

    def _purge_stale_draft_rows(self, db: Session, working: Ontology) -> int:
        """合并前收敛到一域一本体：清掉域内多余的**草稿**行。

        两条红线：已发布行绝不删（它可能仍在对外服务）；带人工痕迹的草稿行也不删——
        那是用户在分叉里做过的事，宁可留着让 P1 的唯一约束逼人显式处理，也不静默销毁。
        """
        stale = [
            o
            for o in ontology_workspace.list_stale_ontologies(
                db, working.domain_context_id
            )
            if o.status != OntologyStatus.PUBLISHED.value
        ]
        keep = [o for o in stale if self._has_manual_work(db, o.id)]
        drop = [o for o in stale if o not in keep]
        for o in keep:
            logger.warning(
                "保留带人工痕迹的遗留草稿本体 %s（域 %s），未自动清理",
                o.id,
                working.domain_context_id,
            )
        if not drop:
            return 0
        self._delete_ontologies_cascade(db, [o.id for o in drop])
        _log_change(
            db,
            "ontology",
            working.id,
            "purge_draft",
            summary=f"合并前清理 {len(drop)} 个遗留草稿本体行",
        )
        return len(drop)

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
    """进程启动时：将「陈旧」的 queued/running 任务标记为 failed，避免永久僵尸任务。

    策略说明（B5）：本进程内 Semaphore + asyncio 任务在进程重启后无法恢复，
    故采用 fail-on-restart，不自动 resume。用户可重新触发生成。

    仅回收 updated_at 早于 now-grace 的任务（grace=draft_task_stale_grace_seconds）：
    开发热重载或异常退出会杀掉承载任务的进程，而残留/孤儿的旧进程可能与新进程短暂
    并存（日志中出现过 "Address already in use"）。若无差别地回收所有活跃任务，新进程
    的启动钩子会误杀仍在另一存活进程里推进的任务。真死的任务其 updated_at 已冻结、必然
    超过窗口而被回收；活着的任务每写一次进度都会刷新 updated_at，从而被保护。now 取自
    数据库（func.now()）而非本地时钟，避免 Python 与 DB 时区口径不一致。
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        grace = max(0, int(settings.draft_task_stale_grace_seconds))
        db_now = db.execute(select(func.now())).scalar()
        cutoff = db_now - timedelta(seconds=grace) if db_now is not None else None

        active = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.status.in_(list(ACTIVE_STATUSES)))
            .all()
        )
        if not active:
            return 0

        reaped = 0
        skipped = 0
        for task in active:
            if cutoff is not None and task.updated_at is not None and task.updated_at > cutoff:
                # 最近仍在推进 → 可能属于另一存活进程，暂不回收（下次启动越过宽限再回收）。
                skipped += 1
                continue
            task.status = "failed"
            task.message = "服务进程重启（开发热重载或异常退出），任务已中断，请重新触发生成。"
            reaped += 1
        db.commit()
        if reaped or skipped:
            logger.warning(
                "Recovered %s stale draft generation task(s) → failed; "
                "skipped %s recently-active task(s) within %ss grace",
                reaped,
                skipped,
                grace,
            )
        return reaped
    finally:
        db.close()
