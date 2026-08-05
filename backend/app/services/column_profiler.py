"""字段取值画像（P1.3）：让 Agent 在写 WHERE 之前知道字段里**实际存着什么**。

**为什么存在**：``sql_soundness`` 证明的是 **schema 合法性**（表存在、列归属、JOIN 有据、
聚合语义合规），**不证明谓词有意义**。模型写 ``WHERE status = '已完成'`` 而库里存的是
``Completed``——语义证明全绿、SQL 执行成功、返回 0 行、答案「该状态无数据」。
**静默错误，全链路无人拦截**，这是 DATA_AGENT_V2_PLAN §1.1.B 记的那个缺口。

补法只有一条：去读真实取值。故本模块按字段的**语义类型**分派画像策略——
类别/标识看取值分布，度量看极值与均值，时间看区间；技术字段一律不画像
（``SemanticType.TECHNICAL`` 的语义就是「默认不入业务查询」）。

**生成的 SQL 由本模块负责正确性**（不同于 Agent 手写的 SQL）：用 sqlglot 按目标方言
渲染并加标识符引号——``order`` 这类保留字不加引号会直接语法错。渲染完仍过一遍
``prove_sql_sound`` 自证：证不过说明是生成器的 bug，宁可不画像也不发一条错 SQL。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from app.config import settings as env_settings
from app.ontology_types import SemanticType
from app.services.ontology_projection import ObjView, OntologyProjection, PropView
from app.services.sql_soundness import SqlRejection, prove_sql_sound

logger = logging.getLogger("ontometa.column_profiler")

# 输出列别名统一加前缀：`_apply_mapping` 会把本体属性名整词替换成物理列名，
# 若别名恰好叫 total/value 且本体里也有同名属性，别名会被一起改写。加前缀即不可能命中。
_A = "__p_"

# sqlglot 不认识的后端 → 用兼容方言渲染（kyuubi 走 Spark/Hive 系语法）
_DIALECT_ALIASES = {"kyuubi": "hive"}

# 画像不返回明细，只返回聚合与 TopN；行数上限给 execute_sql 兜底用
_PROFILE_ROW_LIMIT = 200
# 画像查询超时（与 run_sql 同档：画像也是打真实库的只读查询）
_SQL_TIMEOUT = 15


@dataclass(frozen=True)
class ColumnProfile:
    """单个字段的取值画像。``available=False`` 时只有 ``note`` 有意义。"""

    object_name: str
    property_name: str
    semantic_type: str
    strategy: str  # top_values | numeric_range | temporal_range | skipped
    available: bool = False
    note: str | None = None
    row_count: int | None = None
    non_null_count: int | None = None
    null_ratio: float | None = None
    distinct_count: int | None = None
    top_values: list[dict] = field(default_factory=list)  # [{value, freq}]
    min_value: Any = None
    max_value: Any = None
    avg_value: Any = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "object": self.object_name,
            "property": self.property_name,
            "semantic_type": self.semantic_type,
            "strategy": self.strategy,
            "available": self.available,
        }
        if self.note:
            d["note"] = self.note
        if not self.available:
            return d
        for key, val in (
            ("row_count", self.row_count),
            ("non_null_count", self.non_null_count),
            ("null_ratio", self.null_ratio),
            ("distinct_count", self.distinct_count),
            ("min_value", self.min_value),
            ("max_value", self.max_value),
            ("avg_value", self.avg_value),
        ):
            if val is not None:
                d[key] = val
        if self.top_values:
            d["top_values"] = self.top_values
            d["values_note"] = (
                "以上是**实际存在**的取值，写 WHERE 时必须原样使用其中之一；"
                "不在此列表中的字面量不要用（TopN 之外可能还有其它取值，"
                "以 distinct_count 为准）。"
            )
        return d


# --------------------------------------------------------------------------- 策略

# 语义类型 → 画像策略。TECHNICAL/UNKNOWN 不画像：前者按语义就不该进业务查询，
# 后者拿不准——与全链路「拿不准即禁止」的保守取向一致。
_STRATEGY: dict[SemanticType, str] = {
    SemanticType.CATEGORICAL: "top_values",
    SemanticType.IDENTIFIER: "top_values",
    SemanticType.TEXTUAL: "top_values",
    SemanticType.MEASURE: "numeric_range",
    SemanticType.TEMPORAL: "temporal_range",
}


def strategy_for(semantic_type: SemanticType) -> str:
    return _STRATEGY.get(semantic_type, "skipped")


# --------------------------------------------------------------------------- SQL 生成


def _dialect_of(backend: str | None) -> str | None:
    if not backend:
        return None
    return _DIALECT_ALIASES.get(backend, backend)


def _render(select: exp.Select, backend: str | None) -> str:
    """按目标方言渲染并强制加标识符引号（``order`` 等保留字必须引起来）。"""
    dialect = _dialect_of(backend)
    try:
        return select.sql(dialect=dialect, identify=True)
    except Exception:  # noqa: BLE001 — 未知方言退回通用渲染，不因此放弃画像
        return select.sql(identify=True)


def _summary_select(obj: str, col: str, *, numeric: bool, distinct: bool) -> exp.Select:
    c = exp.column(col, table=obj)
    items = [
        exp.alias_(exp.Count(this=exp.Star()), f"{_A}total"),
        exp.alias_(exp.Count(this=c.copy()), f"{_A}non_null"),
    ]
    if distinct:
        items.append(
            exp.alias_(
                exp.Count(this=exp.Distinct(expressions=[c.copy()])), f"{_A}distinct"
            )
        )
    items.append(exp.alias_(exp.Min(this=c.copy()), f"{_A}min"))
    items.append(exp.alias_(exp.Max(this=c.copy()), f"{_A}max"))
    if numeric:
        items.append(exp.alias_(exp.Avg(this=c.copy()), f"{_A}avg"))
    return exp.select(*items).from_(exp.to_table(obj))


def _top_values_select(obj: str, col: str, limit: int) -> exp.Select:
    c = exp.column(col, table=obj)
    return (
        exp.select(
            exp.alias_(c.copy(), f"{_A}value"),
            exp.alias_(exp.Count(this=exp.Star()), f"{_A}freq"),
        )
        .from_(exp.to_table(obj))
        .group_by(c.copy())
        # 按聚合表达式本身排序，**不按输出别名**：别名在 ORDER BY 里是一个裸列引用，
        # 语义证明器会把它当成臆造字段而拒掉（自证时真撞上过）。
        .order_by(exp.Ordered(this=exp.Count(this=exp.Star()), desc=True))
        .limit(limit)
    )


def _proved(sql: str, proj: OntologyProjection, backend: str | None) -> bool:
    """自证：生成的画像 SQL 必须过语义证明器。

    证不过就是生成器 bug（引号、别名、聚合语义），必须可见——记 warning 并放弃该次
    画像，绝不把一条自己都证不过的 SQL 发给数据库。
    """
    try:
        verdict = prove_sql_sound(sql, proj, dialect=_dialect_of(backend))
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile sql prover error, skip profiling: %s", exc)
        return False
    if isinstance(verdict, SqlRejection):
        logger.warning(
            "生成的画像 SQL 未过语义证明（生成器 bug）：%s | %s | %s",
            verdict.code, verdict.message, sql,
        )
        return False
    return True


# --------------------------------------------------------------------------- 缓存


class _ProfileCache:
    """进程内 TTL 缓存。取值分布变化慢，没必要每问一次就打一次库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple, tuple[float, ColumnProfile]] = {}

    def get(self, key: tuple) -> ColumnProfile | None:
        ttl = max(0, int(getattr(env_settings, "agent_profile_cache_seconds", 900)))
        if ttl == 0:
            return None
        with self._lock:
            hit = self._data.get(key)
        if hit is None:
            return None
        ts, profile = hit
        if time.time() - ts > ttl:
            with self._lock:
                self._data.pop(key, None)
            return None
        return profile

    def put(self, key: tuple, profile: ColumnProfile) -> None:
        with self._lock:
            self._data[key] = (time.time(), profile)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CACHE = _ProfileCache()


