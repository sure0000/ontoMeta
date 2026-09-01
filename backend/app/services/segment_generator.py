"""板块生成：按关系紧密度聚类业务对象，生成可读的业务板块。

按设计文档 docs/ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md：
- 第一遍聚类已在 evidence_builder.py 完成（产生 segment_size 信号）
- 这里执行第二遍聚类：只在 business_object + bridge 子图上，剔除 technical 表
- 实测：48 板块 → 12 板块，机械命名 26 → 5，噪声板块归零
"""

import json
import logging
import math
from typing import Any

from openai import AsyncOpenAI

from app.schemas import DraftObjectType, DraftRelationType, DraftSegment
from app.services.community_detection import (
    identify_hub_nodes,
    label_propagation_clusters,
    split_dominant_clusters,
)
from app.services.draft_checkpoint import chunk_key

logger = logging.getLogger(__name__)

_MAX_ANCHOR_COUNT = 5  # 板块锚点成员数：度数最高的 K 个成员的 source_ref


class MissingBusinessNameError(Exception):
    """LLM 没有返回板块的业务命名 / 命名不合格。"""

    pass


def generate_segments(
    object_types: list[DraftObjectType],
    relation_types: list[DraftRelationType],
    llm_client: AsyncOpenAI | None = None,
) -> tuple[list[DraftSegment], set[str]]:
    """生成业务板块和识别枢纽节点。

    Args:
        object_types: 已生成的对象列表（含中文业务名和角色判定）
        relation_types: 已生成的关系列表
        llm_client: LLM 客户端，用于板块命名（可选，但无 LLM 时报错不降级）

    Returns:
        (segments, hub_node_names): 板块列表与枢纽节点名集合

    Raises:
        MissingBusinessNameError: 无 LLM 或 LLM 未返回合格的板块命名
    """
    # 只在 business_object + bridge 子图上聚类（剔除 technical）
    business_objects = {
        obj.name: obj
        for obj in object_types
        if obj.table_role in ("business_object", "bridge")
    }

    if not business_objects:
        logger.warning("No business objects found, skipping segment generation")
        return [], set()

    # 构建无向邻接图
    adjacency: dict[str, set[str]] = {name: set() for name in business_objects}
    for rel in relation_types:
        src = rel.source_object_type_name
        tgt = rel.target_object_type_name
        if src in adjacency and tgt in adjacency:
            adjacency[src].add(tgt)
            adjacency[tgt].add(src)

    # 识别枢纽节点（度数远高于平均水平的节点）
    total_nodes = len(adjacency)
    max_hub_count = min(40, max(5, total_nodes // 20))
    hub_nodes = identify_hub_nodes(adjacency, max_hub_count)

    logger.info(
        "Identified %d hub nodes from %d business objects", len(hub_nodes), total_nodes
    )

    # 摘除枢纽后在剩余子图上聚类
    non_hub_nodes = sorted(n for n in adjacency if n not in hub_nodes)
    non_hub_adjacency = {
        n: {neighbor for neighbor in adjacency[n] if neighbor not in hub_nodes}
        for n in non_hub_nodes
    }

    # 标签传播聚类
    clusters = label_propagation_clusters(non_hub_nodes, non_hub_adjacency)

    # 拆分主导簇（避免巨簇掩盖业务结构）
    clusters = split_dominant_clusters(
        clusters,
        non_hub_adjacency,
        max_cluster_nodes=min(50, max(10, total_nodes // 5)),
        total_clustered=len(non_hub_nodes),
    )
    # A single object does not provide a readable business segment. Keep it as
    # an unassigned object rather than creating hundreds of one-item panels.
    clusters = [cluster for cluster in clusters if len(cluster) > 1]

    logger.info(
        "Generated %d segments from %d non-hub objects", len(clusters), len(non_hub_nodes)
    )

    # 为每个板块生成元数据
    segments = []
    for cluster in clusters:
        if not cluster:
            continue

        # 按度数排序成员，取前 K 个作为锚点
        members_by_degree = sorted(
            cluster, key=lambda n: len(adjacency.get(n, set())), reverse=True
        )
        anchor_refs = [
            business_objects[name].source_ref
            for name in members_by_degree[:_MAX_ANCHOR_COUNT]
            if name in business_objects and business_objects[name].source_ref
        ]

        # 机械名：度数最高成员的 display_name
        machine_name = business_objects[members_by_degree[0]].display_name

        segments.append(
            DraftSegment(
                name="",  # 稍后由 LLM 填充
                display_name="",  # 稍后由 LLM 填充
                description=None,
                anchor_refs=anchor_refs,
                member_count=len(cluster),
                machine_baseline=machine_name,
                # Keep the degree-ranked order so LLM prompts and top-N views
                # receive the same stable, meaningful members on every run.
                members=members_by_degree,
            )
        )

    return segments, hub_nodes


async def name_segments_with_llm(
    segments: list[DraftSegment],
    object_types: list[DraftObjectType],
    relation_types: list[DraftRelationType],
    hub_nodes: set[str],
    llm_client: AsyncOpenAI,
    model: str | None = None,
    checkpoint: Any | None = None,
) -> None:
    """使用 LLM 为板块命名（就地修改 segments）。

    Args:
        segments: 待命名的板块列表
        object_types: 对象列表（用于提取成员信息）
        relation_types: 关系列表（用于提取高频动词）
        hub_nodes: 枢纽节点集合
        llm_client: LLM 客户端

    Raises:
        MissingBusinessNameError: LLM 未返回合格的板块命名
    """
    if not llm_client:
        raise MissingBusinessNameError("板块命名需要 LLM 客户端")

    obj_by_name = {obj.name: obj for obj in object_types}

    for segment in segments:
        # 提取板块信息用于 LLM 命名
        members_by_degree = segment.members[:15]  # 取前 15 个成员
        member_names = [
            obj_by_name[name].display_name
            for name in members_by_degree
            if name in obj_by_name
        ]

        # 提取板块内高频关系动词
        relation_verbs = [
            rel.display_name
            for rel in relation_types
            if rel.source_object_type_name in segment.members
            and rel.target_object_type_name in segment.members
        ]
        top_verbs = sorted(set(relation_verbs))[:5]

        # 提取该板块连接的枢纽
        connected_hubs = set()
        for rel in relation_types:
            if rel.source_object_type_name in segment.members and rel.target_object_type_name in hub_nodes:
                connected_hubs.add(obj_by_name[rel.target_object_type_name].display_name)
            elif rel.target_object_type_name in segment.members and rel.source_object_type_name in hub_nodes:
                connected_hubs.add(obj_by_name[rel.source_object_type_name].display_name)

        # 构建提示词
        prompt = _build_segment_naming_prompt(
            member_names,
            top_verbs,
            list(connected_hubs)[:5],
            segment.machine_baseline or "",
        )
        checkpoint_key = chunk_key(prompt)
        if checkpoint is not None:
            cached = checkpoint.load(checkpoint_key)
            if cached:
                cached_display = str(cached.get("display_name", "")).strip()
                cached_name = str(cached.get("name", "")).strip()
                if cached_display and cached_name and cached_name.replace("_", "").isalnum():
                    segment.display_name = cached_display
                    segment.name = cached_name
                    segment.description = str(cached.get("description", "")).strip() or None
                    continue

        # 调用 LLM
        try:
            completion = await llm_client.chat.completions.create(
                model=model or "glm-4-plus",
                messages=[
                    {
                        "role": "system",
                        "content": "你是数据治理专家，擅长从业务对象列表中提炼业务板块的语义名称。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )

            result_text = (completion.choices[0].message.content or "{}").strip()
            if result_text.startswith("```"):
                lines = result_text.splitlines()
                result_text = "\n".join(lines[1:-1]).strip()
            result = json.loads(result_text)

            display_name = result.get("display_name", "").strip()
            name = result.get("name", "").strip()
            description = result.get("description", "").strip() or None

            # 验证命名合格性
            if not display_name or not name:
                raise MissingBusinessNameError(
                    f"板块命名不完整：display_name={display_name!r}, name={name!r}"
                )

            # 英文标识符验证
            if not name.replace("_", "").isalnum():
                raise MissingBusinessNameError(
                    f"板块英文标识符不合法（只能包含字母、数字和下划线）：{name!r}"
                )

            segment.display_name = display_name
            segment.name = name
            segment.description = description
            if checkpoint is not None:
                checkpoint.save(
                    checkpoint_key,
                    {
                        "display_name": display_name,
                        "name": name,
                        "description": description,
                    },
                )

        except Exception as e:
            logger.error("Failed to name segment: %s", e)
            raise MissingBusinessNameError(
                f"LLM 板块命名失败：{e}"
            ) from e


def _build_segment_naming_prompt(
    member_names: list[str],
    top_verbs: list[str],
    connected_hubs: list[str],
    machine_baseline: str,
) -> str:
    """构建板块命名的 LLM 提示词。"""
    members_str = "、".join(member_names)
    verbs_str = "、".join(top_verbs) if top_verbs else "（无）"
    hubs_str = "、".join(connected_hubs) if connected_hubs else "（无）"

    return f"""请为以下业务板块生成语义化的名称：

**板块成员**（按重要性排序）：
{members_str}

**板块内高频关系动词**：
{verbs_str}

**该板块连接的枢纽对象**：
{hubs_str}

**机械名（仅供参考）**：
{machine_baseline}

请返回 JSON 格式，包含以下字段：
- display_name: 中文业务名称（2-8 个字，体现业务含义）
- name: 英文标识符（小写蛇形命名，如 financial_management）
- description: 简短描述（可选，20-50 字）

示例：
```json
{{
    "display_name": "财务管理",
    "name": "financial_management",
    "description": "包含成本中心、报价单、固定资产、商机、供应商报价等财务相关对象"
}}
```

要求：
1. display_name 必须准确反映板块的业务含义，不要使用机械名
2. name 必须是有效的英文标识符（只能包含字母、数字和下划线）
3. 优先考虑成员的语义和高频关系动词，而非单纯按字面意思命名
"""


def dedupe_segment_names(segments: list[DraftSegment]) -> None:
    """去重板块名称（就地修改）。

    与 object_naming.dedupe_object_names 同理：同名板块添加数字后缀。
    """
    name_counts: dict[str, int] = {}
    display_name_counts: dict[str, int] = {}

    for segment in segments:
        # 英文标识符去重
        if segment.name in name_counts:
            name_counts[segment.name] += 1
            segment.name = f"{segment.name}_{name_counts[segment.name]}"
        else:
            name_counts[segment.name] = 0

        # 中文名去重
        if segment.display_name in display_name_counts:
            display_name_counts[segment.display_name] += 1
            segment.display_name = (
                f"{segment.display_name}{display_name_counts[segment.display_name]}"
            )
        else:
            display_name_counts[segment.display_name] = 0
