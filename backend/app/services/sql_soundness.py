"""SQL 语义证明器：执行前静态证明一条 SQL 语义合法，否则拒绝。

**为什么存在**：Data Agent 会写出语法正确、但语义错误的 SQL——列不属于对象、
JOIN 两个本无关系的对象、沿多对多 JOIN 后 SUM 翻倍。这些错误 ``_apply_mapping``
的正则替换拦不住，跑进数据库要么报错（还好）、要么**静默返回错数**（最危险）。

本模块在 SQL 进数据库**之前**做纯静态证明：给定 SQL + 已发布本体投影，逐条验证
「表存在 / 列归属 / JOIN 有据 / 不扇出 / 聚合合法」。任一步不成立即拒绝，绝不放行
一条可能给出错误数值的查询。这把 execute_message_sql 注释里那句「准确性是架构保证
而非提示词保证」变成真代码。

保守原则贯穿全篇：拿不准（基数未知、解析不确定）一律判失败 → 拒答，绝不错答。

设计见 FORMAL_VALIDATION_IMPL.md 第二部分 §2.3。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from app.ontology_types import Cardinality, can_aggregate, can_group_by
from app.services.ontology_projection import (
    ObjView,
    OntologyProjection,
    RelView,
    other_is_many,
)


@dataclass
class SqlRejection:
    """SQL 未通过语义证明。``message`` 面向用户、可照做；``ok`` 恒 False。

    ``hint`` 是**给模型的修复信号**（P1.4）：拒绝结果本就作为 ``role:tool`` 回灌，
    只说「拒绝臆造字段」模型无从下手，附上候选字段/合法对端/安全改写后它下一步就能自修。
    守卫从守门员变成教练——这是把拒答率换成正确率的最低成本改动。
    """

    code: str
    message: str
    detail: dict = field(default_factory=dict)
    hint: dict = field(default_factory=dict)
    ok: bool = False


@dataclass
class SqlCertificate:
    """SQL 通过语义证明的凭证。``ok`` 恒 True。"""

    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    aggregations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True


# 聚合翻倍风险最高的两种：SUM / AVG（COUNT/MIN/MAX 受扇出影响小，先不拦）。
_FANOUT_SENSITIVE_AGGS = {"sum", "avg"}

# ---------------------------------------------------------------- P1.4 修复信号
# 候选清单条数上限：提示是给模型的「下一步怎么改」，不是把整张表塞回上下文。
_HINT_LIST_LIMIT = 20
_DID_YOU_MEAN_N = 3


def _did_you_mean(token: str, candidates: list[str]) -> list[str]:
    """按编辑距离挑最像的几个候选。空 token / 无候选时返回空列表。"""
    if not token or not candidates:
        return []
    # cutoff 放宽到 0.4：模型臆造的名字往往只对一半（order_amt vs amount）
    return difflib.get_close_matches(token, candidates, n=_DID_YOU_MEAN_N, cutoff=0.4)


def _columns_of(obj: ObjView) -> list[str]:
    return sorted(pv.name for pv in obj.props.values())


def _column_hint(obj: ObjView, col: str) -> dict:
    """未知列的提示：像哪几个 + 该对象全部合法列。"""
    available = _columns_of(obj)
    return {
        "object": obj.name,
        "did_you_mean": _did_you_mean(col, available),
        "available_columns": available[:_HINT_LIST_LIMIT],
        "available_truncated": len(available) > _HINT_LIST_LIMIT,
    }


def _semantic_columns(obj: ObjView, predicate) -> list[str]:
    return sorted(pv.name for pv in obj.props.values() if predicate(pv.semantic_type))


def _undeclared_join_hint(proj: OntologyProjection, a: ObjView, b: ObjView) -> dict:
    """两对象无直接关系时的修复信号。

    先问语义导航器有没有**多跳通路**——「订单和区域没直接关系，但可经客户relate」
    是可执行的修正；只回一句「无关系」模型只能放弃或继续臆造。
    导航器故障绝不能拖垮证明器：兜底退回对端清单。
    """
    hint: dict = {
        "fix": (
            "只能沿已声明的业务关系 JOIN。若下方给出了多跳路径，请照它改写；"
            "否则说明本体中缺该关系。"
        ),
    }
    try:
        from app.services.semantic_navigator import describe_paths, find_join_path

        paths = find_join_path(proj, a.name, b.name)
        if paths:
            hint["join_paths"] = describe_paths(paths)
            return hint
    except Exception:  # noqa: BLE001 — 提示是增强，不是证明的前提
        pass
    hint[f"partners_of_{a.name}"] = proj.partners_of(a.name)[:_HINT_LIST_LIMIT]
    hint[f"partners_of_{b.name}"] = proj.partners_of(b.name)[:_HINT_LIST_LIMIT]
    return hint


def _schema_from_projection(proj: OntologyProjection) -> dict:
    """给 sqlglot.qualify 的 schema：{对象 name: {列 name: 类型}}，用于解析裸列归属。"""
    schema: dict[str, dict[str, str]] = {}
    for obj in proj.objects.values():
        schema[obj.name] = {pv.name: "UNKNOWN" for pv in obj.props.values()}
    return schema


def prove_sql_sound(
    sql: str, proj: OntologyProjection, *, dialect: str | None = None
) -> SqlCertificate | SqlRejection:
    """静态证明 SQL 语义合法。返回 SqlCertificate（放行）或 SqlRejection（拒绝）。"""
    # 0) 解析 + 列作用域限定。qualify 把每个裸列绑定到其来源表（多表时必需）。
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception as exc:  # noqa: BLE001
        return SqlRejection("unparseable", f"SQL 无法解析，拒绝执行：{exc}", {"sql": sql})
    if tree is None:
        return SqlRejection("unparseable", "SQL 为空或无法解析，拒绝执行。", {"sql": sql})

    try:
        tree = qualify(
            tree,
            schema=_schema_from_projection(proj),
            qualify_columns=True,
            validate_qualify_columns=False,  # 未知列我们自己报更友好的错，不让 qualify 抛
            identify=False,
        )
    except Exception:
        # qualify 失败（如列歧义无法定位）——保守拒绝，不冒险放行歧义查询。
        return SqlRejection(
            "ambiguous",
            "SQL 存在无法确定归属的列（可能列名歧义或缺表限定），拒绝执行以免误判。",
            {"sql": sql},
            hint={"fix": "给每个字段加表别名限定（如 o.amount 而非 amount）后重试。"},
        )

    # 1) 表存在性：每个表 token 必须对应一个已发布业务对象
    alias_to_obj: dict[str, ObjView] = {}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        obj = proj.object_of(t.name)
        if obj is None:
            known = sorted(o.name for o in proj.objects.values())
            return SqlRejection(
                "unknown_table",
                f"表「{t.name}」不对应任何已发布业务对象，拒绝执行。",
                {"table": t.name},
                hint={
                    "did_you_mean": _did_you_mean(t.name, known),
                    "fix": "表名必须用本体对象标识符；可先调 search_objects 确认。",
                },
            )
        alias_to_obj[t.alias_or_name] = obj
        # 同一对象也允许用其本名限定（无别名时 alias_or_name == 表名）
        alias_to_obj.setdefault(obj.name, obj)
        tables.append(obj.name)

    if not alias_to_obj:
        return SqlRejection(
            "no_table", "查询未引用任何本体对象，拒绝执行。", {"sql": sql}
        )

    # 2) 列归属性：每个 (表.列) 必须是该对象的已发布属性
    #
    # 例外：**本查询自己的输出别名**。`qualify` 会把 `ORDER BY COUNT(*)` 归一成
    # `ORDER BY <别名>`，于是别名以裸列形态出现——不排除的话，
    # 「按金额降序取 TopN」这类最常见的查询会被误判成臆造字段而拒掉（真发生过）。
    # 安全性不受损：别名的**定义式**仍在 SELECT 列表里逐列证明，
    # `SELECT ghost AS x ... ORDER BY x` 依然会在 ghost 处被拒。
    # 只认**显式** `AS x` 定义的别名：裸列 `SELECT fake_col` 的输出名也叫 fake_col，
    # 若一并豁免，臆造字段就能靠「自己给自己当别名」蒙混过关。
    explicit_aliases = {
        (a.alias or "").strip().lower()
        for select in tree.find_all(exp.Select)
        for a in select.expressions
        if isinstance(a, exp.Alias)
    }
    col_obj: dict[int, ObjView] = {}  # id(Column) -> 所属对象
    columns: list[str] = []
    for c in tree.find_all(exp.Column):
        # 且只在 ORDER BY / HAVING / GROUP BY 里豁免——SQL 里也只有这些位置能引用别名。
        if (
            not c.table
            and (c.name or "").strip().lower() in explicit_aliases
            and _within(c, (exp.Order, exp.Having, exp.Group))
        ):
            continue
        table_key = c.table
        if not table_key:
            # 单表查询里 qualify 通常已补全；仍缺则单表可推断，多表则歧义拒绝
            if len(alias_to_obj) == 1:
                obj = next(iter(alias_to_obj.values()))
            else:
                # 哪些在场对象确实有这个列——直接告诉模型该加哪个限定
                owners = sorted(
                    {o.name for o in alias_to_obj.values() if o.resolve_property(c.name)}
                )
                return SqlRejection(
                    "ambiguous",
                    f"字段「{c.name}」未限定所属表且存在多表，拒绝执行以免误判。",
                    {"column": c.name},
                    hint={
                        "candidates": owners,
                        "fix": f"给「{c.name}」加表别名限定后重试。",
                    },
                )
        else:
            obj = alias_to_obj.get(table_key)
            if obj is None:
                return SqlRejection(
                    "unknown_table",
                    f"字段「{c.name}」引用了未知表别名「{table_key}」，拒绝执行。",
                    {"column": c.name, "table": table_key},
                    hint={
                        "known_aliases": sorted(alias_to_obj.keys()),
                        "fix": "别名必须在 FROM/JOIN 里先声明。",
                    },
                )
        pv = obj.resolve_property(c.name)
        if pv is None:
            return SqlRejection(
                "unknown_column",
                f"字段「{c.name}」不属于对象「{obj.display_name}」，拒绝臆造字段。",
                {"object": obj.name, "column": c.name},
                hint=_column_hint(obj, c.name),
            )
        col_obj[id(c)] = obj
        columns.append(f"{obj.name}.{pv.name}")

    # 3) JOIN 合法性 + 4) 扇出安全
    has_sensitive_agg = any(
        (a.key or "").lower() in _FANOUT_SENSITIVE_AGGS
        for a in tree.find_all(exp.AggFunc)
    )
    joins: list[str] = []
    for j in tree.find_all(exp.Join):
        pair = _objects_in_join(j, alias_to_obj)
        if len(pair) != 2:
            # 自连接或无法确定两端——保守拒绝（宁可拒答）
            return SqlRejection(
                "undeclared_join",
                "无法确定 JOIN 两端对应的业务对象，拒绝执行以免臆造关联。",
                {"join": j.sql()},
                hint={"fix": "ON 条件两侧各写一个已声明对象的限定列（a.x = b.y）。"},
            )
        a, b = pair
        rels = proj.relation_between(a.name, b.name)
        if not rels:
            return SqlRejection(
                "undeclared_join",
                f"对象「{a.display_name}」与「{b.display_name}」之间没有已声明的业务关系，"
                "拒绝臆造 JOIN。",
                {"a": a.name, "b": b.name},
                hint=_undeclared_join_hint(proj, a, b),
            )
        joins.append(f"{a.name}<->{b.name}")
        if has_sensitive_agg:
            reason = _fanout_reason(rels, a, b, tree, col_obj)
            if reason is not None:
                return SqlRejection(
                    "fanout_risk",
                    f"该 JOIN 会使度量沿「{a.display_name}↔{b.display_name}」基数展开导致重复计数"
                    f"（{reason}），拒绝执行以免给出错误数值。",
                    {"a": a.name, "b": b.name, "cardinalities": [
                        r.cardinality.value if r.cardinality else "unknown" for r in rels
                    ]},
                    hint={
                        "safe_rewrite": (
                            "改用 COUNT(DISTINCT <标识列>)，或先在单表内聚合成子查询再 JOIN，"
                            "避免度量沿基数展开被重复计数。"
                        ),
                    },
                )

    # 5) 聚合合法性：SUM/AVG 只能作用于 measure；GROUP BY 只能维度/时间/标识
    aggregations: list[str] = []
    for agg in tree.find_all(exp.AggFunc):
        agg_key = (agg.key or "").lower()
        for c in _aggregated_columns(agg):
            obj = col_obj.get(id(c))
            if obj is None:
                continue
            pv = obj.resolve_property(c.name)
            if pv is None:
                continue
            if agg_key in _FANOUT_SENSITIVE_AGGS and not can_aggregate(pv.semantic_type):
                return SqlRejection(
                    "illegal_aggregation",
                    f"对非度量字段「{pv.name}」（语义类型 {pv.semantic_type.value}）做 "
                    f"{agg_key.upper()} 无业务意义，拒绝执行。",
                    {"column": pv.name, "semantic_type": pv.semantic_type.value},
                    hint={
                        "object": obj.name,
                        "measures_of_object": _semantic_columns(obj, can_aggregate)[
                            :_HINT_LIST_LIMIT
                        ],
                        "fix": "SUM/AVG 只能作用于度量字段；若想计数改用 COUNT。",
                    },
                )
            aggregations.append(f"{agg_key}({obj.name}.{pv.name})")

    for c in _group_by_columns(tree):
        obj = col_obj.get(id(c))
        if obj is None:
            continue
        pv = obj.resolve_property(c.name)
        if pv is None:
            continue
        if not can_group_by(pv.semantic_type):
            return SqlRejection(
                "illegal_group_by",
                f"按字段「{pv.name}」（语义类型 {pv.semantic_type.value}）分组通常是口径错误，"
                "拒绝执行。",
                {"column": pv.name, "semantic_type": pv.semantic_type.value},
                hint={
                    "object": obj.name,
                    "groupable_of_object": _semantic_columns(obj, can_group_by)[
                        :_HINT_LIST_LIMIT
                    ],
                    "fix": "GROUP BY 只能用维度/时间/标识类字段。",
                },
            )

    return SqlCertificate(
        tables=sorted(set(tables)),
        columns=sorted(set(columns)),
        joins=joins,
        aggregations=aggregations,
    )


def _within(node: exp.Expression, kinds: tuple[type, ...]) -> bool:
    """节点是否位于某类子句之内（沿父链上溯）。"""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, kinds):
            return True
        parent = parent.parent
    return False


def _objects_in_join(join: exp.Join, alias_to_obj: dict[str, ObjView]) -> list[ObjView]:
    """取一个 JOIN 的 ON 条件里出现的、去重后的业务对象（按列的表限定解析）。"""
    on = join.args.get("on")
    seen: dict[str, ObjView] = {}
    if on is not None:
        for c in on.find_all(exp.Column):
            obj = alias_to_obj.get(c.table)
            if obj is not None:
                seen[obj.name] = obj
    if len(seen) == 2:
        return list(seen.values())
    # ON 缺失/USING/单侧：退回「被 JOIN 的表 + 其左邻表」难以稳妥判定，返回原样交上层拒绝
    joined = alias_to_obj.get(_joined_table_alias(join))
    if joined is not None and joined.name not in seen:
        seen[joined.name] = joined
    return list(seen.values())


def _joined_table_alias(join: exp.Join) -> str | None:
    this = join.this
    if isinstance(this, exp.Table):
        return this.alias_or_name
    return None


def _fanout_reason(
    rels: list[RelView],
    a: ObjView,
    b: ObjView,
    tree: exp.Expression,
    col_obj: dict[int, ObjView],
) -> str | None:
    """有 SUM/AVG 时，a↔b 的 JOIN 是否会放大被聚合度量所在对象的行。

    返回扇出原因（字符串）或 None（安全）。保守：基数未知一律判扇出。

    判定：设被 SUM/AVG 的度量列所属对象为 M（可能是 a 或 b，也可能都不是）。
    对每条 a↔b 关系 r（存 src→tgt 的基数），换算「从 M 看，另一端是否为『多』端」：
      - 另一端为多（M one_to_many O 或 O many_to_one M）→ 扇出
      - many_to_many → 扇出
      - 基数 None（未知）→ 扇出（保守）
    若 M 不是 a/b 任何一个（度量在别处），则本 JOIN 不直接放大 M，视为安全（交由
    其它 JOIN 各自判定）。
    """
    measure_objs = _aggregated_measure_objects(tree, col_obj)
    if not measure_objs:
        return None
    for r in rels:
        card = r.cardinality
        if card == Cardinality.MANY_TO_MANY:
            return "多对多关系"
        for m in measure_objs:
            other = None
            if m.name == a.name:
                other = b
            elif m.name == b.name:
                other = a
            if other is None:
                continue  # 度量不在这对 JOIN 的两端
            # 从度量对象 M 看向 other，other 是否为「多」端
            # （与语义导航器共用 ontology_projection.other_is_many，规则只有一份）
            many_side = other_is_many(r, m)
            if many_side is None:
                return "关系基数未知"
            if many_side:
                return f"基数 {card.value if card else 'unknown'}"
    return None


def _aggregated_measure_objects(
    tree: exp.Expression, col_obj: dict[int, ObjView]
) -> list[ObjView]:
    """被 SUM/AVG 作用的度量列，各自所属的对象（去重）。"""
    result: dict[str, ObjView] = {}
    for agg in tree.find_all(exp.AggFunc):
        if (agg.key or "").lower() not in _FANOUT_SENSITIVE_AGGS:
            continue
        for c in agg.find_all(exp.Column):
            obj = col_obj.get(id(c))
            if obj is not None:
                result[obj.name] = obj
    return list(result.values())


def _aggregated_columns(agg: exp.AggFunc) -> list[exp.Column]:
    """取聚合函数**真正被聚合的那些列**——CASE 的判定条件里的列不算。

    与 :func:`_group_by_columns` 同一条判据：看这一列是不是被聚合的**值**，而不是
    「有没有出现在聚合函数里」。``SUM(CASE WHEN payment_terms IS NULL THEN 1 ELSE 0 END)``
    求和的是 1/0 这两个字面量，``payment_terms`` 只是谓词——它是空值率统计的标准写法。
    此前递归收集全部列，这条 SQL 会被判成「对非度量字段 payment_terms 做 SUM」而拒掉，
    于是数据质量分析在真实本体上根本跑不完。

    仍然拦得住 ``SUM(CASE WHEN … THEN amount ELSE 0 END)``：amount 在分支**取值**里，
    照收不误。
    """
    cols: list[exp.Column] = []
    for c in agg.find_all(exp.Column):
        node: exp.Expression | None = c
        in_predicate = False
        while node is not None and node is not agg:
            parent = node.parent
            if isinstance(parent, (exp.If, exp.Case)) and parent.args.get("this") is node:
                # If.this = WHEN 谓词；Case.this = CASE <expr> 的switch 表达式，都是判定用的
                in_predicate = True
                break
            node = parent
        if not in_predicate:
            cols.append(c)
    return cols


def _group_by_columns(tree: exp.Expression) -> list[exp.Column]:
    """取 GROUP BY 的**分组键本身**是裸列的那些。

    只看直接的分组表达式，**不递归进派生表达式**：
    `GROUP BY amount` 是口径错误（每个不同金额一组，无意义）；
    但 `GROUP BY CASE WHEN amount > 1000 THEN '高' ELSE '低' END` 是**分桶**，
    完全合法——按输入列的语义类型去否决它，会把标签类口径（P3.5）整类拒掉。

    判据是「分组键是不是这一列」，而不是「这一列有没有出现在分组键里」。
    """
    cols: list[exp.Column] = []
    for grp in tree.find_all(exp.Group):
        for e in grp.expressions:
            if isinstance(e, exp.Column):
                cols.append(e)
    return cols


__all__ = [
    "SqlRejection",
    "SqlCertificate",
    "prove_sql_sound",
]
