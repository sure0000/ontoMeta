"""任务链（多任务编排）：链只管顺序与上下文传递，不碰治理门槛。

盯住四条不变量：
1. **建链不起草任何制品**——第一步也要等用户点了才起草；
2. **上游没执行成功，下游不给起草**，且拒绝时说清卡在哪一步；
3. **上游定下的落点沿链继承**（目标数据源/库/引擎），下游不必重报；显式给的优先；
4. **「未确认不得执行」逐制品仍然成立**——链没有、也不该有「一键跑完整条链」的路径。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import DataSource, GovernanceArtifact
from app.models.agent import ArtifactStatus
from app.services.agent_pipeline import PipelineError
from app.services.task_pipeline import TaskPipelineService
from tests.test_chat_bi_golden import _seed_golden_domain


def _logic_id(db, onto_id: str) -> str:
    """golden 种子里已发布的业务逻辑 id（order_total）。"""
    from app.models import BusinessLogic

    logic = (
        db.query(BusinessLogic)
        .filter(BusinessLogic.ontology_id == onto_id, BusinessLogic.status == "published")
        .first()
    )
    assert logic is not None, "golden 种子应有已发布业务逻辑"
    return logic.id


def _chain(db, onto_id: str, *, datasource_id: str = "ds-chain"):
    """物化 → 清洗 → 聚合 的三步链。"""
    return TaskPipelineService().create(
        db,
        name="客户主数据入仓链",
        intent="物化到数仓后清洗，再按口径聚合",
        ontology_id=onto_id,
        steps=[
            {"kind": "materialize", "intent": "物化到数仓",
             "context": {"target_datasource_id": datasource_id, "target_database": "dw"}},
            {"kind": "transform", "intent": "去重清洗", "context": {"target_table": "customer"}},
            {"kind": "metric", "intent": "按口径聚合"},
        ],
    )


@pytest.fixture
def domain():
    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id="ds-chain", name="生产 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, status="ok", dsn_secret_ref="mysql://doris",
        ))
        db.commit()
        try:
            yield db, onto_id
        finally:
            # 带 dsn 的源会被 run_sql 的数据源解析选走，串到别的用例
            db.query(DataSource).filter(DataSource.id == "ds-chain").delete()
            db.commit()


def test_create_drafts_nothing(domain):
    """建链只落意图。第一步也不预先起草——制品一旦落库就会被人当成已经定下的东西。"""
    db, onto_id = domain
    pipeline = _chain(db, onto_id)
    detail = TaskPipelineService().detail(db, pipeline.id)

    assert [s["kind"] for s in detail["steps"]] == ["materialize", "transform", "metric"]
    assert all(s["artifact_id"] is None for s in detail["steps"])
    assert all(s["artifact_status"] is None for s in detail["steps"])
    assert detail["status"] == "drafted"
    assert detail["next_step_index"] == 0
    assert detail["next_blocked_reason"] is None  # 第一步没有上游，可以直接起草


def test_advance_drafts_only_the_next_step(domain):
    """推进一步 = 起草一条制品，状态停在 drafted：校验/确认/执行仍走各自的门。"""
    db, onto_id = domain
    pipeline = _chain(db, onto_id)
    artifact = TaskPipelineService().advance(db, pipeline.id)

    assert artifact.kind == "materialize"
    assert artifact.status == ArtifactStatus.DRAFTED.value
    detail = TaskPipelineService().detail(db, pipeline.id)
    assert detail["steps"][0]["artifact_id"] == artifact.id
    assert detail["steps"][1]["artifact_id"] is None
    assert detail["status"] == "running"


def test_downstream_blocked_until_upstream_succeeds(domain):
    """上游没执行成功就不给起草下游，且说清卡在哪一步、当前什么状态。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _chain(db, onto_id)
    svc.advance(db, pipeline.id)  # 第 1 步：物化，起草完停在 drafted

    with pytest.raises(PipelineError) as exc:
        svc.advance(db, pipeline.id)
    assert "第 1 步" in str(exc.value) and "materialize" in str(exc.value)

    detail = svc.detail(db, pipeline.id)
    assert detail["next_step_index"] == 1
    assert "第 1 步" in (detail["next_blocked_reason"] or "")


