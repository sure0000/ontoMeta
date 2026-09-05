"""治理制品流水线：草稿 → 校验 → 确认 → 执行 → 回执。

状态机（单向，非法跃迁一律拒绝）::

    drafted ──validate──> validated ──confirm──> confirmed ──execute──> succeeded
       ^                      │                                    └──> failed
       └── 校验有阻断项时停留 ─┘

关键不变量：
- **未确认不得执行**：``execute`` 只接受 ``confirmed`` 状态。
- **确认前必须有校验报告与 dry-run 差异**，人看得到将要发生什么。
- **幂等**：已 ``succeeded`` 的制品重复执行直接返回原回执，不产生第二次副作用。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.agents import registry
from app.agents.validation import is_blocking, validate_spec
from app.governance import active_standard
from app.models.agent import ArtifactKind, ArtifactStatus, GovernanceArtifact


class PipelineError(ValueError):
    """流水线状态或前置条件错误。"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


# 问题清单的展示优先级。数越小越靠前。
# 由来：ERP 本体一次校验产出 188 条 issue，其中 185 条是 ontology_issue（关系表未落地之类，
# 与本次执行无关的存量噪声），而「Airflow 没解析到 DAG」这条恰恰是执行必失败的原因，却排在
# 第 2/188 条、藏在默认折叠的面板里——等于没提示。按「离本次执行有多近」排序。
_ISSUE_RANK: dict[str, int] = {
    "missing_required_field": 0,
    "credential_in_spec": 0,
    "unknown_object": 0,
    "unknown_property": 0,
    "preflight_blocked": 1,
    "preflight_warning": 2,
    "preflight_unavailable": 2,
    "engine_unknown": 3,
    "engine_unverified": 4,
    "ontology_issue": 9,  # 存量噪声垫底
}


def _rank_issues(issues: list) -> list:
    """稳定排序：阻断项/自检项在前，本体存量噪声垫底。不增删条目，只调顺序。"""
    return sorted(issues, key=lambda i: _ISSUE_RANK.get(i.code, 5))


def _unique_name(db: Session, kind: str, name: str) -> str:
    """同类制品内给**自动派生**的任务名去重：撞名则加「（2）」「（3）」后缀。

    派生名只由 spec 决定（同一本体同一配置必然同名），列表里一眼看去全是同一个名字，
    人分不出哪个是哪个、更点不准要重跑的那个。

    只用于派生名——调用方显式给的名字一律原样保留（见 test_spec_path_bypasses_drafter），
    人自己起的名不该被我们改。
    """
    base = (name or kind).strip()[:80] or kind
    taken = {
        n
        for (n,) in db.query(GovernanceArtifact.name).filter(
            GovernanceArtifact.kind == kind,
            GovernanceArtifact.name.like(f"{base}%"),
        )
    }
    if base not in taken:
        return base
    for seq in range(2, 1000):
        candidate = f"{base}（{seq}）"
        if candidate not in taken:
            return candidate
    return base


def _receipt_failure(receipt: Any) -> str | None:
    """回执自陈的失败原因；None = 未自陈失败。

    多批里任一批投递失败即整体失败。同步任务的「只生成计划、未执行」由 execute 按
    kind 单独判定；其它类型保留既有的产物交付语义。
    """
    if not isinstance(receipt, dict):
        return None
    batches = receipt.get("batches")
    candidates: list[dict] = [receipt]
    if isinstance(batches, list):
        candidates.extend(b for b in batches if isinstance(b, dict))
    for item in candidates:
        if item.get("ok") is False or str(item.get("state") or "") == "failed":
            return str(
                item.get("error")
                or receipt.get("error")
                or "执行失败（回执未给出原因）"
            )
    return None