def reset_cache() -> None:
    """清空画像缓存（测试用；发布新本体后也应调用）。"""
    _CACHE.clear()


# --------------------------------------------------------------------------- 入口


def profile_property(
    proj: OntologyProjection,
    obj: ObjView,
    prop: PropView,
    *,
    dsn: str | None,
    mapping: dict | None = None,
    backend: str | None = None,
    top_n: int | None = None,
    scope_key: str = "",
) -> ColumnProfile:
    """画像单个字段。``dsn`` 为空表示无可用数据源——返回 available=False 而非报错。

    ``scope_key`` 是缓存作用域（调用方传 ``ontology_id`` + 数据源标识）：本体重新发布或
    换了数据源，同名字段的取值分布就不是同一回事，必须落在不同的缓存键上。
    """
    from app.services import data_app_executor

    strategy = strategy_for(prop.semantic_type)
    base = {
        "object_name": obj.name,
        "property_name": prop.name,
        "semantic_type": prop.semantic_type.value,
        "strategy": strategy,
    }

    if strategy == "skipped":
        return ColumnProfile(
            **base,
            note=(
                f"语义类型 {prop.semantic_type.value} 的字段不做取值画像"
                "（技术字段不入业务查询；未知类型拿不准，不猜）。"
            ),
        )
    if not dsn:
        return ColumnProfile(
            **base, note="当前数据域无可执行数据源，无法读取真实取值。"
        )

    top_n = top_n or max(1, int(getattr(env_settings, "agent_profile_top_n", 20)))
    key = (scope_key, obj.name, prop.name, strategy, top_n)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    numeric = strategy == "numeric_range"
    want_top = strategy == "top_values"
    summary_sql = _render(
        _summary_select(obj.name, prop.name, numeric=numeric, distinct=want_top), backend
    )
    if not _proved(summary_sql, proj, backend):
        return ColumnProfile(**base, note="画像 SQL 未通过语义自证，已跳过。")

    try:
        cols, rows = data_app_executor.execute_sql(
            dsn=dsn, sql=summary_sql, limit=_PROFILE_ROW_LIMIT,
            mapping=mapping or None, timeout_seconds=_SQL_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — 画像失败不得拖垮问答
        logger.info("column profile query failed (%s.%s): %s", obj.name, prop.name, exc)
        return ColumnProfile(**base, note=f"读取真实取值失败：{str(exc)[:200]}")

    vals = _positional(cols, rows)
    idx = 0
    total = _as_int(vals[idx]) if idx < len(vals) else None
    idx += 1
    non_null = _as_int(vals[idx]) if idx < len(vals) else None
    idx += 1
    distinct = None
    if want_top:
        distinct = _as_int(vals[idx]) if idx < len(vals) else None
        idx += 1
    min_v = vals[idx] if idx < len(vals) else None
    idx += 1
    max_v = vals[idx] if idx < len(vals) else None
    idx += 1
    avg_v = vals[idx] if numeric and idx < len(vals) else None

    null_ratio = None
    if total:
        null_ratio = round((total - (non_null or 0)) / total, 4)

    top_values: list[dict] = []
    if want_top:
        top_sql = _render(_top_values_select(obj.name, prop.name, top_n), backend)
        if _proved(top_sql, proj, backend):
            try:
                tcols, trows = data_app_executor.execute_sql(
                    dsn=dsn, sql=top_sql, limit=max(top_n, 1),
                    mapping=mapping or None, timeout_seconds=_SQL_TIMEOUT,
                )
                vkey = tcols[0]["key"] if tcols else None
                fkey = tcols[1]["key"] if len(tcols) > 1 else None
                for r in trows:
                    top_values.append({
                        "value": r.get(vkey) if vkey else None,
                        "freq": _as_int(r.get(fkey)) if fkey else None,
                    })
            except Exception as exc:  # noqa: BLE001
                logger.info("top values query failed (%s.%s): %s", obj.name, prop.name, exc)

    profile = ColumnProfile(
        **base,
        available=True,
        row_count=total,
        non_null_count=non_null,
        null_ratio=null_ratio,
        distinct_count=distinct,
        top_values=top_values,
        min_value=_scalar(min_v),
        max_value=_scalar(max_v),
        avg_value=_scalar(avg_v),
    )
    _CACHE.put(key, profile)
    return profile


def _positional(cols: list[dict], rows: list[dict]) -> list[Any]:
    """按**列顺序**取第一行的值。

    不按名字取：``_apply_mapping`` 可能改写输出别名，位置才是稳定的。
    """
    if not rows:
        return []
    row = rows[0]
    return [row.get(c.get("key")) for c in cols]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scalar(value: Any):
    """把驱动返回的 Decimal/date 等转成 JSON 友好的标量。"""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


__all__ = ["ColumnProfile", "profile_property", "strategy_for", "reset_cache"]