def test_downstream_inherits_upstream_target(domain):
    """上游定下的落点沿链继承：清洗那一步不必再报一遍目标数据源与库。

    这正是任务链要消灭的事——否则用户在对话里得把同一个 id 报三遍。
    """
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _chain(db, onto_id)
    first = svc.advance(db, pipeline.id)
    # 直接把上游置为成功（本用例验的是继承，不是执行链路）
    db.get(GovernanceArtifact, first.id).status = ArtifactStatus.SUCCEEDED.value
    db.commit()

    second = svc.advance(db, pipeline.id)
    assert second.kind == "transform"
    # 物化的 spec 里目标库是逐层的 database_overrides，各层同库时收敛成一个库名传给下游
    import json

    spec = json.loads(db.get(GovernanceArtifact, first.id).spec_json)
    assert set(spec["database_overrides"].values()) == {"dw"}
    inherited = svc._inherited(
        svc._steps(db, pipeline.id), svc._artifact_map(db, svc._steps(db, pipeline.id)), before=1
    )
    assert inherited["target_datasource_id"] == "ds-chain"
    assert inherited["target_database"] == "dw"


def test_explicit_step_context_wins_over_inheritance(domain):
    """链的继承是补默认值，不是覆盖用户在这一步的明确选择。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = svc.create(
        db, name="分库链", intent=None, ontology_id=onto_id,
        steps=[
            {"kind": "materialize", "intent": "物化",
             "context": {"target_datasource_id": "ds-chain", "target_database": "dw",
                         "engine": "hive"}},
            {"kind": "transform", "intent": "清洗",
             "context": {"target_table": "customer", "engine": "doris"}},
        ],
    )
    first = svc.advance(db, pipeline.id)
    db.get(GovernanceArtifact, first.id).status = ArtifactStatus.SUCCEEDED.value
    db.commit()

    import json

    second = svc.advance(db, pipeline.id)
    assert json.loads(second.spec_json)["engine"] == "doris"


def test_advance_past_the_end_is_rejected(domain):
    """链走到头就是走到头，不该悄悄再建一条制品。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = svc.create(
        db, name="单步链", intent=None, ontology_id=onto_id,
        steps=[{"kind": "materialize", "intent": "物化",
                "context": {"target_datasource_id": "ds-chain"}}],
    )
    svc.advance(db, pipeline.id)
    with pytest.raises(PipelineError, match="已全部起草"):
        svc.advance(db, pipeline.id)


def test_unregistered_kind_rejected_at_create_time(domain):
    """未实现的任务类型在**建链时**就拦掉——等到 advance 才发现，前几步已经跑完了。"""
    from app.agents import registry

    db, onto_id = domain
    with pytest.raises(registry.UnregisteredKindError):
        TaskPipelineService().create(
            db, name="坏链", intent=None, ontology_id=onto_id,
            steps=[
                {"kind": "materialize", "intent": "物化",
                 "context": {"target_datasource_id": "ds-chain", "target_database": "dw"}},
                {"kind": "nonexistent", "intent": "不存在的任务"},
            ],
        )
    # 校验在建行之前，故不留半条链
    assert all(
        p.name != "坏链" for p in TaskPipelineService().list_pipelines(db, ontology_id=onto_id)
    )


