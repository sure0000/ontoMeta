"""社区检测：将 ObjectType 按关系紧密度自动聚类为业务子域。

纯 Python 实现的标签传播（Label Propagation），不依赖 networkx 等第三方图库。
"""

import hashlib
import math
import statistics
from collections import Counter

_MAX_ITERATIONS = 10


def _stable_rank(label: str) -> str:
    """确定性的平局裁断依据：与进程无关的稳定 hash（Python str hash 逐进程随机）。"""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def label_propagation_clusters(
    node_ids: list[str], adjacency: dict[str, set[str]]
) -> list[set[str]]:
    """标签传播聚类。

    - 迭代上限 `_MAX_ITERATIONS` 轮，标签不再变化时提前收敛。
    - 每轮按 node_ids 排序后的确定顺序异步更新（同轮内已更新的标签立即可见）。
    - 平局（多个标签票数相同）按标签的稳定 hash 裁断，保证跨进程结果一致。
    - 自环（node 是自己的邻居）会被忽略，不参与投票。
    """
    if not node_ids:
        return []

    ordered_ids = sorted(node_ids)
    labels: dict[str, str] = {nid: nid for nid in ordered_ids}

    for _ in range(_MAX_ITERATIONS):
        changed = False
        for nid in ordered_ids:
            neighbors = [n for n in adjacency.get(nid, ()) if n != nid and n in labels]
            if not neighbors:
                continue
            votes = Counter(labels[n] for n in neighbors)
            top_count = max(votes.values())
            candidates = sorted(
                (label for label, count in votes.items() if count == top_count),
                key=_stable_rank,
            )
            new_label = candidates[0]
            if new_label != labels[nid]:
                labels[nid] = new_label
                changed = True
        if not changed:
            break

    clusters: dict[str, set[str]] = {}
    for nid, label in labels.items():
        clusters.setdefault(label, set()).add(nid)
    return list(clusters.values())


_MAX_SPLIT_DEPTH = 3


def split_dominant_clusters(
    clusters: list[set[str]],
    adjacency: dict[str, set[str]],
    max_cluster_nodes: int,
    total_clustered: int,
    _depth: int = 0,
) -> list[set[str]]:
    """标签传播在稠密图上容易把大部分节点收敛成一个"巨簇"，掩盖业务结构。

    对明显主导全图的簇（远超正常聚类规模），在其内部诱导子图上重跑一次标签传播，
    尝试拆出更细的子域。递归深度上限 `_MAX_SPLIT_DEPTH`，避免在无法进一步拆分的
    稠密子图上（如近似完全图）无限递归。
    """
    if _depth >= _MAX_SPLIT_DEPTH or total_clustered == 0:
        return clusters

    dominant_threshold = max(max_cluster_nodes * 2, int(total_clustered * 0.3))
    result: list[set[str]] = []
    changed = False
    for cluster in clusters:
        if len(cluster) > dominant_threshold:
            sub_adjacency = {
                nid: {n for n in adjacency.get(nid, ()) if n in cluster} for nid in cluster
            }
            sub_clusters = label_propagation_clusters(sorted(cluster), sub_adjacency)
            if len(sub_clusters) > 1:
                changed = True
                result.extend(sub_clusters)
                continue
        result.append(cluster)

    if not changed:
        return result
    return split_dominant_clusters(
        result, adjacency, max_cluster_nodes, total_clustered, _depth + 1
    )


def identify_hub_nodes(adjacency: dict[str, set[str]], max_hub_count: int) -> set[str]:
    """识别度数远高于平均水平的枢纽节点（如"公司""文档类型"这类几乎处处被引用的公共维度表）。

    枢纽节点若参与常规聚类，会因为连接过于稠密把大部分节点传递闭包般收敛成一个
    "巨簇"，掩盖真实的业务子域结构。聚类前将其临时摘除，聚类后再作为独立的单节点簇
    展示——这天然会与很多其他簇产生聚合边，恰好如实体现其"公共枢纽"的角色。
    """
    degrees = {n: len(v) for n, v in adjacency.items() if v}
    if not degrees or max_hub_count <= 0:
        return set()
    mean_degree = statistics.mean(degrees.values())
    floor = max(15, mean_degree * 3)
    candidates = sorted(
        (n for n, d in degrees.items() if d >= floor),
        key=lambda n: (-degrees[n], n),
    )
    return set(candidates[:max_hub_count])


def _degree(node_id: str, adjacency: dict[str, set[str]]) -> int:
    return len({n for n in adjacency.get(node_id, ()) if n != node_id})


