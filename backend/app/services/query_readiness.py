"""被就绪闸门拦下时：先把陈旧的运行状态对平，再决定拒不拒，拒也要说清卡在哪。

``query_routing.readiness_error`` 判的是 Projection 上那条落数结论，而这条结论只有
**有人去读制品**时才会被 Airflow 的真实状态推进（``AgentPipelineService.get`` →
``_reconcile_orchestrated_status``）。读路径的可用性因此挂在「有没有人打开某个页面」上。

实测事故：客户分组的一次同步 04:08:10 就已经 success，回执却冻在 ``queued``；
中间态先把 ``projection.queryable`` 置了 False，此后没有任何东西再推进它，于是那张表
连续 6 小时查不了，Data Agent 每次只会重复一句「对象尚未同步或加工完成，不可查询」——
数据明明就在 ODS 里躺着。

这里把「读时对账」接到真正吃亏的那条路径上：

* :func:`reconcile_blocking_runs` —— 查询将因未就绪被拒时，先把相关对象挂着的非终态
  同步运行对一次账（复用制品服务里那份唯一的对账实现），让调用方重判一次。
* :func:`readiness_detail` —— 对不动就照旧拒答，但错误信息带上是哪一次运行、什么状态、
  卡了多久、上一次成功落数是什么时候。模型据此能给出用户可行动的解释，而不是空转重试。

只处理**同步契约**这一类阻塞源：它有 ``IngestionContract`` 这条明确的对象↔运行链路，
证据也出自这里。transform/materialize 造成的不可查由 :func:`readiness_detail` 如实转述
Projection 上的状态，不猜测运行。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import GovernanceArtifact, IngestionContract, ObjectType
from app.models.agent import ArtifactStatus

# 契约处于这些状态时，Projection 的读权限被中间态压着，值得回头问一次 Airflow。
_UNSETTLED_CONTRACT_STATUS = frozenset({"running", "submitted"})
# 只有这些制品状态还可能被对账推进；terminal 的不必再问。
_UNSETTLED_ARTIFACT_STATUS = frozenset(
    {ArtifactStatus.EXECUTING.value, ArtifactStatus.SUCCEEDED.value}
)


def _objects(
    db: Session, *, ontology_ids: list[str], object_names: list[str]
) -> list[ObjectType]:
    """把 SQL 里的对象 token 解析成本体对象行（大小写不敏感，按 name 匹配）。"""
    wanted = {str(n).strip().lower() for n in object_names or [] if str(n).strip()}
    if not wanted or not ontology_ids:
        return []
    rows = (
        db.query(ObjectType)
        .filter(ObjectType.ontology_id.in_(list(dict.fromkeys(ontology_ids))))
        .all()
    )
    return [o for o in rows if (o.name or "").strip().lower() in wanted]


def blocking_contracts(
    db: Session, *, ontology_ids: list[str], object_names: list[str]
) -> list[IngestionContract]:
    """这些对象上「还没落定」的同步契约。ready/failed 都算落定，不在其列。"""
    objects = _objects(db, ontology_ids=ontology_ids, object_names=object_names)
    if not objects:
        return []
    contracts = (
        db.query(IngestionContract)
        .filter(IngestionContract.object_type_id.in_([o.id for o in objects]))
        .all()
    )
    return [c for c in contracts if (c.status or "") in _UNSETTLED_CONTRACT_STATUS]


def _sync_artifact_of(db: Session, contract_id: str) -> GovernanceArtifact | None:
    """找这条契约最近一次还没对出终态的同步制品。

    回执里的 ``ingestion_contract_id`` 是契约↔制品的唯一链路（``reconcile_sync_receipt``
    也认它）。候选集是「非终态的 sync 制品」，本就只有个位数，故在 Python 里比对回执，
    不对 JSON 列做 LIKE 扫描。
    """
    candidates = (
        db.query(GovernanceArtifact)
        .filter(
            GovernanceArtifact.kind == "sync",
            GovernanceArtifact.status.in_(sorted(_UNSETTLED_ARTIFACT_STATUS)),
        )
        .order_by(GovernanceArtifact.updated_at.desc())
        .all()
    )
    for artifact in candidates:
        try:
            receipt = json.loads(artifact.execution_receipt_json or "{}")
        except (TypeError, ValueError):
            continue
        if str(receipt.get("ingestion_contract_id") or "") == contract_id:
            return artifact
    return None


def reconcile_blocking_runs(
    db: Session, *, ontology_ids: list[str] | None, object_names: list[str] | None
) -> bool:
    """把这些对象挂着的非终态同步运行对一次账。返回是否有状态被推进。

    对账本身复用 ``AgentPipelineService.get``（内部即 ``_reconcile_orchestrated_status``）：
    它是全仓唯一一份「问 Airflow → 回写制品 → 镜像契约 → 推进 Projection」的实现，
    这里不重写一份。Airflow 不可达 / 无凭据时那份实现自己 best-effort 静默返回，
    所以本函数在离线环境里是个空操作，不会把查询路径拖垮。
    """
    contracts = blocking_contracts(
        db,
        ontology_ids=list(ontology_ids or []),
        object_names=list(object_names or []),
    )
    if not contracts:
        return False

    from app.api.deps import agent_pipeline

    advanced = False
    for contract in contracts:
        artifact = _sync_artifact_of(db, contract.id)
        if artifact is None:
            continue
        before = (artifact.status, contract.status)
        agent_pipeline.get(db, artifact.id)
        db.refresh(contract)
        db.refresh(artifact)
        if (artifact.status, contract.status) != before:
            advanced = True
    return advanced


def _age(started: datetime | None) -> str:
    if started is None:
        return "开始时间未知"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started_naive = started.replace(tzinfo=None) if started.tzinfo else started
    minutes = max(0, int((now - started_naive).total_seconds() // 60))
    if minutes < 60:
        return f"已 {minutes} 分钟"
    return f"已 {minutes // 60} 小时 {minutes % 60} 分钟"


def readiness_detail(
    db: Session, *, ontology_ids: list[str] | None, object_names: list[str] | None
) -> str:
    """把「为什么不可查」摊开成可行动的一句话。查不出线索就返回空串。

    拒答本身不是问题，「拒了却不说卡在哪」才是：实测里模型被这句话挡住后只会换个写法
    重试三次，最后把这句原文引进答案，还被答案校验当成幻觉整条拒掉。
    """
    contracts = blocking_contracts(
        db,
        ontology_ids=list(ontology_ids or []),
        object_names=list(object_names or []),
    )
    if not contracts:
        return ""
    objects = {
        o.id: o
        for o in _objects(
            db,
            ontology_ids=list(ontology_ids or []),
            object_names=list(object_names or []),
        )
    }
    parts: list[str] = []
    for contract in contracts:
        obj = objects.get(contract.object_type_id)
        label = (obj.display_name or obj.name) if obj else contract.target_ods_table
        artifact = _sync_artifact_of(db, contract.id)
        if artifact is not None:
            run = (
                f"同步任务「{artifact.name}」{_age(artifact.executed_at)}"
                f"处于 {artifact.status}"
            )
        else:
            run = f"同步契约状态为 {contract.status}，没有找到对应的任务运行"
        last = (
            f"，最近一次成功落数在 {contract.last_success_at:%Y-%m-%d %H:%M}"
            if contract.last_success_at
            else "，此前从未成功落数"
        )
        parts.append(f"{label}：{run}{last}")
    return "；".join(parts)
