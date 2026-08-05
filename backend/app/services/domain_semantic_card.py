"""域语义卡（P2.1）：把「这个域长什么样」变成常驻上下文，而不是每轮现查。

**为什么存在**：Claude Code 有 CLAUDE.md 常驻，项目结构先验是免费的；本项目的 Agent
每轮从零开始——域里有哪些业务板块、核心对象是谁、有哪些现成指标，全靠模型**主动调**
``get_domain_overview`` 才知道，还要占掉 6 步预算里的 1 步，结果又可能被截断。

于是那些本该由上下文承担的事，全被写成了 prompt 铁律（「概览类问题必须先调
get_domain_overview」「不得把样本当全集」…）。**架构缺陷用提示词打补丁**。

语义卡把这份先验前移：发布本体时生成、缓存，每轮直接拼进 system prompt。
模型开口前就知道域的骨架，也就不需要那几条纪律了。

**只统计已发布内容**——与 Agent 能检索到的世界严格一致，卡上有的一定查得到。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    EntityStatus,
    ObjectType,
    Ontology,
    Property,
    RelationType,
)
logger = logging.getLogger("ontometa.domain_card")

_TOP_CLUSTERS = 8       # 业务板块最多列几个
_TOP_OBJECTS = 10       # 核心对象（关系最多）最多列几个
_TOP_METRICS = 30       # 指标目录最多列几个
_PUB = EntityStatus.PUBLISHED.value


@dataclass(frozen=True)
class DomainSemanticCard:
    domain_name: str
    ontology_id: str
    object_count: int
    relation_count: int
    metric_count: int
    objects_with_relations: int
    clusters: list[tuple[str, int]] = field(default_factory=list)      # (板块名, 成员数)
    core_objects: list[tuple[str, int]] = field(default_factory=list)  # (显示名, 关系数)
    role_counts: dict[str, int] = field(default_factory=dict)
    metrics: list[str] = field(default_factory=list)                   # 已发布指标显示名
    compilable_metrics: int = 0                                        # 其中已形式化的
    metrics_truncated: bool = False
    naming_note: str = ""

    def render(self) -> str:
        """渲染成拼进 system prompt 的紧凑文本。"""
        lines = [f"【{self.domain_name}·域语义卡】（以下均为**已发布**内容，可直接检索到）"]
        lines.append(
            f"规模：{self.object_count} 个业务对象 / {self.relation_count} 条关系 / "
            f"{self.metric_count} 个指标；其中 {self.objects_with_relations} 个对象有关系、"
            f"{self.object_count - self.objects_with_relations} 个无关系。"
        )
        if self.role_counts:
            roles = "、".join(f"{k} {v}" for k, v in sorted(self.role_counts.items()))
            lines.append(f"对象角色分布：{roles}。")
        if self.clusters:
            parts = "、".join(f"{name}({n})" for name, n in self.clusters)
            lines.append(f"业务板块：{parts}。")
        if self.core_objects:
            parts = "、".join(f"{name}({n} 条关系)" for name, n in self.core_objects)
            lines.append(f"核心对象（关联最多）：{parts}。")
        if self.metrics:
            tail = " 等" if self.metrics_truncated else ""
            lines.append(
                f"已发布指标：{('、'.join(self.metrics))}{tail}"
                f"（{self.compilable_metrics} 个已形式化、可用 compile_metric 编译）。"
            )
        if self.naming_note:
            lines.append(f"命名规范：{self.naming_note}")
        lines.append(
            "以上是**骨架**，不是全集：具体字段、口径细节、完整清单仍需调工具获取。"
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------- 缓存


class _CardCache:
    """按 (ontology_id, version, published_at) 缓存。

    键里带版本与发布时间：重新发布必然改变其一，缓存自动失效——不必依赖调用方
    记得清缓存（那种「记得调 reset」的约定迟早会漏）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple, DomainSemanticCard] = {}

    def get(self, key: tuple) -> DomainSemanticCard | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: tuple, card: DomainSemanticCard) -> None:
        with self._lock:
            self._data[key] = card

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CACHE = _CardCache()


