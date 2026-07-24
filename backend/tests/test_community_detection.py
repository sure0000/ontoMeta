"""社区检测（域层级图谱聚类）单元测试。"""

from __future__ import annotations

from app.services.community_detection import (
    compute_graph_layout,
    identify_hub_nodes,
    label_propagation_clusters,
    name_cluster,
    split_dominant_clusters,
)


def _undirected(pairs: list[tuple[str, str]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for a, b in pairs:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    return adjacency


def test_label_propagation_separates_disconnected_components():
    adjacency = _undirected([("a", "b"), ("b", "c"), ("d", "e")])
    adjacency.setdefault("f", set())
    clusters = label_propagation_clusters(list(adjacency.keys()), adjacency)
    grouped = {frozenset(c) for c in clusters}
    assert frozenset({"a", "b", "c"}) in grouped
    assert frozenset({"d", "e"}) in grouped
    assert frozenset({"f"}) in grouped


def test_label_propagation_ignores_self_loops():
    adjacency = {"a": {"a", "b"}, "b": {"a"}}
    clusters = label_propagation_clusters(["a", "b"], adjacency)
    assert len(clusters) == 1
    assert clusters[0] == {"a", "b"}


def test_label_propagation_is_deterministic():
    adjacency = _undirected(
        [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e"), ("e", "f"), ("f", "d")]
    )
    node_ids = list(adjacency.keys())
    first = label_propagation_clusters(node_ids, adjacency)
    for _ in range(5):
        again = label_propagation_clusters(node_ids, adjacency)
        assert {frozenset(c) for c in again} == {frozenset(c) for c in first}


def test_split_dominant_clusters_splits_disconnected_component_mistakenly_merged():
    # 两个内部稠密但彼此完全不相连的团，模拟上一步聚类错误地把它们合并成了一个"巨簇"。
    left = {f"l{i}" for i in range(6)}
    right = {f"r{i}" for i in range(6)}
    pairs = [(f"l{i}", f"l{j}") for i in range(6) for j in range(i + 1, 6)]
    pairs += [(f"r{i}", f"r{j}") for i in range(6) for j in range(i + 1, 6)]
    adjacency = _undirected(pairs)
    all_ids = list(left | right)
    dominant_cluster = set(all_ids)

    result = split_dominant_clusters(
        [dominant_cluster], adjacency, max_cluster_nodes=2, total_clustered=len(all_ids)
    )
    sizes = sorted(len(c) for c in result)
    assert sizes == [6, 6]


def test_split_dominant_clusters_leaves_small_clusters_untouched():
    adjacency = _undirected([("a", "b"), ("b", "c")])
    clusters = [{"a", "b", "c"}]
    result = split_dominant_clusters(
        clusters, adjacency, max_cluster_nodes=50, total_clustered=3
    )
    assert result == clusters


def test_identify_hub_nodes_picks_high_degree_outliers():
    # 一个枢纽连接了几乎所有节点，其余节点度数很低
    pairs = [("hub", f"n{i}") for i in range(30)]
    pairs += [("n0", "n1"), ("n1", "n2")]
    adjacency = _undirected(pairs)
    hubs = identify_hub_nodes(adjacency, max_hub_count=5)
    assert "hub" in hubs
    assert "n0" not in hubs


def test_identify_hub_nodes_empty_when_no_outliers():
    # 所有节点度数相近（一个环），不应识别出枢纽
    ids = [f"n{i}" for i in range(10)]
    pairs = [(ids[i], ids[(i + 1) % 10]) for i in range(10)]
    adjacency = _undirected(pairs)
    hubs = identify_hub_nodes(adjacency, max_hub_count=5)
    assert hubs == set()


class _Obj:
    def __init__(self, name: str, display_name: str):
        self.name = name
        self.display_name = display_name


def test_name_cluster_small_cluster_joins_top_two_by_degree():
    adjacency = _undirected([("a", "b"), ("a", "c")])
    obj_by_id = {
        "a": _Obj("customer", "客户"),
        "b": _Obj("order", "订单"),
        "c": _Obj("invoice", "发票"),
    }
    name = name_cluster({"a", "b", "c"}, obj_by_id, adjacency)
    assert "客户" in name


def test_name_cluster_large_cluster_uses_common_prefix():
    ids = [f"dim_{i}" for i in range(6)]
    adjacency = _undirected([(ids[i], ids[i + 1]) for i in range(5)])
    obj_by_id = {nid: _Obj(f"dim_{part}", f"维度{part}") for nid, part in zip(ids, "abcdef")}
    name = name_cluster(set(ids), obj_by_id, adjacency)
    assert name == "Dim Group"


def test_name_cluster_empty_returns_fallback():
    assert name_cluster(set(), {}, {}) == "Group"


def test_compute_graph_layout_is_deterministic():
    node_ids = [f"n{i}" for i in range(12)]
    edges = [("n0", "n1", 3.0), ("n1", "n2", 1.0), ("n3", "n4", 2.0), ("n5", "n0", 1.0)]
    first = compute_graph_layout(node_ids, edges)
    for _ in range(3):
        again = compute_graph_layout(node_ids, edges)
        assert again == first
    # 每个节点都拿到坐标，且互不重合
    assert set(first.keys()) == set(node_ids)
    coords = list(first.values())
    assert len(set(coords)) == len(coords)


def test_compute_graph_layout_edge_cases():
    assert compute_graph_layout([], []) == {}
    assert compute_graph_layout(["solo"], []) == {"solo": (0.0, 0.0)}


def test_compute_graph_layout_sizes_prevent_overlap():
    # 给定较大的展开半径，任意两节点最终距离都不应小于半径之和（去重叠）。
    node_ids = [f"n{i}" for i in range(10)]
    edges = [("n0", "n1", 1.0), ("n2", "n3", 1.0)]
    sizes = {nid: 1.2 for nid in node_ids}
    pos = compute_graph_layout(node_ids, edges, sizes=sizes)
    for i, a in enumerate(node_ids):
        for b in node_ids[i + 1 :]:
            dist = ((pos[a][0] - pos[b][0]) ** 2 + (pos[a][1] - pos[b][1]) ** 2) ** 0.5
            assert dist >= sizes[a] + sizes[b] - 1e-6


def test_compute_graph_layout_connected_nodes_are_closer():
    # 一对强关联节点应比一对无关联节点靠得更近
    node_ids = ["a", "b", "c", "d"]
    edges = [("a", "b", 5.0)]
    pos = compute_graph_layout(node_ids, edges)

    def dist(p, q):
        return ((pos[p][0] - pos[q][0]) ** 2 + (pos[p][1] - pos[q][1]) ** 2) ** 0.5

    assert dist("a", "b") < dist("c", "d")
