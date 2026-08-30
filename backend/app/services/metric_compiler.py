"""口径编译器（P3）：把已发布指标的 ``expression_json`` **确定性地**编译成 SQL。

**为什么这是整条改造线的核心**：没有它，Data Agent 与「一个接了数据库的通用 SQL agent」
没有本质区别——模型读到 `expression_summary` 这段文字（「订单金额求和，排除已取消」），
然后**凭自己的理解重写一遍 SQL**，口径就在这一步丢了。同一个「GMV」，问数、数据应用、
物化 ETL 三处各算各的，语义层名存实亡。

有了它，模型的职责收缩成「选哪个口径 + 按什么维度看」，SQL 由本体确定性生成：
**幻觉面从整个 SQL 语法空间坍缩到一组受控枚举**。

三条硬约束：

1. **编译产物必须过 ``prove_sql_sound``**（§ ``_certify``）。看似冗余——SQL 是我们自己
   生成的——实为关键不变式：它保证「编译器不会生成证明器会拒的 SQL」，二者永不打架。
   任何一次自证失败都是编译器 bug，必须当场可见而不是让 Agent 去撞。
2. **JOIN 只走语义导航器**（P1.2），与证明器共用同一份投影与基数规则。
3. **``caliber_trace`` 是一等交付物**，不是日志：它取代 ``chat_bi._steps_to_caliber``
   那种「从工具轨迹事后反推口径」的猜测，让前端口径卡从猜测变成契约。

字面量一律走 sqlglot 的 ``exp.Literal`` 构造，不做字符串拼接——注入面为零。

暂只编译 ``type == "metric"``（占比最高）。``tag`` / ``rule`` 明确报错，见 P3.5。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlalchemy.orm import Session

from app.models import BusinessLogic, EntityStatus
from app.ontology_types import SemanticType, can_aggregate, can_group_by
from app.services.ontology_projection import (
    ObjView,
    OntologyProjection,
    PropView,
    build_projection,
)
from app.services.semantic_navigator import JoinHop, find_join_path
from app.services.sql_soundness import SqlRejection, prove_sql_sound

logger = logging.getLogger("ontometa.metric_compiler")

# expression_formatter 产出的聚合算子全集
_METRIC_OPS = {"sum", "count", "avg", "min", "max", "distinct_count"}
# 会被 JOIN 扇出放大的算子（与 sql_soundness._FANOUT_SENSITIVE_AGGS 同口径）
_FANOUT_SENSITIVE = {"sum", "avg"}
# 比较算子白名单：只认 expression_formatter 会产出的这些，其余一律拒绝
_COMPARE_OPS = {"=", "!=", ">", ">=", "<", "<=", "like", "in", "not_in"}
_TIME_UNITS = {"day", "week", "month", "quarter", "year"}

_DEFAULT_LIMIT = 100


class MetricCompileError(ValueError):
    """口径无法编译。``code`` 供程序分支，``hint`` 是给模型的修复信号（同 P1.4 取向）。"""

    def __init__(self, code: str, message: str, hint: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or {}


@dataclass(frozen=True)
class CompiledMetric:
    logic_id: str
    logic_name: str
    logic_display_name: str
    logic_type: str  # metric | tag | rule
    sql: str
    base_object: str
    objects: list[str]
    # 标识符 → 中文显示名。答案要用显示名称呼对象，没有它模型只能拿 `customer`
    # 这种技术名作答，或者自己译一个「客户」——后者会被 F4 判成未接地实体。
    object_labels: dict[str, str]
    dimensions: list[str]
    grain: str | None
    join_hops: list[dict]
    caliber_trace: list[str]
    certificate: dict
    fanout_note: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "logic_id": self.logic_id,
            "logic": self.logic_display_name,
            "logic_type": self.logic_type,
            "sql": self.sql,
            "base_object": self.base_object,
            "objects": self.objects,
            "object_labels": self.object_labels,
            "dimensions": self.dimensions,
            "grain": self.grain,
            "join_hops": self.join_hops,
            "caliber_trace": self.caliber_trace,
            "certificate": self.certificate,
        }
        if self.fanout_note:
            d["fanout_note"] = self.fanout_note
        if self.warnings:
            d["warnings"] = self.warnings
        return d


# --------------------------------------------------------------------------- refs


@dataclass(frozen=True)
class _Ref:
    ref_id: str
    obj: ObjView
    prop: PropView | None  # 只绑对象、不绑字段的 ref（如 count 的主对象）为 None

    @property
    def qualified(self) -> str:
        return f"{self.obj.name}.{self.prop.name}" if self.prop else self.obj.name

    def column(self) -> exp.Column:
        if self.prop is None:
            raise MetricCompileError(
                "ref_without_property",
                f"引用「{self.obj.display_name}」未绑定到具体字段，无法参与计算。",
            )
        return exp.column(self.prop.name, table=self.obj.name)


def _resolve_refs(ast: dict, proj: OntologyProjection) -> dict[str, _Ref]:
    """``refs`` 数组 → {ref_id: _Ref}，逐条对**已发布投影**校验。

    口径可能是在字段/对象被下线之前定的：此时宁可整条口径编译失败，也不能悄悄
    换个字段算出一个数——那正是「口径漂移」最隐蔽的形态。
    """
    out: dict[str, _Ref] = {}
    for r in ast.get("refs") or []:
        if not isinstance(r, dict) or not r.get("ref_id"):
            continue
        obj_name = r.get("object_name")
        obj = proj.object_of(obj_name) if obj_name else None
        if obj is None:
            raise MetricCompileError(
                "unresolved_object",
                f"口径引用的对象「{obj_name or '?'}」不在已发布本体中，无法编译。",
                {"object": obj_name},
            )
        prop = None
        prop_name = r.get("property_name")
        if prop_name:
            prop = obj.resolve_property(prop_name)
            if prop is None:
                raise MetricCompileError(
                    "unresolved_property",
                    f"口径引用的字段「{obj.display_name}.{prop_name}」不在已发布本体中，无法编译。",
                    {
                        "object": obj.name,
                        "property": prop_name,
                        "available_columns": sorted(p.name for p in obj.props.values())[:20],
                    },
                )
        out[str(r["ref_id"])] = _Ref(str(r["ref_id"]), obj, prop)
    return out


def _ref_of(node: Any, refs: dict[str, _Ref]) -> _Ref | None:
    if not isinstance(node, dict):
        return None
    rid = node.get("ref")
    return refs.get(str(rid)) if isinstance(rid, str) else None


def _literal_of(node: Any) -> exp.Expression | None:
    """字面量节点 → sqlglot 字面量。用构造而非拼串，注入面为零。"""
    if not isinstance(node, dict):
        return None
    # expression_formatter 用 {"value": v}；LLM 有时写成 {"literal": v}，两种都认
    for key in ("value", "literal"):
        if key in node:
            return _as_literal(node[key])
    return None


def _as_literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.null()
    if isinstance(value, bool):
        return exp.true() if value else exp.false()
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    if isinstance(value, (list, tuple)):
        return exp.Tuple(expressions=[_as_literal(v) for v in value])
    return exp.Literal.string(str(value))


# --------------------------------------------------------------------------- 条件


def _compile_condition(node: Any, refs: dict[str, _Ref], trace: list[str]) -> exp.Expression | None:
    """AST 条件 → sqlglot 表达式。认不出的形态一律拒绝，不猜。"""
    if not isinstance(node, dict):
        return None
    op = (node.get("op") or "").strip().lower()

    if op in ("and", "or"):
        parts = [
            c for c in (
                _compile_condition(x, refs, trace) for x in (node.get("conditions") or [])
            ) if c is not None
        ]
        if not parts:
            return None
        combined = parts[0]
        for p in parts[1:]:
            combined = exp.And(this=combined, expression=p) if op == "and" else exp.Or(
                this=combined, expression=p
            )
        return exp.Paren(this=combined) if len(parts) > 1 else combined

    left_ref = _ref_of(node.get("left"), refs)
    if left_ref is None:
        raise MetricCompileError(
            "unresolved_filter",
            "口径的过滤条件左值不是一个可解析的本体字段，无法编译。",
            {"condition": node},
        )
    if op not in _COMPARE_OPS:
        raise MetricCompileError(
            "unsupported_operator",
            f"过滤条件使用了不支持的比较算子「{op or '?'}」。",
            {"supported": sorted(_COMPARE_OPS)},
        )
    right = _literal_of(node.get("right"))
    if right is None:
        right_ref = _ref_of(node.get("right"), refs)
        if right_ref is None:
            raise MetricCompileError(
                "incomplete_filter",
                f"过滤条件「{left_ref.qualified} {op} ?」缺少比较值——"
                "口径未形式化完整，不能猜一个值代入。",
                {"left": left_ref.qualified, "op": op},
            )
        right = right_ref.column()

    col = left_ref.column()
    trace.append(f"过滤：{left_ref.qualified} {op} {_render_literal(right)}")
    return _comparison(col, op, right)


def _comparison(col: exp.Expression, op: str, right: exp.Expression) -> exp.Expression:
    if op == "=":
        return exp.EQ(this=col, expression=right)
    if op == "!=":
        return exp.NEQ(this=col, expression=right)
    if op == ">":
        return exp.GT(this=col, expression=right)
    if op == ">=":
        return exp.GTE(this=col, expression=right)
    if op == "<":
        return exp.LT(this=col, expression=right)
    if op == "<=":
        return exp.LTE(this=col, expression=right)
    if op == "like":
        return exp.Like(this=col, expression=right)
    # in / not_in：单值也包成 tuple，保证语法合法
    values = right.expressions if isinstance(right, exp.Tuple) else [right]
    in_expr = exp.In(this=col, expressions=list(values))
    return exp.Not(this=exp.Paren(this=in_expr)) if op == "not_in" else in_expr


def _render_literal(node: exp.Expression) -> str:
    try:
        return node.sql()
    except Exception:  # noqa: BLE001
        return "?"


# --------------------------------------------------------------------------- 聚合


def _compile_aggregate(
    operation: str, measure: _Ref | None, trace: list[str]
) -> exp.Expression:
    op = (operation or "").strip().lower()
    if op not in _METRIC_OPS:
        # 错误必须能被照着改：只回「支持哪些算子」时，键名写错（aggregation/op 之类）的
        # 调用方会拿着一份正确的算子表反复改 value，改不到问题上——实测连挂三轮。
        raise MetricCompileError(
            "unsupported_operation",
            (
                f"不支持的聚合算子「{operation}」。"
                if op
                else "metric 的表达式体缺少聚合算子：键名是 operation（不是 aggregation/op/agg）。"
            ),
            {
                "supported": sorted(_METRIC_OPS),
                "expected_body_keys": ["operation", "args", "group_by", "filter"],
                "example_body": {
                    "operation": "count",
                    "args": [{"ref": "别名"}],
                    "group_by": [],
                    "filter": {
                        "left": {"ref": "别名"},
                        "op": "=",
                        "right": {"value": 0},
                    },
                },
            },
        )
    if measure is None or measure.prop is None:
        if op != "count":
            raise MetricCompileError(
                "missing_measure",
                f"{op.upper()} 需要一个被聚合的字段，但口径里没有绑定。",
            )
        trace.append("聚合：COUNT(*)（计数满足条件的行）")
        return exp.Count(this=exp.Star())

    col = measure.column()
    # 与证明器同一条语义代数：SUM/AVG 只能作用于度量
    if op in _FANOUT_SENSITIVE and not can_aggregate(measure.prop.semantic_type):
        raise MetricCompileError(
            "illegal_aggregation",
            f"对非度量字段「{measure.qualified}」"
            f"（语义类型 {measure.prop.semantic_type.value}）做 {op.upper()} 无业务意义。",
            {"semantic_type": measure.prop.semantic_type.value},
        )
    trace.append(f"聚合：{op.upper()}({measure.qualified})")
    if op == "sum":
        return exp.Sum(this=col)
    if op == "avg":
        return exp.Avg(this=col)
    if op == "min":
        return exp.Min(this=col)
    if op == "max":
        return exp.Max(this=col)
    if op == "distinct_count":
        return exp.Count(this=exp.Distinct(expressions=[col]))
    return exp.Count(this=col)


# --------------------------------------------------------------------------- tag / rule


def _has_label(then_node: Any) -> bool:
    """该分支是否给了**非空**标签值。"""
    if not isinstance(then_node, dict):
        return False
    for key in ("value", "literal"):
        if key in then_node:
            v = then_node[key]
            return v is not None and str(v).strip() != ""
    return False


def _compile_tag(
    body: dict, refs: dict[str, _Ref], logic: BusinessLogic, trace: list[str]
) -> exp.Expression:
    """标签口径 → ``CASE WHEN … THEN … ELSE … END``（分桶表达式）。

    编译成**分布查询**（按标签取值分组计数），而不是逐行打标：
    「高价值客户有多少」「各分层各占多少」才是问数场景真正要的；
    逐行明细本就该用 run_sql 自己写。
    """
    cases = body.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise MetricCompileError(
            "bad_expression", f"标签「{logic.display_name}」没有任何分支，无法编译。"
        )

    whens: list[exp.Expression] = []
    default: exp.Expression | None = None
    labelled = 0
    for case in cases:
        if not isinstance(case, dict):
            continue
        then = _literal_of(case.get("then")) or exp.null()
        # 「解析出了字面量」不等于「有标签值」——形式化没抽出标签时写的是
        # {"value": null}，它会被解析成一个合法的 NULL 字面量。判据要看**值本身**。
        if _has_label(case.get("then")):
            labelled += 1
        cond_node = case.get("when")
        if cond_node is None:
            default = then          # when=null 即 else 分支
            continue
        cond = _compile_condition(cond_node, refs, trace)
        if cond is None:
            continue
        whens.append(exp.If(this=cond, true=then))

    if not whens:
        raise MetricCompileError(
            "bad_expression",
            f"标签「{logic.display_name}」没有可解析的判定分支，无法编译。",
        )
    if labelled == 0:
        # 形式化时没抽出标签值（`then` 全是 null）——编出来只会是一列 NULL。
        # 与全链路一致：拿不准就报错，不糊一个能跑但无意义的查询。
        raise MetricCompileError(
            "incomplete_tag",
            f"标签「{logic.display_name}」的各分支都没有标签值，口径未形式化完整，无法编译。",
            {"fix": "请在本体中补全该标签各分支的取值后重试。"},
        )

    trace.append(f"标签分桶：{len(whens)} 个判定分支" + ("（含默认分支）" if default is not None else ""))
    return exp.Case(ifs=whens, default=default)


def _compile_rule(
    body: dict, refs: dict[str, _Ref], logic: BusinessLogic, trace: list[str]
) -> exp.Expression:
    """规则口径 → **违规行**的谓词（对规则条件取反）。

    规则本身是「应当成立」的断言，直接查它没有意义（那只是把满足的行数出来）；
    有价值的是**违规**：不满足断言的有多少行。故编译成 ``NOT (<condition>)``。
    """
    cond_node = body.get("condition")
    if cond_node is None:
        raise MetricCompileError(
            "bad_expression",
            f"规则「{logic.display_name}」没有判定条件，无法编译。",
        )
    cond = _compile_condition(cond_node, refs, trace)
    if cond is None:
        raise MetricCompileError(
            "bad_expression",
            f"规则「{logic.display_name}」的判定条件无法解析，无法编译。",
        )
    message = str(body.get("message") or "").strip()
    trace.append(
        "规则校验：统计**不满足**该条件的行数"
        + (f"（违规提示：{message}）" if message else "")
    )
    return exp.Not(this=exp.Paren(this=cond))


# --------------------------------------------------------------------------- 维度


def _resolve_dimension(token: str, proj: OntologyProjection, base: ObjView) -> _Ref:
    """``"customer.region"`` 或 ``"region"``（默认基对象）→ _Ref。"""
    raw = (token or "").strip()
    if not raw:
        raise MetricCompileError("bad_dimension", "维度不能为空。")
    if "." in raw:
        obj_token, prop_token = raw.split(".", 1)
        obj = proj.object_of(obj_token)
        if obj is None:
            raise MetricCompileError(
                "unknown_dimension_object",
                f"维度里的对象「{obj_token}」不是已发布业务对象。",
                {"known_objects": sorted(o.name for o in proj.objects.values())[:20]},
            )
    else:
        obj, prop_token = base, raw
    prop = obj.resolve_property(prop_token)
    if prop is None:
        raise MetricCompileError(
            "unknown_dimension",
            f"字段「{prop_token}」不属于对象「{obj.display_name}」。",
            {
                "object": obj.name,
                "available_columns": sorted(p.name for p in obj.props.values())[:20],
            },
        )
    if not can_group_by(prop.semantic_type):
        raise MetricCompileError(
            "illegal_group_by",
            f"按「{obj.name}.{prop.name}」（语义类型 {prop.semantic_type.value}）分组"
            "通常是口径错误。",
            {
                "groupable_of_object": sorted(
                    p.name for p in obj.props.values() if can_group_by(p.semantic_type)
                )[:20],
            },
        )
    return _Ref(f"dim:{obj.name}.{prop.name}", obj, prop)


# --------------------------------------------------- 口径类型 → 结果表形状（唯一权威）
#
# 编译器对三类口径产出的列并不相同（见下面 §1 的分叉），所以「口径结果表长什么样」
# 只能由这里说了算。此前有**两处**各自写死了指标形状（stat_date + metric_value decimal）：
# `agents/executors/metric._build_table`（指标任务）与 `warehouse_generator._logic_tables`
# （本体物化）。两处都对标签/规则给错形状，且彼此还会不一致——物化建的表与指标任务
# INSERT 的列对不上。本模块的 docstring 早就写过「不要另写一套口径→SQL 的翻译」，
# 结果表形状是同一条道理。

LOGIC_TYPES: tuple[str, ...] = ("metric", "tag", "rule")

# 供工具 schema / 前端提示复用，避免各处再抄一份算子清单（抄岔了就是编译期才报错）。
METRIC_OPS: tuple[str, ...] = tuple(sorted(_METRIC_OPS))
COMPARE_OPS: tuple[str, ...] = tuple(sorted(_COMPARE_OPS))

# 标签取值列名。标签的「值」是个可分组的字符串标签，不能挤进 metric_value。
TAG_VALUE_COLUMN = "tag_value"


def effective_logic_type(logic: BusinessLogic) -> str:
    """这条口径**编译时会被当成哪一类**。

    以 AST 的 ``type`` 为准、表列 ``logic_type`` 兜底——``compile_metric`` 读的正是 AST，
    调用方若按表列判类型，建出来的表列就可能与编译器产的 SQL 列对不上。
    """
    if logic.expression_json:
        try:
            ast = json.loads(logic.expression_json)
        except (TypeError, ValueError):
            ast = None
        if isinstance(ast, dict):
            lt = str(ast.get("type") or "").strip().lower()
            if lt in LOGIC_TYPES:
                return lt
    lt = str(logic.logic_type or "metric").strip().lower()
    return lt if lt in LOGIC_TYPES else "metric"


def value_source_column(logic_type: str, logic_name: str) -> str:
    """``metric_value`` 该从编译产物的哪一列取。

    metric → 度量列别名即口径技术名；tag → 分桶计数 ``row_count``；
    rule → 违规行数 ``violations``（**没有**以口径名命名的列，照 metric 取会 column not found）。
    """
    if logic_type == "tag":
        return "row_count"
    if logic_type == "rule":
        return "violations"
    return logic_name


def result_column_specs(
    logic_type: str, display_name: str
) -> tuple[tuple[str, str, str, str], ...]:
    """结果表里**除 stat_date 与维度列以外**的列：(列名, 物理类型, 语义类型, 注释)。

    返回裸元组而不是 LogicalColumn：编译器不该依赖 warehouse 层，由调用方自行构造。
    tag/rule 落的是**计数**不是金额——语义标 count（adapter 语义优先，映射成整型），
    标成 amount 会得到 DECIMAL(18,4)，读表的人会以为那是笔钱。
    """
    if logic_type == "tag":
        return (
            (TAG_VALUE_COLUMN, "string", "category", f"{display_name} · 标签取值"),
            ("metric_value", "bigint", "count", f"{display_name} · 该标签取值下的实体数"),
        )
    if logic_type == "rule":
        return (("metric_value", "bigint", "count", f"{display_name} · 违规行数"),)
    return (("metric_value", "decimal", "amount", display_name),)


# --------------------------------------------------------------------------- 主流程


def compile_candidate(
    db: Session,
    *,
    ontology_id: str,
    ast: dict,
    name: str,
    display_name: str | None = None,
    expression_summary: str | None = None,
    dimensions: list[str] | tuple[str, ...] = (),
    filters: list[dict] | tuple[dict, ...] = (),
    grain: str | None = None,
    time_property: str | None = None,
    limit: int | None = _DEFAULT_LIMIT,
    dialect: str | None = None,
    mapping: dict | None = None,
) -> CompiledMetric:
    """编译一个**尚未落库**的候选表达式（AST），走与已发布口径完全同一条路。

    为什么需要它：``compile_metric`` 只认「已落库且已发布」的口径，于是编译器——这个系统里
    唯一能判定「这条口径到底能不能变成一条可执行且自证的 SQL」的东西——在**写入路径上从未
    被调用过**。表达式格式化出来什么就存什么、存什么就发布什么，第一次发现 AST 编不出来
    是在建任务的 dry-run 里（甚至是在问数时）。谁写的口径，谁在几天后才知道它是坏的。

    有了它，「LLM 出表达式」才敢做：模型产 AST → 当场编译 → 编不过就把编译器的错误原样
    还给模型让它改，编过了才允许作为提案交给人看，而人看到的是**真 SQL 与口径展开轨迹**，
    不是一段自然语言承诺。

    传入的候选口径不落库、不进 session（transient ORM 实例），因此本函数无任何写副作用。
    """
    candidate = BusinessLogic(
        ontology_id=ontology_id,
        name=name,
        display_name=display_name or name,
        logic_type=str((ast or {}).get("type") or "metric"),
        expression_summary=expression_summary,
        status=EntityStatus.PUBLISHED.value,
    )
    return _compile_ast(
        db,
        logic=candidate,
        ast=ast if isinstance(ast, dict) else {},
        dimensions=dimensions,
        filters=filters,
        grain=grain,
        time_property=time_property,
        limit=limit,
        dialect=dialect,
        mapping=mapping,
    )


def compile_metric(
    db: Session,
    logic_id: str,
    *,
    dimensions: list[str] | tuple[str, ...] = (),
    filters: list[dict] | tuple[dict, ...] = (),
    grain: str | None = None,
    time_property: str | None = None,
    limit: int | None = _DEFAULT_LIMIT,
    dialect: str | None = None,
    mapping: dict | None = None,
) -> CompiledMetric:
    """把一个已发布指标编译成 SQL + 口径轨迹 + 语义证书。"""
    logic = db.get(BusinessLogic, logic_id)
    if logic is None or logic.status != EntityStatus.PUBLISHED.value:
        raise MetricCompileError(
            "logic_not_found", f"业务逻辑「{logic_id}」不存在或未发布。"
        )
    return _compile_ast(
        db,
        logic=logic,
        ast=_load_ast(logic),
        dimensions=dimensions,
        filters=filters,
        grain=grain,
        time_property=time_property,
        limit=limit,
        dialect=dialect,
        mapping=mapping,
    )


def _compile_ast(
    db: Session,
    *,
    logic: BusinessLogic,
    ast: dict,
    dimensions: list[str] | tuple[str, ...] = (),
    filters: list[dict] | tuple[dict, ...] = (),
    grain: str | None = None,
    time_property: str | None = None,
    limit: int | None = _DEFAULT_LIMIT,
    dialect: str | None = None,
    mapping: dict | None = None,
) -> CompiledMetric:
    """编译主体。``logic`` 只作元信息载体（名字/域/摘要），可以是未落库的候选。"""
    logic_type = (ast.get("type") or "metric").lower()
    if logic_type not in ("metric", "tag", "rule"):
        raise MetricCompileError(
            "unsupported_logic_type",
            f"「{logic.display_name}」的逻辑类型 {logic_type} 无法编译成查询。",
            {"logic_type": logic_type, "supported": ["metric", "tag", "rule"]},
        )

    proj = build_projection(db, logic.ontology_id, mapping)
    refs = _resolve_refs(ast, proj)
    body = ast.get("body") or {}
    if not isinstance(body, dict):
        raise MetricCompileError("bad_expression", "口径表达式结构不合法，无法编译。")

    trace: list[str] = [f"口径「{logic.display_name}」（{logic.name}）"]
    if logic.expression_summary:
        trace.append(f"原始口径：{logic.expression_summary}")

    # 1) 聚合。三类逻辑在这里分叉，其余步骤（维度/过滤/JOIN/自证）完全共用——
    #    tag 与 rule 的产出同样是聚合查询，只是被聚合的东西不同：
    #    metric 聚合度量、tag 按分桶计数、rule 数违规行。
    args = body.get("args") or []
    measure = _ref_of(args[0], refs) if args else None
    operation = (body.get("operation") or "").strip().lower()
    tag_expr: exp.Expression | None = None       # tag：CASE 表达式，既进 SELECT 也作分组键
    rule_condition: exp.Expression | None = None  # rule：违规谓词，并入 WHERE
    if logic_type == "tag":
        tag_expr = _compile_tag(body, refs, logic, trace)
        agg = exp.Count(this=exp.Star())
        agg_alias = "row_count"
    elif logic_type == "rule":
        rule_condition = _compile_rule(body, refs, logic, trace)
        agg = exp.Count(this=exp.Star())
        agg_alias = "violations"
    else:
        agg = _compile_aggregate(operation, measure, trace)
        agg_alias = logic.name

    # 2) 基对象：度量所在对象优先；纯计数口径退回分组维度 / 首个 ref
    base = _pick_base(measure, body, refs)
    if base is None:
        raise MetricCompileError(
            "no_base_object", f"「{logic.display_name}」没有可确定的主对象，无法编译。"
        )

    # 3) 维度 = 口径自带的 group_by + 调用方追加的
    dim_refs: list[_Ref] = []
    for node in body.get("group_by") or []:
        r = _ref_of(node, refs)
        if r is not None and r.prop is not None:
            dim_refs.append(r)
    for token in dimensions or ():
        dim_refs.append(_resolve_dimension(str(token), proj, base))
    dim_refs = _dedup_refs(dim_refs)

    # 4) 过滤 = 口径自带的 + 调用方追加的
    conditions: list[exp.Expression] = []
    own = _compile_condition(body.get("filter"), refs, trace)
    if own is not None:
        conditions.append(own)
    for f in filters or ():
        conditions.append(_compile_extra_filter(f, proj, base, trace))
    if rule_condition is not None:
        conditions.append(rule_condition)

    # 5) 时间粒度
    grain_expr, grain_ref, grain_unit = _compile_grain(
        grain, time_property, proj, base, dim_refs, trace
    )

    # 6) 参与对象 → JOIN 路径（只走语义导航器）
    involved = _involved_objects(base, measure, dim_refs, grain_ref, refs, body)
    hops, fanout_note = _plan_joins(proj, base, involved, operation, trace)

    # 7) 组装
    select = _assemble(
        base=base, agg=agg, agg_alias=agg_alias,
        dim_refs=dim_refs, grain_expr=grain_expr, grain_unit=grain_unit,
        conditions=conditions, hops=hops, limit=limit,
        tag_expr=tag_expr, tag_alias=logic.name,
    )
    sql = _render(select, dialect)

    # 8) 自证：编译产物必须过证明器
    certificate = _certify(sql, proj, dialect, logic)
    trace.append(
        "已通过语义证明：表 "
        + "、".join(certificate.get("tables") or [])
        + ("；聚合 " + "、".join(certificate.get("aggregations") or [])
           if certificate.get("aggregations") else "")
    )

    return CompiledMetric(
        logic_id=logic.id,
        logic_name=logic.name,
        logic_display_name=logic.display_name,
        logic_type=logic_type,
        sql=sql,
        base_object=base.name,
        objects=sorted({base.name} | {o.name for o in involved}),
        object_labels={o.name: o.display_name for o in [base, *involved]},
        dimensions=[r.qualified for r in dim_refs],
        grain=grain_unit,
        join_hops=[h.to_dict() for h in hops],
        caliber_trace=trace,
        certificate=certificate,
        fanout_note=fanout_note,
    )


# --------------------------------------------------------------------------- 内部


def _load_ast(logic: BusinessLogic) -> dict:
    if not logic.expression_json:
        raise MetricCompileError(
            "no_expression",
            f"「{logic.display_name}」只有文字口径、尚未形式化（expression_json 为空），"
            "无法编译成 SQL。",
            {"expression_summary": logic.expression_summary},
        )
    try:
        ast = json.loads(logic.expression_json)
    except (TypeError, ValueError) as exc:
        raise MetricCompileError(
            "bad_expression", f"口径表达式不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(ast, dict):
        raise MetricCompileError("bad_expression", "口径表达式结构不合法。")
    return ast


def _pick_base(measure: _Ref | None, body: dict, refs: dict[str, _Ref]) -> ObjView | None:
    if measure is not None:
        return measure.obj
    for node in body.get("group_by") or []:
        r = _ref_of(node, refs)
        if r is not None:
            return r.obj
    return next(iter(refs.values())).obj if refs else None


def _dedup_refs(items: list[_Ref]) -> list[_Ref]:
    seen: set[str] = set()
    out: list[_Ref] = []
    for r in items:
        if r.qualified in seen:
            continue
        seen.add(r.qualified)
        out.append(r)
    return out


def _compile_extra_filter(
    f: Any, proj: OntologyProjection, base: ObjView, trace: list[str]
) -> exp.Expression:
    """调用方追加的过滤：``{"property": "order.status", "op": "=", "value": "Completed"}``。"""
    if not isinstance(f, dict):
        raise MetricCompileError("bad_filter", "过滤条件必须是对象。")
    token = str(f.get("property") or f.get("dimension") or "").strip()
    if not token:
        raise MetricCompileError("bad_filter", "过滤条件缺少 property。")
    obj, prop = _resolve_column(token, proj, base)
    op = (str(f.get("op") or "=")).strip().lower()
    if op not in _COMPARE_OPS:
        raise MetricCompileError(
            "unsupported_operator",
            f"不支持的比较算子「{op}」。",
            {"supported": sorted(_COMPARE_OPS)},
        )
    if "value" not in f and "values" not in f:
        raise MetricCompileError(
            "incomplete_filter", f"过滤条件「{obj.name}.{prop.name} {op} ?」缺少比较值。"
        )
    value = f.get("values") if "values" in f else f.get("value")
    right = _as_literal(value)
    trace.append(f"过滤（调用方追加）：{obj.name}.{prop.name} {op} {_render_literal(right)}")
    return _comparison(exp.column(prop.name, table=obj.name), op, right)


def _resolve_column(
    token: str, proj: OntologyProjection, base: ObjView
) -> tuple[ObjView, PropView]:
    if "." in token:
        obj_token, prop_token = token.split(".", 1)
        obj = proj.object_of(obj_token)
        if obj is None:
            raise MetricCompileError(
                "unknown_object", f"「{obj_token}」不是已发布业务对象。"
            )
    else:
        obj, prop_token = base, token
    prop = obj.resolve_property(prop_token)
    if prop is None:
        raise MetricCompileError(
            "unknown_column",
            f"字段「{prop_token}」不属于对象「{obj.display_name}」。",
            {"available_columns": sorted(p.name for p in obj.props.values())[:20]},
        )
    return obj, prop


def _compile_grain(
    grain: str | None,
    time_property: str | None,
    proj: OntologyProjection,
    base: ObjView,
    dim_refs: list[_Ref],
    trace: list[str],
) -> tuple[exp.Expression | None, _Ref | None, str | None]:
    """时间粒度：DATE_TRUNC(unit, 时间字段)。时间字段不明确时**报错要求指定**，不猜。"""
    if not grain:
        return None, None, None
    unit = str(grain).strip().lower()
    if unit not in _TIME_UNITS:
        raise MetricCompileError(
            "bad_grain", f"不支持的时间粒度「{grain}」。", {"supported": sorted(_TIME_UNITS)}
        )

    if time_property:
        obj, prop = _resolve_column(str(time_property), proj, base)
    else:
        candidates = [r for r in dim_refs if r.prop and r.prop.semantic_type is SemanticType.TEMPORAL]
        if not candidates:
            candidates = [
                _Ref(f"time:{base.name}.{p.name}", base, p)
                for p in base.props.values()
                if p.semantic_type is SemanticType.TEMPORAL
            ]
        if len(candidates) != 1:
            raise MetricCompileError(
                "ambiguous_time_property",
                "指定了时间粒度但无法确定按哪个时间字段——请用 time_property 明确指定。",
                {"candidates": [c.qualified for c in candidates]},
            )
        obj, prop = candidates[0].obj, candidates[0].prop

    ref = _Ref(f"time:{obj.name}.{prop.name}", obj, prop)
    if prop.semantic_type is not SemanticType.TEMPORAL:
        raise MetricCompileError(
            "bad_time_property",
            f"「{ref.qualified}」不是时间语义字段，不能做时间粒度截断。",
            {"semantic_type": prop.semantic_type.value},
        )
    trace.append(f"时间粒度：按{unit}截断 {ref.qualified}")
    return (
        exp.DateTrunc(unit=exp.Literal.string(unit), this=ref.column()),
        ref,
        unit,
    )


def _involved_objects(
    base: ObjView,
    measure: _Ref | None,
    dim_refs: list[_Ref],
    grain_ref: _Ref | None,
    refs: dict[str, _Ref],
    body: dict,
) -> list[ObjView]:
    """本次查询触及的**非基**对象（需要 JOIN 进来的那些）。

    直接扫**整个 body** 收集 ref：metric 的 filter、tag 的 cases、rule 的 condition
    形状各不相同，逐个特判迟早漏一个——漏了就会生成一条缺 JOIN 的 SQL，
    然后被证明器以 unknown_table 拒掉，白跑一轮。
    """
    seen: dict[str, ObjView] = {}
    pool: list[_Ref | None] = [measure, grain_ref, *dim_refs]
    for rid in _filter_ref_ids(body):
        pool.append(refs.get(rid))
    for r in pool:
        if r is None or r.obj.name == base.name:
            continue
        seen[r.obj.name] = r.obj
    return list(seen.values())


def _filter_ref_ids(node: Any, acc: set[str] | None = None) -> set[str]:
    if acc is None:
        acc = set()
    if isinstance(node, dict):
        rid = node.get("ref")
        if isinstance(rid, str):
            acc.add(rid)
        for v in node.values():
            _filter_ref_ids(v, acc)
    elif isinstance(node, list):
        for item in node:
            _filter_ref_ids(item, acc)
    return acc


def _plan_joins(
    proj: OntologyProjection,
    base: ObjView,
    targets: list[ObjView],
    operation: str,
    trace: list[str],
) -> tuple[list[JoinHop], str | None]:
    """为每个非基对象规划 JOIN。只走语义导航器，绝不自行构造关联。"""
    hops: list[JoinHop] = []
    joined = {base.name}
    fanout_note: str | None = None

    for target in targets:
        if target.name in joined:
            continue
        paths = find_join_path(proj, base.name, target.name, measure_object=base.name)
        usable = next((p for p in paths if p.joinable), None)
        if usable is None:
            raise MetricCompileError(
                "unjoinable",
                f"「{base.display_name}」与「{target.display_name}」之间没有可用的关联路径，"
                "无法按该维度拆分此口径。",
                {
                    "from": base.name,
                    "to": target.name,
                    "paths_found": [p.to_dict() for p in paths],
                },
            )
        if usable.fanout_risk and operation in _FANOUT_SENSITIVE:
            raise MetricCompileError(
                "fanout_risk",
                f"沿「{base.display_name}」→「{target.display_name}」的关联会放大行，"
                f"{operation.upper()} 会重复计数：{usable.fanout_risk}",
                {"safe_aggs": usable.safe_aggs, "path": usable.to_dict()},
            )
        if usable.fanout_risk:
            fanout_note = usable.fanout_risk
        for hop in usable.hops:
            if hop.to_obj in joined:
                continue
            hops.append(hop)
            joined.add(hop.to_obj)
            trace.append(
                f"关联：{hop.from_obj} →（{hop.relation_display}）{hop.to_obj}"
                f"，ON {hop.on}（{hop.cardinality}）"
            )
    return hops, fanout_note


def _assemble(
    *,
    base: ObjView,
    agg: exp.Expression,
    agg_alias: str,
    dim_refs: list[_Ref],
    grain_expr: exp.Expression | None,
    grain_unit: str | None,
    conditions: list[exp.Expression],
    hops: list[JoinHop],
    limit: int | None,
    tag_expr: exp.Expression | None = None,
    tag_alias: str = "tag",
) -> exp.Select:
    projections: list[exp.Expression] = []
    group_items: list[exp.Expression] = []

    if tag_expr is not None:
        # 分桶表达式既是输出列也是分组键——「各标签各多少」正是这个形状
        projections.append(exp.alias_(tag_expr, tag_alias))
        group_items.append(tag_expr.copy())
    if grain_expr is not None:
        projections.append(exp.alias_(grain_expr, f"{grain_unit}_bucket"))
        group_items.append(grain_expr.copy())
    for r in dim_refs:
        projections.append(r.column())
        group_items.append(r.column())
    projections.append(exp.alias_(agg, agg_alias))

    select = exp.select(*projections).from_(exp.to_table(base.name))
    for hop in hops:
        select = select.join(
            exp.to_table(hop.to_obj),
            on=exp.EQ(
                this=exp.column(hop.from_key, table=hop.from_obj),
                expression=exp.column(hop.to_key, table=hop.to_obj),
            ),
            join_type="inner",
        )
    if conditions:
        where = conditions[0]
        for c in conditions[1:]:
            where = exp.And(this=where, expression=c)
        select = select.where(where)
    if group_items:
        select = select.group_by(*group_items)
    # limit=None：不加 LIMIT。问答场景要截断（看个数），但写库场景（指标物化成 ADS 表）
    # 加了 LIMIT 就只落 100 行——一个看不出来、结果全错的截断。
    return select if limit is None else select.limit(max(1, int(limit)))


def _render(select: exp.Select, dialect: str | None) -> str:
    """按目标方言渲染并强制加标识符引号（``order`` 这类保留字必须引起来）。"""
    try:
        return select.sql(dialect=dialect or None, identify=True)
    except Exception:  # noqa: BLE001 — 未知方言退回通用渲染
        return select.sql(identify=True)


def _certify(
    sql: str, proj: OntologyProjection, dialect: str | None, logic: BusinessLogic
) -> dict:
    """**关键不变式**：编译产物必须过证明器。

    自证失败只有两种可能——编译器有 bug，或这条口径本就无法安全地表达成查询。
    两者都必须当场报错并带上拒绝码，绝不把一条自己都证不过的 SQL 交出去。
    """
    try:
        verdict = prove_sql_sound(sql, proj, dialect=dialect or None)
    except Exception as exc:  # noqa: BLE001
        raise MetricCompileError(
            "prover_error", f"口径编译产物无法完成语义证明：{exc}"
        ) from exc
    if isinstance(verdict, SqlRejection):
        logger.warning(
            "口径「%s」编译产物未过语义证明：%s | %s | %s",
            logic.name, verdict.code, verdict.message, sql,
        )
        raise MetricCompileError(
            "uncertified_output",
            f"口径「{logic.display_name}」的编译结果未通过语义证明：{verdict.message}",
            {"rejection_code": verdict.code, **(verdict.hint or {})},
        )
    return {
        "tables": list(verdict.tables),
        "columns": list(verdict.columns),
        "joins": list(verdict.joins),
        "aggregations": list(verdict.aggregations),
    }


__all__ = [
    "CompiledMetric",
    "MetricCompileError",
    "compile_metric",
    "compile_candidate",
    "METRIC_OPS",
    "COMPARE_OPS",
    "LOGIC_TYPES",
    "TAG_VALUE_COLUMN",
    "effective_logic_type",
    "value_source_column",
    "result_column_specs",
]
