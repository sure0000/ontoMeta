"""facet-A 跨表实体消解：把同一真实业务对象在多表/多域的重复识别出来，
提出「权威源(canonical) + 归并候选」，交治理流程人工确认（绝不自动物理改动）。

管线三段（对齐 retrieve-then-judge）：
1. **Block（召回）**：按业务字段签名 / 名称词干把疑似同一实体的对象聚成候选组，
   避免 N² 全比。只考虑业务对象角色，天然排除子表/数据表/技术表。
2. **Judge（判定）**：可注入的「是否同一实体」判据（默认确定性启发式：字段签名
   完全一致=同一；高相似=待判）。真实场景可换成 LLM judge。
3. **Authority（权威源）**：veto→打分→margin 选 canonical——人工认证/已发布直选；
   否则按 hub 入度 + 字段完整度 + 分层/容器 - 物理阶段命名惩罚 打分；margin 门控
   决定置信度。**所有归并候选一律 needs_review=True**（高 blast-radius，必人工确认）。

本模块不写库、不调用 LLM（judge 可注入），便于离线验证与单测。canonical_term_id
落库与 ChangeConfirmation 落地由上层治理接管。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# 物理阶段/临时命名词元：作为 canonical 的命名惩罚（这些不该被选为权威源）。
_STAGE_TOKENS = ("stg", "ods", "tmp", "temp", "bak", "backup", "stage", "raw", "wrk", "work")
# 分层/容器命名词元：更可能是精炼后的权威实体（维度/明细/整合层）。
_CANONICAL_LAYER_TOKENS = ("dim", "dwd", "dws", "curated", "conformed", "master", "mdm")
# 字段签名过小（如单列查找表）时，签名完全一致多为结构巧合而非同一实体
# （Issue Type 与 Warehouse Type 都只有一个 name 列）。此时不凭签名归并，
# 必须叠加名称词干一致才算候选，避免把互不相干的小查找表误并。
_MIN_SIGNATURE_FIELDS = 3


def _norm_stem(name: str) -> str:
    """归一化名称词干：小写、去分隔符、去阶段/分层前缀、去复数尾。"""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    base = re.sub(
        r"^(stg|ods|tmp|temp|bak|dim|fact|dwd|dws|ads|raw|stage|t|dm)_", "", base
    )
    return base.rstrip("s")


@dataclass
class ResolutionInput:
    """参与消解的单个对象的归一化信号（独立于 DB/证据来源，便于测试）。"""

    id: str  # 稳定标识（urn 或对象 id）
    name: str  # 技术/候选名
    display_name: str = ""
    business_fields: frozenset[str] = field(default_factory=frozenset)
    role: str = "business_object"
    fk_in_degree: int = 0  # 被多少表引用（hub 度）
    row_count: int | None = None
    is_published: bool = False
    is_certified: bool = False  # 人工认证/已挂术语


@dataclass
class MergeCandidate:
    """一组疑似同一实体的归并候选，含建议的权威源。"""

    canonical_id: str
    member_ids: list[str]
    confidence: float
    margin: float
    reason: str
    needs_review: bool = True


# 同实体判据：给定一组候选，返回 (是否同一实体, 置信度, 理由)。可注入 LLM 版本。
SameEntityJudge = Callable[[list[ResolutionInput]], "tuple[bool, float, str]"]

ROLE_BUSINESS_OBJECT = "business_object"


class _UnionFind:
    def __init__(self, keys: list[str]) -> None:
        self.parent = {k: k for k in keys}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def default_same_entity_judge(group: list[ResolutionInput]) -> tuple[bool, float, str]:
    """确定性判据：字段签名完全一致 → 高置信同一实体；否则按平均 Jaccard 给中置信，
    交人工。用于无 LLM 场景与单测的可预测行为。"""
    sigs = {m.business_fields for m in group}
    if len(sigs) == 1 and next(iter(sigs)):
        return True, 0.9, "业务字段签名完全一致"
    # 平均两两 Jaccard
    sims = []
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            sims.append(_jaccard(group[i].business_fields, group[j].business_fields))
    avg = sum(sims) / len(sims) if sims else 0.0
    return (avg >= 0.6), round(0.5 + avg * 0.3, 2), f"业务字段平均相似度 {avg:.0%}"


class ObjectResolver:
    """facet-A 消解器。"""

    def __init__(
        self,
        *,
        name_jaccard_threshold: float = 0.7,
        margin_threshold: float = 1.0,
        judge: SameEntityJudge | None = None,
    ) -> None:
        self.name_jaccard_threshold = name_jaccard_threshold
        self.margin_threshold = margin_threshold
        self.judge = judge or default_same_entity_judge

    # ---- Block ----
    def block(self, inputs: list[ResolutionInput]) -> list[list[ResolutionInput]]:
        """把疑似同一实体的对象聚成候选组（只含业务对象；单例不返回）。"""
        objs = [i for i in inputs if i.role == ROLE_BUSINESS_OBJECT and i.business_fields]
        if len(objs) < 2:
            return []
        uf = _UnionFind([o.id for o in objs])
        by_id = {o.id: o for o in objs}

        # 1) 业务字段签名完全一致 → 同组。仅当签名足够大(非平凡查找表)时才凭签名
        #    归并；过小签名交由下面的「词干+相似度」路径，避免误并无关小表。
        sig_buckets: dict[frozenset[str], list[str]] = {}
        for o in objs:
            if len(o.business_fields) >= _MIN_SIGNATURE_FIELDS:
                sig_buckets.setdefault(o.business_fields, []).append(o.id)
        for ids in sig_buckets.values():
            for other in ids[1:]:
                uf.union(ids[0], other)

        # 2) 名称词干相同 + 业务字段 Jaccard 达阈 → 同组（捕获 stg/dim 同实体变体）。
        stem_buckets: dict[str, list[str]] = {}
        for o in objs:
            stem_buckets.setdefault(_norm_stem(o.name), []).append(o.id)
        for ids in stem_buckets.values():
            for k in range(1, len(ids)):
                a, b = by_id[ids[0]], by_id[ids[k]]
                if _jaccard(a.business_fields, b.business_fields) >= self.name_jaccard_threshold:
                    uf.union(a.id, b.id)

        groups: dict[str, list[ResolutionInput]] = {}
        for o in objs:
            groups.setdefault(uf.find(o.id), []).append(o)
        return [g for g in groups.values() if len(g) >= 2]

    # ---- Authority ----
    def _authority_score(self, m: ResolutionInput) -> float:
        """权威源打分（越高越像该组精炼后的权威实体）。"""
        score = 0.0
        if m.is_certified:
            score += 5.0
        if m.is_published:
            score += 3.0
        score += min(m.fk_in_degree, 10) * 0.5  # hub 入度
        score += len(m.business_fields) * 0.1  # 字段完整度
        stem = (m.name or "").lower()
        if any(tok in re.split(r"[^a-z0-9]+", stem) for tok in _STAGE_TOKENS):
            score -= 2.0  # 物理阶段/临时命名惩罚
        if any(tok in re.split(r"[^a-z0-9]+", stem) for tok in _CANONICAL_LAYER_TOKENS):
            score += 1.0  # 分层/整合命名加成
        return score

    def _pick_canonical(
        self, group: list[ResolutionInput]
    ) -> tuple[ResolutionInput, float, str]:
        """veto→打分→margin：返回 (canonical, margin, 理由)。"""
        # 硬否决/直选：有人工认证的唯一成员直接为权威源。
        certified = [m for m in group if m.is_certified]
        if len(certified) == 1:
            return certified[0], 99.0, "人工认证，直选为权威源"
        scored = sorted(group, key=self._authority_score, reverse=True)
        top, second = scored[0], scored[1]
        margin = self._authority_score(top) - self._authority_score(second)
        reason = (
            f"权威源打分最高（hub入度{top.fk_in_degree}/字段{len(top.business_fields)}），"
            f"领先第二名 {margin:.1f}"
            + ("（差距不足，建议人工指认）" if margin < self.margin_threshold else "")
        )
        return top, margin, reason

    # ---- Resolve ----
    def resolve(self, inputs: list[ResolutionInput]) -> list[MergeCandidate]:
        """产出归并候选（全部 needs_review=True，绝不自动应用）。"""
        candidates: list[MergeCandidate] = []
        for group in self.block(inputs):
            same, judge_conf, judge_reason = self.judge(group)
            if not same:
                continue
            canonical, margin, auth_reason = self._pick_canonical(group)
            members = [m.id for m in group if m.id != canonical.id]
            # 置信度：判据置信 × margin 门控（margin 不足则压低）。
            conf = judge_conf if margin >= self.margin_threshold else min(judge_conf, 0.6)
            candidates.append(
                MergeCandidate(
                    canonical_id=canonical.id,
                    member_ids=members,
                    confidence=round(conf, 2),
                    margin=round(margin, 2),
                    reason=f"{judge_reason}；{auth_reason}",
                    needs_review=True,
                )
            )
        return candidates


def build_resolution_inputs(bundle, evidence) -> list[ResolutionInput]:
    """从 DataHubDomainBundle + EvidenceBundle 组装消解输入（用于离线验证）。

    - 业务字段：剥离源画像系统列 + 主键/外键后的字段集合。
    - role：取 evidence 的 table_role。
    - fk_in_degree：由 evidence.relations 的 foreign_key 关系按 target 计入度。
    """
    from app.services.source_profile import detect_source_profile

    profile = detect_source_profile(bundle)
    # 候选名 → evidence 对象（拿 role），及 evidence 里对象名到 urn 的映射。
    role_by_urn = {o.source_dataset_urn: o.table_role for o in evidence.object_types}
    # fk 入度：按 target_object 名计数不同 source。
    fk_sources: dict[str, set[str]] = {}
    for r in evidence.relations:
        if r.structure_type == "foreign_key":
            fk_sources.setdefault(r.target_object, set()).add(r.source_object)
    fk_in_degree_by_obj = {k: len(v) for k, v in fk_sources.items()}

    from app.services.evidence_builder import _infer_object_name

    inputs: list[ResolutionInput] = []
    for ds in bundle.datasets:
        obj_name = _infer_object_name(ds.name)
        pk_names = profile.primary_key_names(ds)
        biz = frozenset(
            f.name.lower()
            for f in ds.fields
            if not profile.is_system_column(f.name)
            and f.name.lower() not in pk_names
            and not f.is_foreign_key
        )
        inputs.append(
            ResolutionInput(
                id=ds.urn,
                name=ds.name,
                display_name=ds.display_name or ds.name,
                business_fields=biz,
                role=role_by_urn.get(ds.urn, ROLE_BUSINESS_OBJECT),
                fk_in_degree=fk_in_degree_by_obj.get(obj_name, 0),
                row_count=ds.row_count,
                is_certified=bool(ds.glossary_terms),
            )
        )
    return inputs
