"""键族聚类：从**值**里找出跨表复用的实体键。

**为什么不是命名匹配**：真实源（jwsp 域实测）138 张表、0 个声明式主键、0 个声明式外键，
列名是拼音缩写。同一个「人员编号」在 40 张表里有 **52 种列名**（``bl_bh``、``zbr_bh``、
``chujingr_bh``…），任何命名规则都连不起来；但它们的值全是 ``RY00000183`` 这一种形状。
所以这里认的是**值形状**，不是列名。命名口径（``evidence_builder._guess_semantic_type``）
在拼音列上整体失效——``blzz_sj``（笔录制作时间）不含 time/date/_at，会被判成 attribute。

**值形状的代价**：签名按字母段/数字段拆，所以它只认「有结构」的键。md5/uuid 这类
不透明标识符字符随机交错，每个样例拆出来都是不同的签名，整类会撞「形状不稳定」闸门
消失——jwsp 的 ``gid``（设备标识，46 张表）就是这么丢的。:func:`value_shape` 对这两
种形态单独整体成形（``H<32>``/``U<36>``）。

**为什么不是两两配对**：先配对再过滤的第一版漏斗在 jwsp 上产出 26178 对候选，绝大多数是
时间戳互相配对——每个时间戳都唯一，「像主键」的闸门拦不住它们，而所有时间列共享同一个形状。
本模块的顺序是**先按值语义把不能当 JOIN 键的列剔掉，再聚族**：4960 字段 → 479 候选列 →
14 个族（实测）。族数比对数少三个数量级，这是后续能把整域一次交给 LLM 判定的前提。

**基数是算出来的，不是猜的**：``distinct/rows`` 直接给出每一端是主端还是引用端。
``evidence_builder`` 现有四处关系构造的基数全是硬编码常量（``:293``/``:327``/``:356``/``:629``），
本模块的 :func:`cardinality_between` 是全仓第一处真正按数据算基数的地方。

本模块**纯函数**、不碰 DataHub、不碰数据库，只吃 ``DatasetInput``——便于用真实形态的数据做用例。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.schemas import DatasetInput

# --------------------------------------------------------------------------- 闸门阈值

#: 闸门 1：同一列名出现在超过这个比例的表里 → 判为跨表过于普遍，不可能是有区分度的实体键。
#: 实测 jwsp：``row_id`` 100%、``logdate`` 99%、``sj_ly`` 61%——全是 ETL/审计列。
#: 用比例而非绝对数，小域才不会被一刀切；再加 :data:`UBIQUITOUS_MIN_TABLES` 兜底，
#: 免得 10 张表的域里「出现在 3 张表」就被误杀。
UBIQUITOUS_RATIO = 0.25
UBIQUITOUS_MIN_TABLES = 8

#: 闸门 1 的豁免线：``distinct/rows`` 达到此值的列不受「跨表过于普遍」约束。
#:
#: **为什么要开这个口子**：出现在多少张表里只是「没有区分度」的**代理指标**，而区分度
#: 本身是能直接量出来的。域级实体主键天然无处不在——jwsp 的 ``gid``（设备标识）在
#: 44/138 张表里，比阈值 34.5 高，于是这个域最重要的那根关联主干被当成 ETL 列丢掉了。
#: 实测这 9 个「普遍列」的 ``distinct/rows``：``sj_ly`` 0.004、``local_code`` 0.016，
#: 而 ``gid`` 0.745、``xm`` 0.799——码值与实体键之间隔着两个数量级的空档，取 0.5 落在空档里。
#: 语义上这条线是说：**取值多到过半行各不相同的列，携带的是逐行身份而不是类目标签。**
#:
#: 豁免掉的不等于放行——``row_id``（形状 ``N<1>`` 撞短码闸）、``etl_sj``/``rk_sj``/
#: ``logdate``（时间闸）、``xm``（形状不稳定闸）实测全部由其余五道闸各自拦下。把普遍性
#: 闸门整个关掉重跑全域，9 个普遍列里只有 ``local_code`` 能活到聚族，其余 8 个自己就死了。
#: 残留风险：``row_id`` 若在别的域里样例恰好是 4 位数（``N<4>``，躲开短码闸）会漏出来，
#: 届时它会聚成一个跨表巨族，交给 LLM 判定层挡——这是刻意接受的取舍。
UBIQUITOUS_EXEMPT_RATIO = 0.5

#: 闸门 6：distinct 低于此值的列当枚举/码值，不当实体键。
MIN_DISTINCT = 5

#: 近唯一（主端）判定线：distinct/rows 达到此值即认为该列在本表内唯一。
#: 不取 1.0——真实表有空值与脏数据，profiling 的 distinct 也可能带误差。
NEAR_UNIQUE_RATIO = 0.98

#: 一个族至少要跨这么多张表才有意义（同表内复现不是关系）。
MIN_FAMILY_TABLES = 2

#: 族覆盖域内过半表时，形状信号更可能是公共码值/技术字段，降低模型结论的可信度。
#: 所有候选本来就必须人工确认；这里额外保留可审计的风险提示。
WIDE_FAMILY_RATIO = 0.5
WIDE_FAMILY_CONFIDENCE_FACTOR = 0.8

# --------------------------------------------------------------------------- 形状

#: 字母段与数字段一次扫完。**不能分两次 sub**——先替字母得到 ``A<2>``，再替数字会把
#: 占位符里的 ``2`` 也当数字段替掉，变成 ``A<N<1>>``。
_RUN = re.compile(r"[A-Za-z]+|[0-9]+")
_PLACEHOLDER = re.compile(r"[ANHU]<\d+>")
#: 形状层面的日期模板：``N<4>-N<2>-N<2>``、``N<4>/N<2>/N<2>``、含 ``N<2>:N<2>:N<2>`` 的时间。
_DATE_SHAPE = re.compile(r"^N<4>[-/]N<2>[-/]N<2>|N<2>:N<2>:N<2>")
#: 值层面的紧凑日期：``20260907`` / ``20260907143000``。
_COMPACT_DATE = re.compile(r"^(19|20)\d{6}(\d{6})?$")
#: 形状层面的小数：坐标、金额。
_DECIMAL_SHAPE = re.compile(r"^N<\d+>\.N<\d+>$")
#: 低基数码值形状：1~2 位纯数字。
_SHORT_CODE_SHAPE = re.compile(r"^N<[12]>$")
#: 列名层面的度量/坐标后缀。
_MEASURE_SUFFIX = re.compile(r"(_cnt|_num|_amt|_je|_sl|_count|_jd|_wd|lnglat)$")
#: 物理类型里的时间关键字。
_TEMPORAL_TYPE = re.compile(r"DATE|TIME|TIMESTAMP")

#: 不透明标识符：UUID 与十六进制摘要（md5/sha1/sha256 及同形态的 id）。见 :func:`value_shape`。
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
#: 两个前瞻要求**同时含字母和数字**——纯数字的长串（``3101042026457051``)是号码不是摘要，
#: 必须继续走 ``N<16>``；只有字母的长串同理。长度下限 16 挡住 ``ABC123`` 这类短码。
_HEX_DIGEST = re.compile(r"^(?=.*[a-f])(?=.*[0-9])[0-9a-f]{16,}$", re.IGNORECASE)

SHAPE_MAX_LENGTH = 32


def value_shape(value: str) -> str:
    """值 → 形状签名：字母段记 ``A<n>``、数字段记 ``N<n>``，其余字符原样。

    ``RY00000183`` → ``A<2>N<8>``；``2026-09-07 14:30:00`` → ``N<4>-N<2>-N<2> N<2>:N<2>:N<2>``；
    ``汉族`` → ``汉族``（**不含占位符**，这正是常量/枚举的判据，见 :func:`_is_constant_shape`）。

    **不透明标识符整体成形，不按段拆**：md5/uuid 这类值的字母数字是随机交错的，逐段拆出来
    每个样例都是不同的签名——``66e31ba2…`` 给 ``N<2>A<1>N<2>A<2>…``、``e92d66fa…`` 给
    ``A<1>N<2>A<1>N<2>…``——于是撞「形状不稳定」闸门被整列剔掉，而且就算放一个样例进来，
    ``family_id`` 是形状的哈希，两张表的 md5 也永远落不进同一个族。实测 jwsp 的 ``gid``
    （设备标识，44 张表的关联主干）就是这么整类不可见的。归一成 ``H<32>`` 后它才能聚族。
    """
    if _UUID.match(value):
        return f"U<{len(value)}>"
    if _HEX_DIGEST.match(value):
        return f"H<{len(value)}>"

    def render(match: re.Match[str]) -> str:
        token = match.group()
        return f"{'A' if token[0].isalpha() else 'N'}<{len(token)}>"

    return _RUN.sub(render, value)[:SHAPE_MAX_LENGTH]


def _is_constant_shape(shape: str) -> bool:
    """形状里一个占位符都没有 → 5 个样例是同一个字面量，是枚举不是键。

    实测 jwsp 用这一条干掉了「居民身份证」「中国」「汉族」三个噪声族，不需要词典。
    """
    return not _PLACEHOLDER.search(shape)


def _is_temporal(data_type: str | None, shape: str, samples: list[str]) -> bool:
    if _TEMPORAL_TYPE.search((data_type or "").upper()):
        return True
    if _DATE_SHAPE.search(shape):
        return True
    return any(_COMPACT_DATE.match(s) for s in samples)


def _is_measure_like(column: str, shape: str) -> bool:
    return bool(_DECIMAL_SHAPE.match(shape) or _MEASURE_SUFFIX.search(column.lower()))


# --------------------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class KeyColumn:
    """一个候选键列，带它在本表内的区分度。"""

    table: str
    column: str
    data_type: str | None
    distinct: int
    rows: int
    samples: tuple[str, ...]

    @property
    def distinct_ratio(self) -> float:
        """``distinct / rows``。rows 为 0 时返回 0（调用方已保证 rows > 0 才进来）。"""
        return self.distinct / self.rows if self.rows else 0.0

    @property
    def near_unique(self) -> bool:
        """本表内近似唯一 → 这一端像主键/主数据端。"""
        return self.distinct_ratio >= NEAR_UNIQUE_RATIO

    @property
    def ref(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class KeyFamily:
    """一组值形状相同、跨 ≥2 张表的候选键列——即「同一个实体键的所有出现位置」。"""

    id: str
    value_shape: str
    members: tuple[KeyColumn, ...]

    @property
    def tables(self) -> tuple[str, ...]:
        seen: list[str] = []
        for member in self.members:
            if member.table not in seen:
                seen.append(member.table)
        return tuple(seen)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(sorted({member.column for member in self.members}))

    @property
    def anchors(self) -> tuple[KeyColumn, ...]:
        """近唯一的成员——本域里这个实体的候选主表。空 = 该实体没有主数据表。"""
        return tuple(member for member in self.members if member.near_unique)

    @property
    def sample_values(self) -> tuple[str, ...]:
        """去重后的代表值，供人和 LLM 一眼认出这是什么键。"""
        seen: list[str] = []
        for member in self.members:
            for value in member.samples:
                if value not in seen:
                    seen.append(value)
                if len(seen) >= 10:
                    return tuple(seen)
        return tuple(seen)


@dataclass
class DropStats:
    """各闸门剔除了多少列——不摊开就没法解释「为什么我的键不见了」。"""

    ubiquitous: int = 0
    no_profile: int = 0
    unstable_shape: int = 0
    temporal: int = 0
    measure_like: int = 0
    constant_or_low_cardinality: int = 0

    @property
    def total(self) -> int:
        return (
            self.ubiquitous
            + self.no_profile
            + self.unstable_shape
            + self.temporal
            + self.measure_like
            + self.constant_or_low_cardinality
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "跨表过于普遍": self.ubiquitous,
            "无取值画像": self.no_profile,
            "形状不稳定": self.unstable_shape,
            "时间类": self.temporal,
            "度量/坐标类": self.measure_like,
            "常量或低基数": self.constant_or_low_cardinality,
        }


# --------------------------------------------------------------------------- 闸门 + 聚族


def _ubiquitous_columns(datasets: list[DatasetInput]) -> set[str]:
    counts: dict[str, set[str]] = {}
    for dataset in datasets:
        for field in dataset.fields:
            counts.setdefault(field.name.lower(), set()).add(dataset.name)
    ceiling = max(UBIQUITOUS_MIN_TABLES, len(datasets) * UBIQUITOUS_RATIO)
    return {name for name, tables in counts.items() if len(tables) > ceiling}


def _carries_row_identity(distinct: int, rows: int) -> bool:
    """取值多到过半行各不相同 → 这列携带逐行身份，不是类目标签。

    :data:`UBIQUITOUS_EXEMPT_RATIO` 的判据，只用在闸门 1 的豁免上。
    """
    return bool(rows) and distinct / rows >= UBIQUITOUS_EXEMPT_RATIO


def candidate_key_columns(
    datasets: list[DatasetInput],
    *,
    ubiquity_reference: list[DatasetInput] | None = None,
) -> tuple[list[KeyColumn], DropStats]:
    """六道闸门：字段全集 → 候选键列。返回 (存活列, 剔除统计)。

    ``ubiquity_reference`` 是「跨表过于普遍」这道闸门的参照系，缺省就是 ``datasets``
    自己。**只在按子集聚族时必须显式传全域**：这道闸门的阈值是
    ``max(8, 表数 × 0.25)``——734 张表的域里阈值是 183，``ry_bh`` 出现在 40 张表照样
    活着；换成画布上选中的 20 张表，阈值掉到 8，同一个 ``ry_bh`` 反而被当成「太普遍」
    剔掉。普遍与否是**域级事实**，不该随人选了几张表而变。

    普遍列里区分度够高的按 :data:`UBIQUITOUS_EXEMPT_RATIO` 豁免——域级实体主键本来就
    到处都是，不能因为无处不在就判它没有区分度。
    """
    stats = DropStats()
    ubiquitous = _ubiquitous_columns(ubiquity_reference or datasets)
    kept: list[KeyColumn] = []

    for dataset in datasets:
        rows = dataset.row_count or 0
        for field in dataset.fields:
            samples = [s for s in (field.sample_values or []) if s]
            distinct = field.unique_count or 0

            if field.name.lower() in ubiquitous and not _carries_row_identity(
                distinct, rows
            ):
                stats.ubiquitous += 1
                continue
            if not samples or not rows:
                stats.no_profile += 1
                continue

            shapes = {value_shape(s) for s in samples}
            if len(shapes) != 1:
                # 形状都对不齐的列（自由文本、混合格式）不可能是稳定的键空间。
                stats.unstable_shape += 1
                continue
            shape = next(iter(shapes))

            if _is_temporal(field.data_type, shape, samples):
                stats.temporal += 1
                continue
            if _is_measure_like(field.name, shape):
                stats.measure_like += 1
                continue
            if (
                _is_constant_shape(shape)
                or _SHORT_CODE_SHAPE.match(shape)
                or distinct < MIN_DISTINCT
            ):
                stats.constant_or_low_cardinality += 1
                continue

            kept.append(
                KeyColumn(
                    table=dataset.name,
                    column=field.name,
                    data_type=field.data_type,
                    distinct=distinct,
                    rows=rows,
                    samples=tuple(samples),
                )
            )
    return kept, stats


def _family_id(shape: str) -> str:
    return "f_" + hashlib.sha1(shape.encode("utf-8")).hexdigest()[:10]


def build_key_families(
    datasets: list[DatasetInput],
    *,
    ubiquity_reference: list[DatasetInput] | None = None,
) -> tuple[list[KeyFamily], DropStats]:
    """域内所有表 → 键族清单（按成员数降序）+ 剔除统计。

    只做机械判定：**这里不判断族是不是真实体键**——``N<6>``/``310115`` 是行政区划码而
    ``A<2>N<8>``/``RY00000183`` 是人员编号，这层判定要看业务语义，留给 LLM（P2）与人。

    ``ubiquity_reference`` 见 :func:`candidate_key_columns`——按子集聚族时传全域。
    """
    columns, stats = candidate_key_columns(
        datasets, ubiquity_reference=ubiquity_reference
    )

    by_shape: dict[str, list[KeyColumn]] = {}
    for column in columns:
        shape = value_shape(column.samples[0])
        by_shape.setdefault(shape, []).append(column)

    families = [
        KeyFamily(id=_family_id(shape), value_shape=shape, members=tuple(members))
        for shape, members in by_shape.items()
        if len({m.table for m in members}) >= MIN_FAMILY_TABLES
    ]
    families.sort(key=lambda f: (-len(f.members), f.value_shape))
    return families, stats


# --------------------------------------------------------------------------- 基数


def cardinality_between(source: KeyColumn, target: KeyColumn) -> str:
    """两端区分度 → 基数字面量（可直接喂 ``normalize_cardinality``）。

    - 两端都近唯一 → ``one_to_one``
    - 只有 target 近唯一 → ``many_to_one``（source 是引用端）
    - 只有 source 近唯一 → ``one_to_many``
    - 都不唯一 → ``many_to_many``（经由共享实体；实测 jwsp 的族多数是这一类，
      因为该域根本没有人员表/案件表这样的主表）
    """
    if source.near_unique and target.near_unique:
        return "one_to_one"
    if target.near_unique:
        return "many_to_one"
    if source.near_unique:
        return "one_to_many"
    return "many_to_many"


def orient_reference(a: KeyColumn, b: KeyColumn) -> tuple[KeyColumn, KeyColumn]:
    """把一对键列摆成 (引用端, 被引用端)。

    口径与 ``evidence_builder._orient_relation`` 一致——**明细指向主数据**，只是这里有
    更好的判据可用：先比区分度（近唯一的一端是主数据），再比行数（行多的是明细），
    最后按 ``表.列`` 字典序兜底，保证同一份输入永远给同一个方向。
    """
    if a.near_unique != b.near_unique:
        return (b, a) if a.near_unique else (a, b)
    if a.rows != b.rows:
        return (a, b) if a.rows > b.rows else (b, a)
    return (a, b) if a.ref <= b.ref else (b, a)