def reset_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------- 构建


def build_card(db: Session, ontology: Ontology, domain_name: str) -> DomainSemanticCard:
    """构建（或取缓存的）域语义卡。任何异常都不得拖垮问答——调用方负责兜底。"""
    key = (ontology.id, ontology.version, str(ontology.published_at or ""))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    objects = (
        db.query(ObjectType)
        .filter(ObjectType.ontology_id == ontology.id, ObjectType.status == _PUB)
        .all()
    )
    relations = (
        db.query(RelationType)
        .filter(RelationType.ontology_id == ontology.id, RelationType.status == _PUB)
        .all()
    )
    logics = (
        db.query(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology.id, BusinessLogic.status == _PUB)
        .order_by(BusinessLogic.name)
        .all()
    )

    obj_ids = {o.id for o in objects}
    degree: Counter = Counter()
    for r in relations:
        if r.source_object_type_id in obj_ids and r.target_object_type_id in obj_ids:
            degree[r.source_object_type_id] += 1
            degree[r.target_object_type_id] += 1

    obj_by_id = {o.id: o for o in objects}
    core = [
        (obj_by_id[oid].display_name, cnt)
        for oid, cnt in degree.most_common(_TOP_OBJECTS)
        if oid in obj_by_id
    ]

    card = DomainSemanticCard(
        domain_name=domain_name,
        ontology_id=ontology.id,
        object_count=len(objects),
        relation_count=len(relations),
        metric_count=len(logics),
        objects_with_relations=len(degree),
        clusters=_clusters(objects, relations),
        core_objects=core,
        role_counts=dict(Counter(o.table_role or "unknown" for o in objects)),
        metrics=[l.display_name for l in logics[:_TOP_METRICS]],
        compilable_metrics=sum(1 for l in logics if (l.expression_json or "").strip()),
        metrics_truncated=len(logics) > _TOP_METRICS,
        naming_note=_naming_note(db, objects),
    )
    _CACHE.put(key, card)
    return card


def _clusters(
    objects: list[ObjectType], relations: list[RelationType]
) -> list[tuple[str, int]]:
    """业务板块：复用既有社区检测，只喂**已发布**子图。

    ``get_ontology_grouped_graph`` 会混入未发布草稿，不能直接用于语义卡——
    卡上写的每一条都必须是 Agent 真能检索到的。
    """
    if not objects:
        return []
    try:
        from app.services.ontology_query import OntologyQueryService

        part = OntologyQueryService()._compute_cluster_partition(
            objects, relations, max_cluster_nodes=1
        )
        out = [
            (part.cluster_names[cid], len(part.cluster_members[cid]))
            for cid in part.cluster_order[:_TOP_CLUSTERS]
        ]
        return [(name, n) for name, n in out if name]
    except Exception as exc:  # noqa: BLE001 — 板块是锦上添花，算不出就不写
        logger.info("domain card clustering skipped: %s", exc)
        return []


def _naming_note(db: Session, objects: list[ObjectType]) -> str:
    """从已发布对象/字段的实际命名里**观察**出规范，而不是假设一套。

    这条直接顶替了 prompt 里那句「检索关键词优先用中文（本体以中文命名）」——
    真实规范应当来自数据，不同域完全可能不同。
    """
    if not objects:
        return ""
    obj_cn = sum(1 for o in objects if _has_cjk(o.display_name))
    parts = []
    if obj_cn > len(objects) / 2:
        parts.append("对象显示名以中文为主（检索关键词优先用中文）")
    else:
        parts.append("对象显示名以英文为主")

    props = (
        db.query(Property.display_name)
        .filter(
            Property.object_type_id.in_([o.id for o in objects[:200]]),
            Property.status == _PUB,
        )
        .limit(500)
        .all()
    )
    if props:
        cn = sum(1 for (dn,) in props if _has_cjk(dn or ""))
        parts.append("字段显示名以中文为主" if cn > len(props) / 2 else "字段显示名以英文为主")
    return "；".join(parts) + "。"


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text or "")


__all__ = ["DomainSemanticCard", "build_card", "reset_cache"]
