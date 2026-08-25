"""任务链编排：把「物化 → 清洗 → 聚合」这种前后相继的任务串起来。

**链只管顺序与上下文传递，不碰治理门槛。** 每一步仍是一条独立的 GovernanceArtifact，
照旧各自走 ``agent_pipeline`` 的「校验 → dry-run → 人工确认 → 执行」。这里做的是两件
此前只能靠人肉完成的事：

1. **记住下一步是什么**——链存着还没起草的步骤，上一步成功后 ``advance`` 才把它落成制品；
2. **把上游定下的选项接到下游**——目标数据源/库/引擎在链上继承，不必每一步重问一遍。

因此「未确认不得执行」逐制品仍然成立：本模块从不调 ``confirm``/``execute``，也不会跳过
任何一步的 dry-run。链能做的最激进的事，是替用户**起草**下一步。

**下游为什么不在建链时就起草**：它的 context 要等上游真跑完才配得齐（落到哪个库、建了哪
张表）。提前起草出来的是预测不是事实，而制品一旦落库就会被人当成已经定下的东西。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.agents import registry
from app.models.agent import (
    ArtifactStatus,
    GovernanceArtifact,
    GovernanceTaskPipeline,
    GovernanceTaskPipelineStep,
    PipelineStatus,
)
from app.services.agent_pipeline import AgentPipelineService, PipelineError

#: 一条链最多几步。链长到十几步就该是 DAG/调度器的活，不是这里的。
MAX_STEPS: int = 8

#: 上游 → 下游继承的 context 键。
#:
#: **白名单而不是全量透传**：上游 spec 里有大量只对它自己有意义的东西（selected_targets、
#: 逐契约的 overrides），整份灌给下游只会让下游的参数表长出一堆看不懂又改不动的行。这几个
#: 键是「这条链共同的落点」——落到哪个仓、哪个库、什么引擎——才该跟着链走。
INHERITED_CONTEXT_KEYS: tuple[str, ...] = (
    "ontology_id",
    "engine",
    "database_prefix",
    "target_datasource_id",
    "target_database",
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


class TaskPipelineService:
    def __init__(self, artifacts: AgentPipelineService | None = None):
        # 起草仍走 agent_pipeline（同一套 Drafter 注册表与状态机）——链不另开一条建制品的路。
        self._artifacts = artifacts or AgentPipelineService()

    # ---------- 建链 ----------

    def create(
        self,
        db: Session,
        *,
        name: str,
        intent: str | None,
        ontology_id: str | None,
        steps: list[dict[str, Any]],
    ) -> GovernanceTaskPipeline:
        """建一条链。**只落意图，不起草任何制品**——第一步也要等用户点了才起草。"""
        if not steps:
            raise PipelineError("任务链至少要有一步")
        if len(steps) > MAX_STEPS:
            raise PipelineError(f"任务链最多 {MAX_STEPS} 步，收到 {len(steps)} 步")

        normalized: list[dict[str, Any]] = []
        for i, raw in enumerate(steps):
            kind = str(raw.get("kind") or "").strip()
            if not kind:
                raise PipelineError(f"第 {i + 1} 步缺少 kind")
            # 未注册的 kind 在这里就拦掉：等到 advance 才发现，用户已经跑完了前几步。
            registry.get_drafter(kind)
            step_intent = str(raw.get("intent") or "").strip()
            if not step_intent:
                raise PipelineError(f"第 {i + 1} 步（{kind}）缺少 intent")
            context = raw.get("context")
            # C2：血缘依赖（depends_on 步序列表）。agent 从血缘/意图推导，
            # 落成 depends_on_json；空则沿用线性默认（依赖上一步）。
            raw_depends = raw.get("depends_on")
            depends: list[int] = []
            if isinstance(raw_depends, list):
                for d in raw_depends:
                    try:
                        depends.append(int(d))
                    except (TypeError, ValueError):
                        raise PipelineError(
                            f"第 {i + 1} 步的 depends_on 必须是步序数字列表"
                        ) from None
                # 防自依赖/越界/重复：交给编译期环检测兜底，这里只拦明显非法。
                if i in depends:
                    raise PipelineError(f"第 {i + 1} 步不能依赖自己")
            normalized.append({
                "kind": kind,
                "intent": step_intent,
                "context": context if isinstance(context, dict) else {},
                "depends_on": depends,
            })

        pipeline = GovernanceTaskPipeline(
            name=(name or "").strip()[:255] or f"任务链 · {normalized[0]['intent'][:40]}",
            intent=intent,
            ontology_id=ontology_id,
        )
        db.add(pipeline)
        db.flush()
        for i, item in enumerate(normalized):
            db.add(
                GovernanceTaskPipelineStep(
                    pipeline_id=pipeline.id,
                    step_index=i,
                    kind=item["kind"],
                    intent=item["intent"],
                    context_json=_dumps(item["context"]),
                    depends_on_json=_dumps(item["depends_on"])
                    if item["depends_on"]
                    else None,
                )
            )
        db.commit()
        db.refresh(pipeline)
        return pipeline

    # ---------- 推进 ----------

    def advance(
        self,
        db: Session,
        pipeline_id: str,
        *,
        context: dict[str, Any] | None = None,
        intent: str | None = None,
        user_created: bool = False,
    ) -> GovernanceArtifact:
        """起草下一步，返回它的制品。**只起草，不确认、不执行。**

        拒绝的两种情形都如实说清楚，因为它们对用户意味着完全不同的下一步动作：
        上游还没跑完 → 去把上游走完；链已经到头 → 没有下一步了。

        C2：线性「等上一步成功」放宽为「等血缘上游成功」——步骤若声明了
        ``depends_on``（血缘推导的显式上游步序），只要求这些上游成功，
        不再要求「前面所有步」都成功；未声明则沿用线性默认（依赖上一步）。

        ``context`` / ``intent`` / ``user_created`` 是**人在六环向导里定下的取值**（见
        ``/agents/pipelines/{id}/advance-confirmed``）：写回该步后再起草，优先级最高——
        链的继承只补人没填的键。人确认了 A 就不能拿继承来的 B 去建任务。
        """
        pipeline = self.require(db, pipeline_id)
        steps = self._steps(db, pipeline_id)
        artifacts = self._artifact_map(db, steps)

        pending = next((s for s in steps if not s.artifact_id), None)
        if pending is None:
            raise PipelineError("任务链已全部起草，没有下一步了")

        # C2：血缘依赖（depends_on 步序列表）优先；未声明才回落线性上一步。
        depends = _loads(pending.depends_on_json, []) or []
        if not depends and pending.step_index > 0:
            depends = [pending.step_index - 1]

        for up_idx in depends:
            upstream = next((s for s in steps if s.step_index == up_idx), None)
            if upstream is None:
                raise PipelineError(
                    f"第 {pending.step_index + 1} 步依赖的上游步序 {up_idx} 不存在"
                )
            artifact = artifacts.get(upstream.artifact_id or "")
            status = artifact.status if artifact else None
            if status != ArtifactStatus.SUCCEEDED.value:
                raise PipelineError(
                    f"第 {up_idx + 1} 步（{upstream.kind}）尚未执行成功"
                    f"（当前 {status or '未起草'}），第 {pending.step_index + 1} 步不能起草"
                )

        # 人确认过的取值落回该步：链态从此与人的确认一致，抽屉里再看也是这份。
        if context:
            pending.context_json = _dumps({**(_loads(pending.context_json, {}) or {}), **context})
        if intent:
            pending.intent = intent

        step_context = {
            **self._inherited(steps, artifacts, before=pending.step_index),
            # 本步显式给的优先：链的继承是补默认值，不是覆盖用户的选择。
            **(_loads(pending.context_json, {}) or {}),
        }
        step_context.setdefault("ontology_id", pipeline.ontology_id)
        step_context = {k: v for k, v in step_context.items() if v is not None}

        artifact = self._artifacts.draft(
            db,
            kind=pending.kind,
            intent=pending.intent,
            context=step_context,
            ontology_id=pipeline.ontology_id,
            # 人刚在六环向导里逐环确认过这一步，溯源就该是"人工创建"——记成机器创建，
            # 审计时会把一条确认过的任务当成 agent 自己冒出来的。
            user_created=user_created,
        )
        pending.artifact_id = artifact.id
        db.commit()
        return artifact

    def draft_all(self, db: Session, pipeline_id: str) -> list[GovernanceArtifact]:
        """C2：一键起草**全部**步骤，返回各步制品（按 step_index 序）。

        与 :meth:`advance` 不同：不再要求上游执行成功才起草下一步——血缘驱动的
        依赖由各步骤的 ``depends_on`` 表达，起草阶段不阻塞（所有制品先落地，
        人再逐个校验/确认/执行；执行顺序由血缘决定）。

        **只起草，不确认、不执行**——「未确认不得执行」不变量不变。

        已起草的步骤跳过（幂等）；全部起草完返回空列表。
        """
        pipeline = self.require(db, pipeline_id)
        steps = self._steps(db, pipeline_id)
        artifacts: list[GovernanceArtifact] = []
        for pending in steps:
            if pending.artifact_id:
                continue  # 已起草，跳过
            # 血缘依赖（depends_on）参与继承：上游已起草的制品 spec 仍是
            # 事实来源；未起草的步骤没有制品，其 context 不参与继承。
            context = {
                **self._inherited(steps, self._artifact_map(db, steps), before=pending.step_index),
                **(_loads(pending.context_json, {}) or {}),
            }
            context.setdefault("ontology_id", pipeline.ontology_id)
            context = {k: v for k, v in context.items() if v is not None}
            artifact = self._artifacts.draft(
                db,
                kind=pending.kind,
                intent=pending.intent,
                context=context,
                ontology_id=pipeline.ontology_id,
            )
            pending.artifact_id = artifact.id
            artifacts.append(artifact)
        db.commit()
        return artifacts

    def _inherited(
        self,
        steps: list[GovernanceTaskPipelineStep],
        artifacts: dict[str, GovernanceArtifact],
        *,
        before: int,
    ) -> dict[str, Any]:
        """上游已定下的落点。靠后的上游覆盖靠前的——离得近的那步说了算。

        取自上游的 **spec**（Drafter 归一后的事实），不是它当初收到的 context：用户在制品
        抽屉里改过参数的话，生效的是 spec 那一份。
        """
        out: dict[str, Any] = {}
        for step in steps:
            if step.step_index >= before:
                break
            artifact = artifacts.get(step.artifact_id or "")
            if artifact is None:
                continue
            spec = _loads(artifact.spec_json, {}) or {}
            for key in INHERITED_CONTEXT_KEYS:
                value = spec.get(key)
                if value not in (None, "", {}, []):
                    out[key] = value
            # 物化的目标库在 spec 里是逐层的 database_overrides；下游要的是一个库名。
            # 各层同库（弹窗与建数表单都是这么填的）时取那个值，分层不一致就不猜。
            databases = {v for v in (spec.get("database_overrides") or {}).values() if v}
            if len(databases) == 1:
                out["target_database"] = next(iter(databases))
        return out

    # ---------- 查询 ----------

    def require(self, db: Session, pipeline_id: str) -> GovernanceTaskPipeline:
        pipeline = db.get(GovernanceTaskPipeline, pipeline_id)
        if pipeline is None:
            raise LookupError("任务链不存在")
        return pipeline

    def list_pipelines(
        self, db: Session, *, ontology_id: str | None = None, limit: int = 50
    ) -> list[GovernanceTaskPipeline]:
        q = db.query(GovernanceTaskPipeline)
        if ontology_id:
            q = q.filter(GovernanceTaskPipeline.ontology_id == ontology_id)
        return q.order_by(desc(GovernanceTaskPipeline.created_at)).limit(limit).all()

    def detail(self, db: Session, pipeline_id: str) -> dict[str, Any]:
        """一条链的完整状态：整体状态 + 逐步的制品状态 + 下一步是哪一步。"""
        pipeline = self.require(db, pipeline_id)
        steps = self._steps(db, pipeline_id)
        artifacts = self._artifact_map(db, steps)

        step_rows: list[dict[str, Any]] = []
        for step in steps:
            artifact = artifacts.get(step.artifact_id or "")
            step_rows.append({
                "id": step.id,
                "step_index": step.step_index,
                "kind": step.kind,
                "intent": step.intent,
                "context": _loads(step.context_json, {}) or {},
                "artifact_id": step.artifact_id,
                # 还没起草的步骤没有制品，状态如实为 null——不要拿 "drafted" 冒充，
                # 那会让人以为已经建了一条制品。
                "artifact_status": artifact.status if artifact else None,
                "artifact_name": artifact.name if artifact else None,
                # P3-2：显式依赖的上游步序列表（DAG 形态）
                "depends_on": _loads(step.depends_on_json, []) or [],
            })

        next_index = self._next_index(step_rows)
        return {
            "id": pipeline.id,
            "name": pipeline.name,
            "intent": pipeline.intent,
            "ontology_id": pipeline.ontology_id,
            "status": self._status(step_rows),
            "steps": step_rows,
            "next_step_index": next_index,
            "next_blocked_reason": self._blocked_reason(step_rows, next_index),
            "schedule_cron": pipeline.schedule_cron,
            "compiled_dag_id": pipeline.compiled_dag_id,
            "compiled_at": pipeline.compiled_at,
            "created_at": pipeline.created_at,
            "updated_at": pipeline.updated_at,
        }

    # ---------- 状态推导 ----------

    @staticmethod
    def _next_index(steps: list[dict[str, Any]]) -> int | None:
        """下一个还没起草的步序；全起草完则 None。"""
        return next((s["step_index"] for s in steps if not s["artifact_id"]), None)

    @staticmethod
    def _blocked_reason(steps: list[dict[str, Any]], next_index: int | None) -> str | None:
        """下一步为什么还不能起草。能起草则 None。"""
        if next_index is None:
            return None
        for step in steps:
            if step["step_index"] >= next_index:
                break
            if step["artifact_status"] != ArtifactStatus.SUCCEEDED.value:
                return (
                    f"第 {step['step_index'] + 1} 步（{step['kind']}）尚未执行成功"
                    f"，当前 {step['artifact_status'] or '未起草'}"
                )
        return None

    @staticmethod
    def _status(steps: list[dict[str, Any]]) -> str:
        """链的整体状态，**由各步制品聚合推导**（不落库，避免两处状态分叉）。"""
        statuses = [s["artifact_status"] for s in steps]
        if any(s == ArtifactStatus.FAILED.value for s in statuses):
            return PipelineStatus.FAILED.value
        if all(s == ArtifactStatus.SUCCEEDED.value for s in statuses):
            return PipelineStatus.SUCCEEDED.value
        if all(s is None for s in statuses):
            return PipelineStatus.DRAFTED.value
        return PipelineStatus.RUNNING.value

    # ---------- 内部 ----------

    @staticmethod
    def _steps(db: Session, pipeline_id: str) -> list[GovernanceTaskPipelineStep]:
        return (
            db.query(GovernanceTaskPipelineStep)
            .filter(GovernanceTaskPipelineStep.pipeline_id == pipeline_id)
            .order_by(GovernanceTaskPipelineStep.step_index)
            .all()
        )

    @staticmethod
    def _artifact_map(
        db: Session, steps: list[GovernanceTaskPipelineStep]
    ) -> dict[str, GovernanceArtifact]:
        ids = [s.artifact_id for s in steps if s.artifact_id]
        if not ids:
            return {}
        rows = (
            db.query(GovernanceArtifact).filter(GovernanceArtifact.id.in_(ids)).all()
        )
        return {r.id: r for r in rows}