class AgentPipelineService:
    # ---------- 草稿 ----------

    def draft(
        self,
        db: Session,
        *,
        kind: str,
        intent: str | None = None,
        context: dict[str, Any] | None = None,
        ontology_id: str | None = None,
        spec: dict[str, Any] | None = None,
        name: str | None = None,
        user_created: bool = False,
    ) -> GovernanceArtifact:
        if kind not in {k.value for k in ArtifactKind}:
            raise PipelineError(
                f"未知制品类型 {kind}，可选：{', '.join(registry.all_kinds())}"
            )
        drafter = registry.get_drafter(kind)  # 未注册 → UnregisteredKindError

        if spec is not None:
            # 手动结构化路径：不调 drafter，spec 直接落库。校验闸门（validate）独立于
            # drafter 运行，仍会核对本体真实性/凭据/必填——安全边界不被绕过。
            if not isinstance(spec, dict) or not spec:
                raise ValueError("spec 不能为空对象")
            resolved_spec = spec
            resolved_name = (name or "").strip() or _unique_name(
                db, kind, drafter.name_from_spec(spec)
            )
            origin = "user"
            is_user_created = True
        else:
            # 意图/上下文驱动路径。intent 与 context 至少给一样：表单起草只给结构化
            # context（无自然语言 intent），对话/意图起草只给 intent；两者皆空才拒绝。
            # 各 drafter 用显式 context 选择器（object_type/business_logic_id/…）做确定性
            # 派生，intent 仅作回退匹配与命名，故此处不再强制 intent 非空。
            if not (intent or "").strip() and not (context or {}):
                raise ValueError("未提供 spec 时，intent 与 context 至少给一样")
            resolved_spec = drafter.draft(intent or "", context or {})
            # 调用方显式给的名字优先（手工建任务时人填的任务名）。此前一律用
            # drafter 派生名，导致同一本体建的多个物化任务重名到无法分辨。
            resolved_name = (name or "").strip() or _unique_name(
                db, kind, drafter.suggested_name(intent or "", resolved_spec)
            )
            # 表单起草是用户发起（user_created=True 由调用方显式声明），只是走 drafter
            # 派生结构；对话/机器起草则维持 machine 溯源。
            origin = "user" if user_created else "machine"
            is_user_created = user_created

        artifact = GovernanceArtifact(
            kind=kind,
            name=resolved_name,
            ontology_id=ontology_id,
            intent=intent,
            spec_json=_dumps(resolved_spec),
            machine_baseline=_dumps(resolved_spec),
            status=ArtifactStatus.DRAFTED.value,
            origin=origin,
            user_created=is_user_created,
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 编辑 ----------

    # 只有这几个状态允许改 spec：drafted/validated 尚未确认，failed 是执行失败后
    # 回来改配置重试。confirmed/executing/succeeded 已被人确认或已产生副作用，
    # 改动必须留痕，走「新建」而不是覆盖——这与「未确认不得执行」同一条不变量的另一面。
    EDITABLE_STATUSES = frozenset(
        {
            ArtifactStatus.DRAFTED.value,
            ArtifactStatus.VALIDATED.value,
            ArtifactStatus.FAILED.value,
        }
    )

    def edit(
        self,
        db: Session,
        artifact_id: str,
        *,
        name: str | None = None,
        intent: str | None = None,
        context: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
        ontology_id: str | None = None,
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)
        if artifact.status not in self.EDITABLE_STATUSES:
            raise PipelineError(
                f"{artifact.status} 状态的制品不可编辑"
                "（已确认/执行的制品改动必须留痕，请新建任务）"
            )
        drafter = registry.get_drafter(artifact.kind)

        if spec is not None:
            if not isinstance(spec, dict) or not spec:
                raise ValueError("spec 不能为空对象")
            resolved_spec = spec
        elif context is not None or intent is not None:
            if not (intent or "").strip() and not (context or {}):
                raise ValueError("编辑 spec 时，intent 与 context 至少给一样")
            resolved_spec = drafter.draft(intent or artifact.intent or "", context or {})
        else:
            raise ValueError("编辑必须提供 spec 或 intent/context 之一")

        artifact.spec_json = _dumps(resolved_spec)
        if name is not None:
            artifact.name = name
        if intent is not None:
            artifact.intent = intent
        if ontology_id is not None:
            artifact.ontology_id = ontology_id
        # spec 已变，旧校验报告/确认记录对新 spec 不再有效——打回草稿让用户重新走
        # validate → confirm，不能让一个基于旧 spec 的 confirmed 状态继续存在。
        artifact.status = ArtifactStatus.DRAFTED.value
        artifact.validation_report_json = None
        artifact.confirmed_by = None
        artifact.confirmed_at = None
        # 人工编辑过，标记溯源（与 confirm() 里 machine_edited 呼应）；不改
        # machine_baseline——它是起草时的机器基线，编辑是人工覆盖，改了就失去了
        # 「人工相对基线改了什么」的对比意义。
        artifact.origin = "user"
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 校验（含 dry-run） ----------

    def validate(
        self, db: Session, artifact_id: str, *, context: dict[str, Any] | None = None
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)
        if artifact.status in (
            ArtifactStatus.EXECUTING.value,
            ArtifactStatus.SUCCEEDED.value,
        ):
            raise PipelineError(f"{artifact.status} 状态的制品不可重新校验")

        spec = _loads(artifact.spec_json, {})
        issues = validate_spec(
            db,
            kind=artifact.kind,
            spec=spec,
            ontology_id=artifact.ontology_id,
            # 传自己的 id，判重时才排得掉本条（否则每条都跟自己"重复"）。
            artifact_id=artifact.id,
        )
        blocking = [i for i in issues if is_blocking(i)]

        dry_run: dict[str, Any] | None = None
        dry_run_error: str | None = None
        if not blocking:
            try:
                dry_run = registry.get_executor(artifact.kind).dry_run(
                    spec, context or {}
                )
            except registry.UnregisteredKindError as exc:
                dry_run_error = str(exc)
            except Exception as exc:  # noqa: BLE001 —— dry-run 失败不应炸掉校验
                dry_run_error = f"dry-run 失败：{exc}"

        artifact.validation_report_json = _dumps(
            {
                # 逐条带上 blocking：判据只有 is_blocking 一处，前端不必再维护一份
                # 「哪些码是 warning」的镜像——那份镜像已经漂过一次，把提交前自检的
                # **提醒项**画成红色「阻断」，人照着去查一件根本不拦提交的事。
                "issues": [
                    {**i.to_dict(), "blocking": is_blocking(i)}
                    for i in _rank_issues(issues)
                ],
                "blocking_count": len(blocking),
                "dry_run": dry_run,
                "dry_run_error": dry_run_error,
                # 版本戳：审计「本制品在哪版规约下过闸」，规约升级后可据此判是否需 re-lint。
                "standard_version": active_standard(db).version,
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        # 有阻断项、或高危制品拿不到 dry-run 差异 → 不得进入 validated。
        ready = not blocking and (
            dry_run is not None or not artifact.is_high_risk
        )
        artifact.status = (
            ArtifactStatus.VALIDATED.value if ready else ArtifactStatus.DRAFTED.value
        )
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 确认 ----------

    def confirm(
        self, db: Session, artifact_id: str, *, operator: str | None = None
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)
        if artifact.status != ArtifactStatus.VALIDATED.value:
            raise PipelineError(
                f"只有 validated 状态可确认，当前为 {artifact.status}"
                + ("（请先执行校验）" if artifact.status == "drafted" else "")
            )
        report = _loads(artifact.validation_report_json, {})
        if artifact.is_high_risk and not report.get("dry_run"):
            raise PipelineError("高危制品必须先产出 dry-run 差异才可确认")

        artifact.status = ArtifactStatus.CONFIRMED.value
        artifact.confirmed_by = operator
        artifact.confirmed_at = datetime.now(timezone.utc)
        artifact.origin = "machine_edited"
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 执行 ----------

    def claim_execution(
        self, db: Session, artifact_id: str
    ) -> tuple[GovernanceArtifact, bool]:
        """原子抢占一次执行；返回 ``(artifact, claimed)``。

        MCP 的异步入口会先抢占、再派发进程外 worker。状态条件更新是这里真正的
        幂等边界：两个并发请求即使都读到 confirmed，也只有一个能把它改成
        executing，另一个只会拿到当前态，不会重复投递下游作业。
        """
        artifact = self._require(db, artifact_id)
        if artifact.status in {
            ArtifactStatus.EXECUTING.value,
            ArtifactStatus.SUCCEEDED.value,
        }:
            return artifact, False
        if artifact.status != ArtifactStatus.CONFIRMED.value:
            raise PipelineError(
                f"只有 confirmed 状态可执行，当前为 {artifact.status}"
                "（未经人工确认的制品不得执行）"
            )

        # 在占位前确认执行器存在；否则不能把任务留在无人处理的 executing。
        registry.get_executor(artifact.kind)
        updated = (
            db.query(GovernanceArtifact)
            .filter(
                GovernanceArtifact.id == artifact_id,
                GovernanceArtifact.status == ArtifactStatus.CONFIRMED.value,
            )
            .update(
                {GovernanceArtifact.status: ArtifactStatus.EXECUTING.value},
                synchronize_session=False,
            )
        )
        if updated:
            db.commit()
        else:
            db.rollback()
        db.expire_all()
        current = self._require(db, artifact_id)
        if updated:
            return current, True
        if current.status in {
            ArtifactStatus.EXECUTING.value,
            ArtifactStatus.SUCCEEDED.value,
        }:
            return current, False
        raise PipelineError(f"任务状态已变化，当前为 {current.status}，请重新查询后再执行")

    def release_execution_claim(self, db: Session, artifact_id: str) -> None:
        """派发 worker 失败时释放尚未产生回执的执行占位，允许调用方重试。"""
        (
            db.query(GovernanceArtifact)
            .filter(
                GovernanceArtifact.id == artifact_id,
                GovernanceArtifact.status == ArtifactStatus.EXECUTING.value,
                GovernanceArtifact.execution_receipt_json.is_(None),
            )
            .update(
                {GovernanceArtifact.status: ArtifactStatus.CONFIRMED.value},
                synchronize_session=False,
            )
        )
        db.commit()

    def execute_claimed(
        self, db: Session, artifact_id: str, *, context: dict[str, Any] | None = None
    ) -> GovernanceArtifact:
        """执行一个已由 ``claim_execution`` 抢占的制品并写入回执。"""
        artifact = self._require(db, artifact_id)
        if artifact.status == ArtifactStatus.SUCCEEDED.value:
            return artifact
        if artifact.status != ArtifactStatus.EXECUTING.value:
            raise PipelineError(
                f"只有 executing 状态可由执行 worker 处理，当前为 {artifact.status}"
            )

        spec = _loads(artifact.spec_json, {})
        run_context = {**(context or {}), "artifact_id": artifact.id}
        try:
            executor = registry.get_executor(artifact.kind)
            receipt = executor.execute(spec, run_context)
        except Exception as exc:  # noqa: BLE001
            artifact.status = ArtifactStatus.FAILED.value
            artifact.execution_receipt_json = _dumps({"error": str(exc)})
            artifact.executed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(artifact)
            return artifact

        failure = _receipt_failure(receipt)
        if artifact.kind == "sync" and (
            receipt.get("execute_mode") == "handoff" or receipt.get("handoff")
        ):
            failure = str(
                receipt.get("note") or "同步任务只生成了执行计划，未实际搬运数据"
            )
        if failure:
            artifact.status = ArtifactStatus.FAILED.value
        elif receipt.get("execute_mode") == "orchestrated":
            artifact.status = ArtifactStatus.EXECUTING.value
        else:
            artifact.status = ArtifactStatus.SUCCEEDED.value
        artifact.execution_receipt_json = _dumps(receipt)
        artifact.executed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(artifact)
        return artifact

    def execute(
        self, db: Session, artifact_id: str, *, context: dict[str, Any] | None = None
    ) -> GovernanceArtifact:
        artifact, claimed = self.claim_execution(db, artifact_id)
        # executing/succeeded 都按幂等重放处理；只有本次抢占者能触发执行器。
        if not claimed:
            return artifact
        return self.execute_claimed(db, artifact_id, context=context)

    # ---------- 查询 ----------

    def list_artifacts(
        self,
        db: Session,
        *,
        kind: str | None = None,
        status: str | None = None,
        ontology_id: str | None = None,
        limit: int | None = None,
        reconcile: bool = True,
    ) -> list[GovernanceArtifact]:
        """列出治理制品。

        ``reconcile=True``（默认，保持既有调用方的语义）会**逐条**回读 Airflow DagRun
        并在终态时回写——那是「状态不读不推进」的必要代价，但也是一次远程往返/条。
        只想要名字和 id 的场景（列目录、给模型选任务）传 ``reconcile=False``，
        真要终态时再对单条用 ``get``。

        ``limit`` 在**查询里**截断。此前只在调用方切片，对账仍然跑满全表——
        `limit=20` 也可能对账上百条，MCP 侧实测均耗时 11.8 秒。
        """
        q = db.query(GovernanceArtifact)
        if kind:
            q = q.filter(GovernanceArtifact.kind == kind)
        if status:
            q = q.filter(GovernanceArtifact.status == status)
        if ontology_id:
            q = q.filter(GovernanceArtifact.ontology_id == ontology_id)
        q = q.order_by(desc(GovernanceArtifact.created_at))
        if limit is not None:
            q = q.limit(int(limit))
        rows = q.all()
        # P0b：读时对账 orchestrated 制品的 Airflow DagRun 状态，终态时回写 artifact.status
        if reconcile:
            for a in rows:
                self._reconcile_orchestrated_status(db, a)
        return rows

    def get(self, db: Session, artifact_id: str) -> GovernanceArtifact | None:
        artifact = db.get(GovernanceArtifact, artifact_id)
        if artifact:
            self._reconcile_orchestrated_status(db, artifact)
        return artifact

    def _reconcile_orchestrated_status(self, db: Session, artifact: GovernanceArtifact) -> None:
        """P0b：读时对账 orchestrated 制品的 Airflow DagRun 状态，终态时回写 artifact.status。

        制品 status 在 execute() 提交 DAG 后即置 succeeded，但 DAG 可能还在跑或已失败——
        实时权威在 Airflow。此函数惰性对账：读到 SUCCEEDED 制品时查 Airflow，若 DagRun 报终态
        （success/failed）且与制品 status 不一致，回写制品并 commit。

        只对 execute_mode=orchestrated 且处于 EXECUTING/SUCCEEDED 的制品做。
        sync 在 Airflow success 后还要验证 Doris 目标表，验证失败会回写 FAILED。
        """
        if artifact.status not in {
            ArtifactStatus.EXECUTING.value,
            ArtifactStatus.SUCCEEDED.value,
        }:
            return
        receipt = _loads(artifact.execution_receipt_json, {})
        if receipt.get("execute_mode") != "orchestrated":
            return

        # 回执自陈失败 → 直接改判，不必问 Airflow。这类「投递就没成功」的制品压根没有
        # DagRun 可查，下面的对账救不回来；execute() 修好之前落库的存量制品也靠这里治好。
        if _receipt_failure(receipt):
            artifact.status = ArtifactStatus.FAILED.value
            db.commit()
            return

        # 物化回执按批次记录；其它任务使用单 DAG 顶层标识。
        if artifact.kind == "materialize":
            batches = receipt.get("batches") or []
        else:
            batches = [{
                "dag_id": receipt.get("dag_id"),
                "dag_run_id": receipt.get("dag_run_id"),
                "state": receipt.get("state"),
                "error": receipt.get("error"),
            }]
        if not any(b.get("dag_id") and b.get("dag_run_id") for b in batches):
            return  # 没有真实 DagRun → 提交就失败了，制品已是 FAILED（或不应为 SUCCEEDED）

        try:
            from app.connectors.airflow import AirflowClient, is_terminal
            from app.services.settings_service import SettingsService

            rt = SettingsService().get_airflow_runtime(db)
            if not rt.available:
                return

            client = AirflowClient(
                rt.endpoint, username=rt.username, password=rt.password
            )
            try:
                states: list = []
                for b in batches:
                    bid, brun = b.get("dag_id"), b.get("dag_run_id")
                    if not bid or not brun:
                        # 触发失败的批，用回执里的状态
                        states.append(b.get("state") or "failed")
                        continue
                    try:
                        run = client.get_dag_run(bid, brun)
                        states.append(run.get("state"))
                    except Exception as exc:  # noqa: BLE001
                        # 404 ＝ Airflow 上确实没有这次运行（被清理/被删）。它永远不会
                        # 再出现，当作失败改判——否则制品会永久停在 executing：对账读不到
                        # 状态就什么都不改，而 executing 既不能重新校验也不能再确认，
                        # 界面上那条任务就此定死，只能改库。
                        # 其余异常（网络/鉴权）是「这次没问到」，保持未知、下次再对。
                        states.append("failed" if "404" in str(exc) else None)
            finally:
                client.close()

            # 聚合多批状态：任一 failed → failed；全 success → success；否则 running/queued
            from app.api.warehouse import _aggregate_state
            agg = _aggregate_state(states)
            if artifact.kind == "materialize" and agg == "success":
                params = receipt.get("deployment_reconciliation") or {}
                if params:
                    from app.services.doris_deployment import publish_schema_ready

                    publish_schema_ready(db, **params)
            if artifact.kind == "transform" and agg:
                from app.services.transform_reconciliation import reconcile_transform_receipt

                reconcile_transform_receipt(db, receipt=receipt, airflow_state=agg)
            if artifact.kind == "metric" and agg:
                from app.services.metric_reconciliation import reconcile_metric_receipt

                reconcile_metric_receipt(db, receipt=receipt, airflow_state=agg)
            sync_verification = None
            if artifact.kind == "sync" and agg:
                from app.services.sync_reconciliation import reconcile_sync_receipt

                sync_verification = reconcile_sync_receipt(
                    db, receipt=receipt, airflow_state=agg
                )
                if sync_verification is not None:
                    receipt["doris_verification"] = sync_verification
                    artifact.execution_receipt_json = _dumps(receipt)
            if not agg or not is_terminal(agg):
                artifact.status = ArtifactStatus.EXECUTING.value
                db.commit()
                return

            verified = not sync_verification or sync_verification.get("verified") is True
            artifact.status = (
                ArtifactStatus.SUCCEEDED.value
                if agg == "success" and verified
                else ArtifactStatus.FAILED.value
            )
            db.commit()
        except Exception:  # noqa: BLE001
            # best-effort：任何异常都静默吞掉，不炸查询路径
            pass

    def _require(self, db: Session, artifact_id: str) -> GovernanceArtifact:
        artifact = db.get(GovernanceArtifact, artifact_id)
        if artifact is None:
            raise LookupError("制品不存在")
        return artifact
