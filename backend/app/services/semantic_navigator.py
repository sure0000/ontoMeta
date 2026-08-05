"""语义导航器（P1.2）：在已发布本体的关系图上找出**可用的 JOIN 路径**。

**为什么存在**：Agent 想关联两个对象时，只能靠猜外键列名写 JOIN，然后被
``sql_soundness`` 以 ``undeclared_join`` / ``unknown_column`` 拒掉——语义层明明
知道两者怎么连（关系、基数、外键证据都在本体里），却只在事后否决，不在事前给答案。
这正是 DATA_AGENT_V2_PLAN §0 说的「语义层只当否决者，不当生成器」。

本模块把这份知识变成可调用的能力：给两个对象，返回**多跳路径 + 每段 ON 条件 +
基数链 + 扇出风险 + 安全聚合建议**。

**与证明器的硬约束**：两者吃同一份 ``OntologyProjection``，多重性换算共用
``ontology_projection.other_is_many``，JOIN 键共用 ``foreign_key_names``。
否则会出现「导航器说能连、证明器说不能连」的自相矛盾——那比不给路径更糟。

保守取向沿用全链路：推不出 ON 就说推不出，基数未知一律按扇出算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.services.ontology_projection import (
    ObjView,
    OntologyProjection,
    RelView,
    other_is_many,
)

# 默认最大跳数。3 跳已能覆盖「事实表→维度→维度」这类常见形态；
# 再深路径的业务含义通常已不可靠，宁可不给。
DEFAULT_MAX_HOPS = 3
# 同一对象对最多返回几条路径：多了模型选不动，也撑上下文。
DEFAULT_PATH_LIMIT = 3

# 扇出下仍然安全的聚合（不受行重复影响）
_SAFE_AGGS_UNDER_FANOUT = ["COUNT(DISTINCT …)", "MIN", "MAX"]


@dataclass(frozen=True)
class JoinHop:
    """路径上的一段：从 ``from_obj`` 连到 ``to_obj``。"""

    relation: str
    relation_display: str
    from_obj: str
    to_obj: str
    cardinality: str | None
    structure_type: str | None
    on: str | None = None          # "order.customer_id = customer.id"；推不出为 None
    # ON 两端的列名（本体属性名）。`on` 是给人和模型看的渲染串，
    # 这两个是给**编译器**用的——口径编译器要拿它拼 sqlglot 表达式，不能去解析字符串。
    from_key: str | None = None
    to_key: str | None = None
    bridge_obj: str | None = None  # N:N 必经的桥接对象
    note: str | None = None

    def to_dict(self) -> dict:
        d = {
            "relation": self.relation,
            "relation_display": self.relation_display,
            "from": self.from_obj,
            "to": self.to_obj,
            "cardinality": self.cardinality,
            "on": self.on,
        }
        if self.bridge_obj:
            d["bridge_object"] = self.bridge_obj
        if self.note:
            d["note"] = self.note
        return d


@dataclass(frozen=True)
class JoinPath:
    """一条完整的关联路径。"""

    objects: list[str]
    hops: list[JoinHop]
    fanout_risk: str | None = None       # None 表示对 measure_object 而言不扇出
    measure_object: str | None = None    # 扇出是「相对谁」判定的
    safe_aggs: list[str] = field(default_factory=list)
    joinable: bool = True                # 每段 ON 都推得出才为 True

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    def sql_hint(self) -> str | None:
        """给模型的 FROM/JOIN 片段。任一段缺 ON 就不给——半截 SQL 只会误导。"""
        if not self.joinable or not self.hops:
            return None
        parts = [self.objects[0]]
        for hop in self.hops:
            parts.append(f"JOIN {hop.to_obj} ON {hop.on}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "objects": self.objects,
            "hops": [h.to_dict() for h in self.hops],
            "hop_count": self.hop_count,
            "cardinality_chain": [h.cardinality for h in self.hops],
            "joinable": self.joinable,
            "sql_hint": self.sql_hint(),
            "fanout_risk": self.fanout_risk,
            "measure_object": self.measure_object,
            "safe_aggs": self.safe_aggs,
        }


def _adjacency(proj: OntologyProjection) -> dict[str, list[tuple[str, RelView]]]:
    """对象 name(小写) -> [(邻居 name 小写, 关系)]。自反关系不入边（不用于寻路）。"""
    adj: dict[str, list[tuple[str, RelView]]] = {}
    for pair, rels in proj.relations_by_pair.items():
        nodes = list(pair)
        if len(nodes) != 2:
            continue  # 自反关系：JOIN 自身不构成通路
        a, b = nodes
        for rel in rels:
            adj.setdefault(a, []).append((b, rel))
            adj.setdefault(b, []).append((a, rel))
    return adj


def _hop_of(rel: RelView, near: ObjView, far: ObjView) -> JoinHop:
    """把一条关系渲染成「从 near 走到 far」的一段，ON 按方向摆正。"""
    # RelView 的 src_key/tgt_key 是按 src_obj/tgt_obj 存的，遍历方向可能相反
    if near.name == rel.src_obj:
        near_key, far_key = rel.src_key, rel.tgt_key
    else:
        near_key, far_key = rel.tgt_key, rel.src_key

    on = None
    note = None
    if near_key and far_key:
        on = f"{near.name}.{near_key} = {far.name}.{far_key}"
    else:
        note = "本体中该关系未记录可用的外键字段，无法给出 ON 条件"

    if rel.bridge_obj:
        note = (
            f"该关系经桥接对象「{rel.bridge_obj}」实现，"
            "不可直连两端——请分两段 JOIN 经过桥接对象"
        )
        on = None

    return JoinHop(
        relation=rel.name,
        relation_display=rel.display_name,
        from_obj=near.name,
        to_obj=far.name,
        cardinality=rel.cardinality.value if rel.cardinality else None,
        structure_type=rel.structure_type,
        on=on,
        from_key=near_key if on else None,
        to_key=far_key if on else None,
        bridge_obj=rel.bridge_obj,
        note=note,
    )


def _fanout_of(
    hops: list[JoinHop], rels: list[RelView], proj: OntologyProjection, measure: ObjView
) -> str | None:
    """沿路径从度量对象往外走，判断行是否会被放大。

    与 ``sql_soundness._fanout_reason`` 同源：逐段问「从本端看另一端是否为多」，
    任一段为多、或基数未知，即判扇出（保守）。
    """
    near = measure
    for hop, rel in zip(hops, rels):
        if rel.cardinality is not None and rel.cardinality.value == "many_to_many":
            return f"「{hop.from_obj}」↔「{hop.to_obj}」为多对多，会放大行"
        many = other_is_many(rel, near)
        if many is None:
            return f"「{hop.from_obj}」↔「{hop.to_obj}」基数未知，无法保证不放大行"
        if many:
            return (
                f"沿「{hop.from_obj}」→「{hop.to_obj}」展开（{hop.cardinality}），"
                f"「{measure.display_name}」的行会被重复计数"
            )
        nxt = proj.object_of(hop.to_obj)
        if nxt is None:
            return "路径中存在无法解析的对象"
        near = nxt
    return None


def find_join_path(
    proj: OntologyProjection,
    from_obj: str,
    to_obj: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    limit: int = DEFAULT_PATH_LIMIT,
    measure_object: str | None = None,
) -> list[JoinPath]:
    """在已发布本体的关系图上找 ``from_obj`` 到 ``to_obj`` 的关联路径。

    BFS 保证先短后长；同一对象对最多返回 ``limit`` 条。找不到返回空列表——
    这**不是**错误，而是「本体中这两个对象确实无从关联」这一事实本身。

    ``measure_object`` 指定扇出是相对谁判定的（默认起点）：问「订单金额按客户区域
    汇总」时度量在订单，问「客户数按订单状态」时度量在客户，两者的安全性不同。
    """
    src = proj.object_of(from_obj)
    tgt = proj.object_of(to_obj)
    if src is None or tgt is None or src.name == tgt.name:
        return []

    measure = proj.object_of(measure_object) if measure_object else src
    if measure is None:
        measure = src

    adj = _adjacency(proj)
    start, goal = src.name.strip().lower(), tgt.name.strip().lower()

    results: list[JoinPath] = []
    # 队列元素：(当前节点, 已访问节点集, 已走的关系链)
    queue: deque[tuple[str, list[str], list[RelView]]] = deque([(start, [start], [])])
    while queue and len(results) < limit:
        node, visited, rel_chain = queue.popleft()
        if len(rel_chain) >= max_hops:
            continue
        for neighbor, rel in adj.get(node, []):
            if neighbor in visited:
                continue  # 不绕环：同一对象在一条路径里只出现一次
            chain = rel_chain + [rel]
            path_nodes = visited + [neighbor]
            if neighbor == goal:
                results.append(_build_path(proj, path_nodes, chain, measure))
                if len(results) >= limit:
                    break
            else:
                queue.append((neighbor, path_nodes, chain))
    return results


def _build_path(
    proj: OntologyProjection, node_keys: list[str], rels: list[RelView], measure: ObjView
) -> JoinPath:
    objs = [proj.object_of(k) for k in node_keys]
    hops: list[JoinHop] = []
    for i, rel in enumerate(rels):
        near, far = objs[i], objs[i + 1]
        if near is None or far is None:
            continue
        hops.append(_hop_of(rel, near, far))

    joinable = bool(hops) and all(h.on for h in hops)
    fanout = _fanout_of(hops, rels, proj, measure)
    return JoinPath(
        objects=[o.name for o in objs if o is not None],
        hops=hops,
        fanout_risk=fanout,
        measure_object=measure.name,
        safe_aggs=list(_SAFE_AGGS_UNDER_FANOUT) if fanout else [],
        joinable=joinable,
    )


def describe_paths(paths: list[JoinPath]) -> list[dict]:
    """路径列表 → 可回灌给模型的紧凑 dict 列表。"""
    return [p.to_dict() for p in paths]


__all__ = [
    "JoinHop",
    "JoinPath",
    "find_join_path",
    "describe_paths",
    "DEFAULT_MAX_HOPS",
    "DEFAULT_PATH_LIMIT",
]