def test_pipeline_status_aggregates_from_artifacts(domain):
    """链的整体状态由各步制品聚合推导——某步失败即整条链 failed。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _chain(db, onto_id)
    first = svc.advance(db, pipeline.id)
    db.get(GovernanceArtifact, first.id).status = ArtifactStatus.FAILED.value
    db.commit()
    assert svc.detail(db, pipeline.id)["status"] == "failed"


def test_service_offers_no_way_to_execute_a_whole_chain():
    """链上没有「一键跑完」的入口。

    这不是遗漏而是设计：一键跑完必然绕过逐制品的人工确认，而「未确认不得执行」是这条
    流水线的硬不变量。C2 新增的 ``draft_all`` 是「一键**起草**全部」（所有制品先落地，
    人再逐个校验/确认/执行），**仍不执行**——它不违反这条不变量。
    """
    api = {n for n in dir(TaskPipelineService) if not n.startswith("_")}
    # C2：draft_all 只起草不执行，与 advance 并列；仍无 execute/confirm 入口。
    assert api == {"create", "advance", "draft_all", "detail", "require", "list_pipelines"}


# ---------------- Data Agent 侧：propose_pipeline ----------------


def _propose(db, onto_id: str, args: dict):
    from app.services.chat_bi import ChatBiService

    return ChatBiService()._dispatch_propose_pipeline(db, ontology_id=onto_id, args=args)


def test_propose_pipeline_emits_create_payload(domain):
    """产出的是**提案**：带前端原样回传的建链载荷，不写库、不起草任何制品。"""
    db, onto_id = domain
    result, summary, is_error = _propose(db, onto_id, {
        "name": "客户主数据入仓链",
        "steps": [
            {"kind": "materialize", "intent": "物化到数仓",
             "context": {"target_datasource_id": "ds-chain", "target_database": "dw"}},
            {"kind": "transform", "intent": "去重清洗"},
        ],
    })
    assert is_error is False
    assert [s["kind"] for s in result["create_payload"]["steps"]] == [
        "materialize", "transform"
    ]
    assert result["create_payload"]["ontology_id"] == onto_id
    assert "物化 → 加工" in summary
    # 提案阶段一条链都不该落库
    assert TaskPipelineService().list_pipelines(db, ontology_id=onto_id) == []


def test_downstream_need_not_repeat_upstream_context(domain):
    """必填 context 的校验要把继承算进去：清洗那步不必自己给目标数据源。

    若照单步的口径判缺，模型就会被迫在每一步重报同一个 id——那正是任务链要消灭的事。
    """
    db, onto_id = domain
    _result, _summary, is_error = _propose(db, onto_id, {
        "name": "链",
        "steps": [
            {"kind": "materialize", "intent": "物化",
             "context": {"target_datasource_id": "ds-chain", "target_database": "dw"}},
            # 第 2 步若也是物化，仍不必重给目标数据源和数据库——上游已经定下了
            {"kind": "materialize", "intent": "再物化一批"},
        ],
    })
    assert is_error is False


def test_first_step_still_needs_its_own_required_context(domain):
    """第一步没有上游可继承，缺的就是缺的——当场判错并附真实候选。"""
    db, onto_id = domain
    result, _summary, is_error = _propose(db, onto_id, {
        "name": "链",
        "steps": [
            {"kind": "materialize", "intent": "物化"},
            {"kind": "transform", "intent": "清洗"},
        ],
    })
    assert is_error is True
    assert result["missing"] == ["target_datasource_id", "target_database"]
    assert result["step_index"] == 0
    assert any(o["id"] == "ds-chain" for o in result["target_datasource_id_options"])


def test_single_step_pipeline_is_rejected(domain):
    """一件事就用 propose_action，别为单步套一条链。"""
    db, onto_id = domain
    result, _summary, is_error = _propose(db, onto_id, {
        "name": "链", "steps": [{"kind": "materialize", "intent": "物化"}],
    })
    assert is_error is True
    assert "propose_action" in result["error"]


def test_cluster_cannot_enter_a_data_pipeline(domain):
    """基建（cluster）不进数据加工链——它与这条流水线不是一回事。"""
    db, onto_id = domain
    result, _summary, is_error = _propose(db, onto_id, {
        "name": "链",
        "steps": [
            {"kind": "materialize", "intent": "物化",
             "context": {"target_datasource_id": "ds-chain", "target_database": "dw"}},
            {"kind": "cluster", "intent": "扩个节点"},
        ],
    })
    assert is_error is True
    assert "cluster" not in result["available"]


def test_pipeline_proposal_projects_to_block():
    """提案投影成 pipeline_proposal 块，前端据此渲染任务链卡。"""
    from app.services.chat_bi_blocks import answer_to_blocks

    blocks = answer_to_blocks({
        "answer": "",
        "pipeline_proposals": [{"kind": "pipeline", "name": "链", "create_payload": {}}],
    })
    assert [b["type"] for b in blocks].count("pipeline_proposal") == 1


# ---------------- C2：draft_all 一键起草全部 + 血缘 depends_on ----------------


def _two_step_chain(db, onto_id: str):
    """物化 → 清洗 的两步链（不含 metric——metric 起草需要已定义的业务逻辑）。"""
    return TaskPipelineService().create(
        db, name="两步链", intent=None, ontology_id=onto_id,
        steps=[
            {"kind": "materialize", "intent": "物化到数仓",
             "context": {"target_datasource_id": "ds-chain", "target_database": "dw"}},
            {"kind": "transform", "intent": "去重清洗", "context": {"target_table": "customer"}},
        ],
    )


def test_draft_all_drafts_every_step_without_upstream_success(domain):
    """C2：draft_all 一步起草全部步骤，不要求上游执行成功。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _two_step_chain(db, onto_id)
    artifacts = svc.draft_all(db, pipeline.id)
    assert len(artifacts) == 2
    detail = svc.detail(db, pipeline.id)
    assert all(s["artifact_id"] for s in detail["steps"])


