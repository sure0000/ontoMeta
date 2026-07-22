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