def _common_prefix_tokens(names: list[str]) -> list[str]:
    """按 `_` 切分后求所有名称共享的前缀 token 序列。"""
    if not names:
        return []
    token_lists = [name.split("_") for name in names]
    shortest = min(len(t) for t in token_lists)
    prefix: list[str] = []
    for i in range(shortest):
        token = token_lists[0][i]
        if all(t[i] == token for t in token_lists):
            prefix.append(token)
        else:
            break
    return prefix


# 力导向布局：节点数超过此上限时跳过 O(n^2) 力模拟，退化为纯 phyllotaxis 排布，
# 保证任意规模都能秒级返回（域概览通常只有几十~上百个聚类/枢纽，极少触及此上限）。
_LAYOUT_FORCE_MAX_NODES = 400


def compute_graph_layout(
    node_ids: list[str],
    edges: list[tuple[str, str, float]],
    *,
    sizes: dict[str, float] | None = None,
    iterations: int | None = None,
) -> dict[str, tuple[float, float]]:
    """为宏观图（聚类 + 枢纽构成的"超节点"图）计算**确定性**的二维坐标。

    数字孪生式的域概览最需要的是"空间记忆"：同一份数据每次打开，每个业务版块都落在
    同一个位置。这里用一个自实现的 Fruchterman-Reingold 力导向布局：
    - 初始位置用 phyllotaxis（向日葵）确定性铺开，避免随机初值导致每次结果不同；
    - 固定迭代轮数 + 固定遍历顺序 + 无随机数 ⇒ 完全确定，跨进程一致；
    - 边按权重（关系条数）取 log 衰减后加强吸引，让强关联的版块彼此靠近。

    返回以质心为原点的坐标（近邻间距约为 1 个单位），前端按固定像素间距放大即可。
    """
    nodes = sorted(node_ids)
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]: (0.0, 0.0)}

    index = {nid: i for i, nid in enumerate(nodes)}
    golden = math.pi * (3.0 - math.sqrt(5.0))
    px = [0.0] * n
    py = [0.0] * n
    for i in range(n):
        r = math.sqrt(i + 0.5)
        theta = i * golden
        px[i] = r * math.cos(theta)
        py[i] = r * math.sin(theta)

    if n <= _LAYOUT_FORCE_MAX_NODES:
        # 聚合无向加权边（忽略自环与未知端点），排序保证遍历顺序确定
        weighted: dict[tuple[int, int], float] = {}
        for a, b, w in edges:
            ia = index.get(a)
            ib = index.get(b)
            if ia is None or ib is None or ia == ib:
                continue
            key = (ia, ib) if ia < ib else (ib, ia)
            weighted[key] = weighted.get(key, 0.0) + float(w)
        edge_items = sorted(weighted.items())

        k = 1.0
        # 向心引力：把每个节点朝质心拉一把（力度随离心距离线性增大）。
        # 没有它，纯 FR 里彼此不相连的版块只受斥力、无吸引，会被无限推远导致布局爆炸。
        gravity = 0.06
        if iterations is None:
            iterations = max(80, min(400, 20000 // n))
        temp = 0.1 * math.sqrt(n)
        cool = temp / iterations

        for _ in range(iterations):
            dx = [0.0] * n
            dy = [0.0] * n
            for i in range(n):
                xi = px[i]
                yi = py[i]
                for j in range(i + 1, n):
                    ddx = xi - px[j]
                    ddy = yi - py[j]
                    d2 = ddx * ddx + ddy * ddy
                    if d2 < 1e-9:
                        # 重合时按下标施加确定性微扰，避免除零且结果可复现
                        ddx = (i - j) * 1e-4 + 1e-4
                        ddy = (i + j) * 1e-4 + 1e-4
                        d2 = ddx * ddx + ddy * ddy
                    d = math.sqrt(d2)
                    f = (k * k) / d
                    fx = ddx / d * f
                    fy = ddy / d * f
                    dx[i] += fx
                    dy[i] += fy
                    dx[j] -= fx
                    dy[j] -= fy
            for (i, j), w in edge_items:
                ddx = px[i] - px[j]
                ddy = py[i] - py[j]
                d = math.sqrt(ddx * ddx + ddy * ddy) or 1e-6
                f = (d * d) / k * (1.0 + math.log1p(w))
                fx = ddx / d * f
                fy = ddy / d * f
                dx[i] -= fx
                dy[i] -= fy
                dx[j] += fx
                dy[j] += fy
            for i in range(n):
                dx[i] -= gravity * px[i]
                dy[i] -= gravity * py[i]
                d = math.sqrt(dx[i] * dx[i] + dy[i] * dy[i])
                if d > 1e-9:
                    cap = min(d, temp)
                    px[i] += dx[i] / d * cap
                    py[i] += dy[i] / d * cap
            temp = max(temp - cool, 1e-3)

    # 居中到质心。
    cx = sum(px) / n
    cy = sum(py) / n
    px = [x - cx for x in px]
    py = [y - cy for y in py]

    # 把离群点拉回。纯 FR 里"无跨版块关系"的孤立超节点只受斥力，会被推到很远，
    # 极大地撑大包围盒、导致 fitView 后整张图缩成一个点。这些点没有边，落在哪儿只是观感问题，
    # 于是沿其当前方向把半径截断到核心半径中位数的若干倍——既收住离群点，又不动布局良好的核心。
    radii = sorted(math.hypot(px[i], py[i]) for i in range(n))
    median_r = radii[n // 2] if radii else 0.0
    if median_r > 1e-9:
        cap = median_r * 4.0
        for i in range(n):
            r = math.hypot(px[i], py[i])
            if r > cap:
                scale = cap / r
                px[i] *= scale
                py[i] *= scale

    # 归一化尺度：缩放到"最近邻间距中位数 ≈ 1 个单位"，这样无论力度参数如何调，
    # 前端按固定像素间距放大都能得到疏密合适的地图。
    nearest: list[float] = []
    for i in range(n):
        best = math.inf
        for j in range(n):
            if i == j:
                continue
            d2 = (px[i] - px[j]) ** 2 + (py[i] - py[j]) ** 2
            if d2 < best:
                best = d2
        if best < math.inf:
            nearest.append(math.sqrt(best))
    if nearest:
        nearest.sort()
        median_nn = nearest[len(nearest) // 2]
        if median_nn > 1e-9:
            inv = 1.0 / median_nn
            px = [x * inv for x in px]
            py = [y * inv for y in py]

    # 尺寸感知的去重叠：大版块展开成节点网格时占地很大，若仅按点布局，相邻版块展开后会
    # 严重重叠。给定每个节点的"展开半径"后，做若干轮 collide 松弛——两节点距离小于半径之和
    # （含间距）就沿连线推开。小版块半径小、基本不动，大版块自动获得更多留白，天然形成
    # "面积∝规模"的地图，也保证放大展开时各版块网格互不打架。
    if sizes:
        radius = [sizes.get(nodes[i], 0.0) for i in range(n)]
        gap = 0.25
        for _ in range(60):
            moved = False
            for i in range(n):
                for j in range(i + 1, n):
                    ddx = px[i] - px[j]
                    ddy = py[i] - py[j]
                    d = math.hypot(ddx, ddy)
                    min_d = radius[i] + radius[j] + gap
                    if d < min_d:
                        if d < 1e-9:
                            ddx = (i - j) * 1e-3 + 1e-3
                            ddy = (i + j) * 1e-3 + 1e-3
                            d = math.hypot(ddx, ddy)
                        push = (min_d - d) / 2.0
                        ux = ddx / d
                        uy = ddy / d
                        px[i] += ux * push
                        py[i] += uy * push
                        px[j] -= ux * push
                        py[j] -= uy * push
                        moved = True
            if not moved:
                break

    return {nodes[i]: (px[i], py[i]) for i in range(n)}


def name_cluster(
    cluster_node_ids: set[str],
    obj_by_id: dict[str, object],
    adjacency: dict[str, set[str]],
) -> str:
    """为聚类生成语义名称。

    优先级：
    1) 大簇（>=5 节点）且各节点名有共同前缀 -> "{Prefix} Group"
    2) 小簇（<5 节点）-> 拼接度数最高的 Top-2 节点名
    3) 兜底 -> 度数最高的单个节点名
    """
    members = sorted(cluster_node_ids, key=lambda nid: _degree(nid, adjacency), reverse=True)
    if not members:
        return "Group"

    def display_name(nid: str) -> str:
        obj = obj_by_id.get(nid)
        return getattr(obj, "display_name", None) or getattr(obj, "name", None) or nid

    if len(members) >= 5:
        names = [getattr(obj_by_id.get(nid), "name", nid) or nid for nid in members]
        prefix_tokens = _common_prefix_tokens(names)
        if prefix_tokens:
            prefix = " ".join(t.capitalize() for t in prefix_tokens if t)
            if prefix:
                return f"{prefix} Group"

    if len(members) < 5:
        top = members[:2]
        names = [display_name(nid) for nid in top]
        return " / ".join(names)

    return display_name(members[0])
