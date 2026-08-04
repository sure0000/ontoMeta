"""发布时的形式化不变式校验（F2）：让已发布本体「可推理、可作证」。

F3（SQL 语义证明）依赖**基数可信**、F4（口径校验）依赖**AST 可解析**——这些前提由本
模块在**发布时**把关。发布是本体从「草稿」变为「语义权威」的时刻，故形式化不变式在此
强制：不满足 error 级不变式的本体，在 ``formal_enforcement=error`` 下不得发布。

与 ``draft_consistency.validate_ontology``（引用完整性）互补：那里查「关系端点是否存在」，
这里查更强的形式化性质——派生关系无环、指标口径可解析、聚合语义自洽、基数良定义。

设计见 FORMAL_VALIDATION_IMPL.md 第四部分。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    EntityStatus,
    ObjectType,
    Property,
    RelationType,
)
from app.ontology_types import (
    SemanticType,
    normalize_cardinality,
    normalize_semantic_type,
)

# 业务关联关系（需要基数良定义）；derivation 是溯源/血缘，不参与基数/扇出语义。
_BUSINESS_STRUCTURE_TYPES = {"foreign_key", "bridge_table", "fact_table"}
# 会因扇出翻倍的聚合：与 sql_soundness 保持一致。
_FANOUT_SENSITIVE_OPS = {"sum", "avg"}


@dataclass
class FormalIssue:
    code: str
    message: str
    severity: str  # "error" | "warning"
    entity_type: str
    entity_id: str | None = None
    entity_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class FormalValidationError(ValueError):
    """形式化不变式校验失败（error 级），阻断发布。"""

    def __init__(self, issues: list[FormalIssue]) -> None:
        self.issues = issues
        summary = "; ".join(i.message for i in issues[:5])
        if len(issues) > 5:
            summary += f"；另有 {len(issues) - 5} 项"
        super().__init__(f"形式化校验失败（{len(issues)}）: {summary}")


def check_formal_invariants(db: Session, ontology_id: str) -> list[FormalIssue]:
    """跑一遍全部形式化不变式，返回问题清单（error + warning 混合）。只读、无副作用。"""
    issues: list[FormalIssue] = []
    issues.extend(_check_derivation_acyclic(db, ontology_id))
    issues.extend(_check_metric_ast_resolvable(db, ontology_id))
    issues.extend(_check_semantic_type_coherence(db, ontology_id))
    issues.extend(_check_business_relation_cardinality(db, ontology_id))
    return issues


def assert_publishable(
    db: Session, ontology_id: str, enforcement: str
) -> list[FormalIssue]:
    """发布前调用。``enforcement`` ∈ {off, warn, error}。

    error 级不变式在 ``enforcement=error`` 下抛 FormalValidationError 阻断发布；
    否则仅返回问题清单（供展示/日志），不阻断。返回全部问题（含 warning）。
    """
    if enforcement == "off":
        return []
    issues = check_formal_invariants(db, ontology_id)
    if enforcement == "error":
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise FormalValidationError(errors)
    return issues


# ---------------------------------------------------------------------------
# 不变式实现
# ---------------------------------------------------------------------------
def _check_derivation_acyclic(db: Session, ontology_id: str) -> list[FormalIssue]:
    """σ=derivation 的边构成的有向图必须无环（PROV-O：wasDerivedFrom 不可成环）。

    环意味着「A 由 B 派生、B 又由 A 派生」——溯源自相矛盾，物化时会死循环。
    用 DFS 三色标记找回边（back edge）定位环。
    """
    relations = (
        db.query(RelationType)
        .filter(
            RelationType.ontology_id == ontology_id,
            RelationType.structure_type == "derivation",
        )
        .all()
    )
    if not relations:
        return []

    # 邻接表：source -> {target}
    graph: dict[str, set[str]] = {}
    for r in relations:
        graph.setdefault(r.source_object_type_id, set()).add(r.target_object_type_id)
        graph.setdefault(r.target_object_type_id, set())

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in graph}
    cycle_nodes: set[str] = set()

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, ()):  # noqa: SIM118
            if color.get(nxt, WHITE) == GRAY:
                # 回边：nxt..node 构成环
                if nxt in stack:
                    cycle_nodes.update(stack[stack.index(nxt):])
            elif color.get(nxt, WHITE) == WHITE:
                dfs(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for n in list(graph):
        if color[n] == WHITE:
            dfs(n, [])

    if not cycle_nodes:
        return []
    names = {
        o.id: o.display_name
        for o in db.query(ObjectType).filter(ObjectType.id.in_(list(cycle_nodes))).all()
    }
    return [
        FormalIssue(
            code="derivation_cycle",
            message=(
                "派生/血缘关系存在环："
                + " → ".join(names.get(n, n) for n in list(cycle_nodes)[:6])
                + "（溯源不可自相派生，请检查血缘方向）"
            ),
            severity="error",
            entity_type="ontology",
            entity_id=ontology_id,
        )
    ]


def _check_metric_ast_resolvable(db: Session, ontology_id: str) -> list[FormalIssue]:
    """指标的 expression_json 必须能解析，且所有 {"ref": rN} 都在 refs[] 里、
    其 object_type_id/property_id 指向本本体内的实体。

    无 expression_json 的逻辑（仅有文本摘要）跳过——不是所有口径都已结构化。
    """
    logics = (
        db.query(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology_id)
        .all()
    )
    issues: list[FormalIssue] = []
    # 本本体内的合法 id 集合
    obj_ids = {
        o.id for o in db.query(ObjectType.id).filter(ObjectType.ontology_id == ontology_id)
    }
    prop_ids = {
        p.id
        for p in db.query(Property.id)
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
    }

    for logic in logics:
        raw = logic.expression_json
        if not raw:
            continue
        try:
            ast = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            issues.append(
                FormalIssue(
                    code="metric_ast_unparseable",
                    message=f"业务逻辑「{logic.display_name}」的表达式无法解析为结构化口径",
                    severity="error",
                    entity_type="business_logic",
                    entity_id=logic.id,
                    entity_name=logic.name,
                )
            )
            continue
        if not isinstance(ast, dict):
            continue

        refs = {r.get("ref_id"): r for r in (ast.get("refs") or []) if isinstance(r, dict)}
        used = _collect_ref_ids(ast.get("body"))
        for rid in used:
            ref = refs.get(rid)
            if ref is None:
                issues.append(
                    FormalIssue(
                        code="metric_ref_dangling",
                        message=f"业务逻辑「{logic.display_name}」引用了未声明的口径变量 {rid}",
                        severity="error",
                        entity_type="business_logic",
                        entity_id=logic.id,
                        entity_name=logic.name,
                    )
                )
                continue
            oid = ref.get("object_type_id")
            pid = ref.get("property_id")
            if oid and oid not in obj_ids:
                issues.append(
                    FormalIssue(
                        code="metric_ref_unresolved",
                        message=f"业务逻辑「{logic.display_name}」口径引用的对象不在本体内",
                        severity="error",
                        entity_type="business_logic",
                        entity_id=logic.id,
                        entity_name=logic.name,
                    )
                )
            if pid and pid not in prop_ids:
                issues.append(
                    FormalIssue(
                        code="metric_ref_unresolved",
                        message=f"业务逻辑「{logic.display_name}」口径引用的字段不在本体内",
                        severity="error",
                        entity_type="business_logic",
                        entity_id=logic.id,
                        entity_name=logic.name,
                    )
                )
    return issues


def _check_semantic_type_coherence(db: Session, ontology_id: str) -> list[FormalIssue]:
    """被指标 SUM/AVG 的字段，其 semantic_type 必须是 measure。

    否则 F3 会在查询时拒绝对它聚合，指标形同虚设。迁移期 semantic_type 多为 unknown，
    故列 warning（提示补全）而非 error，避免一刀切阻断存量发布。
    """
    logics = (
        db.query(BusinessLogic)
        .filter(BusinessLogic.ontology_id == ontology_id)
        .all()
    )
    prop_by_id = {
        p.id: p
        for p in db.query(Property)
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    }
    issues: list[FormalIssue] = []
    for logic in logics:
        raw = logic.expression_json
        if not raw:
            continue
        try:
            ast = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(ast, dict):
            continue
        body = ast.get("body") or {}
        if not isinstance(body, dict):
            continue
        if (body.get("operation") or "").lower() not in _FANOUT_SENSITIVE_OPS:
            continue
        refs = {r.get("ref_id"): r for r in (ast.get("refs") or []) if isinstance(r, dict)}
        for rid in _collect_ref_ids(body.get("args")):
            ref = refs.get(rid)
            if not ref:
                continue
            prop = prop_by_id.get(ref.get("property_id"))
            if prop is None:
                continue
            st = normalize_semantic_type(prop.semantic_type)
            if st is not SemanticType.MEASURE:
                issues.append(
                    FormalIssue(
                        code="aggregation_non_measure",
                        message=(
                            f"业务逻辑「{logic.display_name}」对字段「{prop.display_name}」"
                            f"做{body.get('operation','').upper()}，但其语义类型为 {st.value}"
                            "（应为 measure，否则查询时会被拒绝聚合）"
                        ),
                        severity="warning",
                        entity_type="business_logic",
                        entity_id=logic.id,
                        entity_name=logic.name,
                    )
                )
    return issues


def _check_business_relation_cardinality(
    db: Session, ontology_id: str
) -> list[FormalIssue]:
    """业务关联关系（外键/桥表/事实表）应有可识别的基数。

    基数缺失/无法归一 → F3 扇出判定会保守拒绝任何跨它的聚合查询。故列 warning 提示补全。
    """
    relations = (
        db.query(RelationType)
        .filter(
            RelationType.ontology_id == ontology_id,
            RelationType.status != EntityStatus.DEPRECATED.value,
        )
        .all()
    )
    issues: list[FormalIssue] = []
    for r in relations:
        if (r.structure_type or "") not in _BUSINESS_STRUCTURE_TYPES:
            continue
        if normalize_cardinality(r.cardinality) is None:
            issues.append(
                FormalIssue(
                    code="cardinality_undefined",
                    message=(
                        f"业务关系「{r.display_name}」的基数未定义或无法识别"
                        f"（当前值：{r.cardinality!r}），跨它的聚合查询会被保守拒绝"
                    ),
                    severity="warning",
                    entity_type="relation_type",
                    entity_id=r.id,
                    entity_name=r.name,
                )
            )
    return issues


def _collect_ref_ids(node) -> set[str]:
    """递归收集 AST 节点里所有 {"ref": "rN"} 的 rN。"""
    found: set[str] = set()

    def walk(n) -> None:
        if isinstance(n, dict):
            if "ref" in n and isinstance(n["ref"], str):
                found.add(n["ref"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return found


__all__ = [
    "FormalIssue",
    "FormalValidationError",
    "check_formal_invariants",
    "assert_publishable",
]
