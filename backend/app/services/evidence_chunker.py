"""证据包分块工具。

草稿生成默认把整个 ``EvidenceBundle`` 一次性序列化后交给 LLM；当数据域下
表多、字段多时会超出模型上下文窗口。本模块提供纯函数式的分块能力：按
「对象(数据集)」为最小单元把证据包贪心装箱成若干子包,每个子包序列化后
不超过给定字符预算,并把两端落在不同子包的跨块关系单独收集出来,交由上层
的「关系专用块」统一处理。

这些函数不触碰 LLM、无副作用,便于单元测试。
"""

from __future__ import annotations

import json
from collections import defaultdict

from app.schemas import EvidenceBundle, RelationEvidencePack

# 序列化时的字段:与 draft_generator._build_prompt 保持一致(排除 business_logics),
# 使这里的体量估计与真正送入 LLM 的 payload 一致。
_SERIALIZE_EXCLUDE = {"business_logics"}


def estimate_size(bundle: EvidenceBundle) -> int:
    """估算证据包序列化后的字符长度,与实际送入 LLM 的 payload 口径一致。"""
    payload = bundle.model_dump(exclude=_SERIALIZE_EXCLUDE)
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def split_evidence(
    bundle: EvidenceBundle,
    budget: int,
    max_tables_per_chunk: int | None = None,
) -> tuple[list[EvidenceBundle], list[RelationEvidencePack]]:
    """把证据包按预算切成多个子包,并分离出跨块关系。

    分块单元为「对象单元」= 一个 ``ObjectTypeEvidencePack`` 及其全部
    ``PropertyEvidencePack``(以 ``object_candidate_name == candidate_name`` 匹配)。
    用贪心装箱把对象单元合并进子包,每个子包(对象 + 属性)序列化后不超过
    ``budget``,且表数不超过 ``max_tables_per_chunk``;单个对象单元本身超预算
    时仍单独成块(对象是最细粒度)。

    按表数分块是主策略(默认每批最多 ``max_tables_per_chunk`` 张表),字符预算
    是兜底细分:一批表还没到数量上限但序列化已超预算时,提前切块,批内表数
    自然收紧。

    关系归类:两端对象落在同一子包 → 随该子包送入;两端跨子包或某端对象缺失
    → 收入返回的跨块关系列表。

    返回 ``(子包列表, 跨块关系列表)``。
    """
    if max_tables_per_chunk is None:
        from app.config import settings

        max_tables_per_chunk = settings.draft_chunk_table_batch_size
    # 按对象归集属性。
    props_by_obj: dict[str, list] = defaultdict(list)
    for prop in bundle.properties:
        props_by_obj[prop.object_candidate_name].append(prop)

    def _intra_relations(names: set[str]) -> list[RelationEvidencePack]:
        """两端都落在给定对象集合内的关系(会随子包一起送入 LLM)。"""
        return [
            rel
            for rel in bundle.relations
            if rel.source_object in names and rel.target_object in names
        ]

    # 贪心装箱:逐个对象单元累加,超预算且当前块非空则另起一块。
    # 体量估计包含块内关系,使子包真实 payload 不超预算(单对象块除外)。
    packed: list[dict] = []
    current_ots: list = []
    current_props: list = []
    current_names: set[str] = set()

    for obj in bundle.object_types:
        obj_props = props_by_obj.get(obj.candidate_name, [])
        trial_names = current_names | {obj.candidate_name}
        trial = EvidenceBundle(
            object_types=[*current_ots, obj],
            properties=[*current_props, *obj_props],
            relations=_intra_relations(trial_names),
        )
        if current_ots and (
            len(current_ots) >= max_tables_per_chunk or estimate_size(trial) > budget
        ):
            packed.append({"object_types": current_ots, "properties": current_props})
            current_ots = [obj]
            current_props = list(obj_props)
            current_names = {obj.candidate_name}
        else:
            current_ots = [*current_ots, obj]
            current_props = [*current_props, *obj_props]
            current_names = trial_names

    if current_ots:
        packed.append({"object_types": current_ots, "properties": current_props})

    # 对象 → 子包序号,用于关系归类。
    obj_to_chunk: dict[str, int] = {}
    for idx, chunk in enumerate(packed):
        for obj in chunk["object_types"]:
            obj_to_chunk[obj.candidate_name] = idx

    chunk_relations: list[list] = [[] for _ in packed]
    cross_relations: list[RelationEvidencePack] = []
    for rel in bundle.relations:
        source_idx = obj_to_chunk.get(rel.source_object)
        target_idx = obj_to_chunk.get(rel.target_object)
        if source_idx is not None and source_idx == target_idx:
            chunk_relations[source_idx].append(rel)
        else:
            cross_relations.append(rel)

    sub_bundles = [
        EvidenceBundle(
            object_types=chunk["object_types"],
            properties=chunk["properties"],
            relations=chunk_relations[idx],
        )
        for idx, chunk in enumerate(packed)
    ]
    return sub_bundles, cross_relations


def split_relations(
    bundle: EvidenceBundle,
    budget: int,
    max_relations_per_chunk: int | None = None,
) -> list[EvidenceBundle]:
    """把*全部*关系(不受对象分块边界限制)按预算切成多个自包含子包。

    与 ``split_evidence`` 是两条独立的分块流水线:对象流水线负责对象/属性
    命名,关系流水线负责关系业务命名,互不依赖对方的输出,可并发执行、独立
    断点续跑。因此这里对 ``bundle.relations`` 做全量分块(而非只处理某个对象
    子包内部的关系),天然覆盖了两端对象落在不同对象子包的跨块关系——
    ``split_evidence`` 返回的 cross_relations 不会再有遗漏未增强的问题。

    分块单元为单条关系;每个子包附带该批关系涉及到的两端对象概要(仅
    candidate_name/display_name/description,不含 properties)作为业务背景,
    帮助 LLM 推断关系语义,但不参与结构组装、也不要求对象命名增强已完成。

    按关系数分块是主策略(默认每批最多 ``max_relations_per_chunk`` 条),字符
    预算是兜底细分;单条关系本身超预算时仍单独成块。
    """
    if max_relations_per_chunk is None:
        from app.config import settings

        max_relations_per_chunk = settings.draft_chunk_relation_batch_size

    obj_by_candidate = {ot.candidate_name: ot for ot in bundle.object_types}

    def _context_objects(names: set[str]) -> list:
        return [obj_by_candidate[n] for n in sorted(names) if n in obj_by_candidate]

    packed: list[tuple[list, set[str]]] = []
    current: list = []
    current_names: set[str] = set()

    for rel in bundle.relations:
        trial_names = current_names | {rel.source_object, rel.target_object}
        trial = EvidenceBundle(
            object_types=_context_objects(trial_names),
            properties=[],
            relations=[*current, rel],
        )
        if current and (
            len(current) >= max_relations_per_chunk or estimate_size(trial) > budget
        ):
            packed.append((current, current_names))
            current = [rel]
            current_names = {rel.source_object, rel.target_object}
        else:
            current = [*current, rel]
            current_names = trial_names

    if current:
        packed.append((current, current_names))

    return [
        EvidenceBundle(
            object_types=_context_objects(names),
            properties=[],
            relations=rels,
        )
        for rels, names in packed
    ]