def test_draft_all_is_idempotent(domain):
    """C2：已起草的步骤跳过（幂等）。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _two_step_chain(db, onto_id)
    first = svc.draft_all(db, pipeline.id)
    second = svc.draft_all(db, pipeline.id)
    assert len(second) == 0  # 全部已起草
    assert len(first) == 2


def test_draft_all_inherits_upstream_context(domain):
    """C2：draft_all 仍沿链继承上游落点（数据源/库），下游不必重报。"""
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = _two_step_chain(db, onto_id)
    svc.draft_all(db, pipeline.id)
    detail = svc.detail(db, pipeline.id)
    # 第 2 步（transform）应继承到第 1 步的 target_datasource_id
    transform_spec = db.get(GovernanceArtifact, detail["steps"][1]["artifact_id"])
    import json

    spec = json.loads(transform_spec.spec_json or "{}")
    assert spec.get("target_datasource_id") == "ds-chain"


def test_create_accepts_depends_on_lineage(domain):
    """C2：建链可带显式 depends_on（血缘依赖），落成 depends_on_json。"""
    db, onto_id = domain
    pipeline = TaskPipelineService().create(
        db, name="血缘链", intent=None, ontology_id=onto_id,
        steps=[
            {"kind": "materialize", "intent": "物化",
             "context": {"target_datasource_id": "ds-chain"}},
            {"kind": "transform", "intent": "清洗", "depends_on": [0]},
            {"kind": "metric", "intent": "聚合", "depends_on": [0, 1]},
        ],
    )
    detail = TaskPipelineService().detail(db, pipeline.id)
    assert detail["steps"][1]["depends_on"] == [0]
    assert detail["steps"][2]["depends_on"] == [0, 1]


def test_advance_uses_lineage_depends_on_not_linear(domain):
    """C2：advance 按血缘 depends_on 判断，不再要求「前面所有步」成功。

    步骤 2 依赖步骤 0（血缘），步骤 1 未起草/失败不影响步骤 2 起草。
    """
    db, onto_id = domain
    svc = TaskPipelineService()
    pipeline = svc.create(
        db, name="血缘链", intent=None, ontology_id=onto_id,
        steps=[
            {"kind": "materialize", "intent": "物化",
             "context": {"target_datasource_id": "ds-chain"}},
            {"kind": "metric", "intent": "聚合（跳过清洗）", "depends_on": [0],
             "context": {"business_logic_id": _logic_id(db, onto_id)}},
        ],
    )
    svc.advance(db, pipeline.id)  # 起草步骤 0
    # 步骤 1 依赖 0（已起草但未执行成功——advance 只要求上游**执行成功**）
    with pytest.raises(PipelineError, match="尚未执行成功"):
        svc.advance(db, pipeline.id)
    # 标记步骤 0 执行成功
    detail = svc.detail(db, pipeline.id)
    a0 = db.get(GovernanceArtifact, detail["steps"][0]["artifact_id"])
    a0.status = ArtifactStatus.SUCCEEDED.value
    db.commit()
    svc.advance(db, pipeline.id)  # 现在步骤 1 可起草（尽管步骤 0 是唯一上游）


def test_advance_rejects_self_dependency(domain):
    db, onto_id = domain
    with pytest.raises(PipelineError, match="不能依赖自己"):
        TaskPipelineService().create(
            db, name="自依赖链", intent=None, ontology_id=onto_id,
            steps=[
                {"kind": "materialize", "intent": "物化",
                 "context": {"target_datasource_id": "ds-chain"}, "depends_on": [0]},
            ],
        )
