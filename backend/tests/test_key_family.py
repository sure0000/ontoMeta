"""键族聚类测试。

钉住的行为（每条都对着真实 jwsp 域的形态写，不是编的）：

1. 形状签名一次扫完——分两次替换会把 ``A<2>`` 里的 2 也当数字段，变成 ``A<N<1>>``；
2. 六道闸门各自的剔除对象：跨表过于普遍、无画像、形状不稳定、时间、度量、常量/低基数；
3. 常量族（``汉族``/``中国``/``居民身份证``）必须被「形状里没有占位符」这一条清掉；
4. 时间戳列即使 distinct/rows = 1.0 也不能当键——这是第一版漏斗炸成 26178 对的根因；
5. 基数按 distinct/rows 算，不是硬编码；
6. 方向按区分度定：近唯一的一端是被引用的主数据端。
"""

from __future__ import annotations

from app.schemas import DatasetInput, FieldInput
from app.services.key_family import (
    KeyColumn,
    build_key_families,
    candidate_key_columns,
    cardinality_between,
    orient_reference,
    value_shape,
)


def _field(name, samples, *, distinct=100, data_type="VARCHAR(255)"):
    return FieldInput(
        name=name,
        data_type=data_type,
        unique_count=distinct,
        sample_values=samples,
    )


def _dataset(name, fields, *, rows=1000):
    return DatasetInput(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:mysql,{name},PROD)",
        name=name,
        row_count=rows,
        fields=fields,
    )


def _column(table, column, distinct, rows):
    return KeyColumn(
        table=table,
        column=column,
        data_type="VARCHAR(255)",
        distinct=distinct,
        rows=rows,
        samples=("RY00000183",),
    )


# --------------------------------------------------------------------------- 形状


def test_value_shape_does_not_rewrite_its_own_placeholders():
    """``RY00000183`` → ``A<2>N<8>``；先替字母再替数字会写成 ``A<N<1>>N<8>``。"""
    assert value_shape("RY00000183") == "A<2>N<8>"
    assert value_shape("3101042026457051") == "N<16>"
    assert value_shape("2026-09-07 14:30:00") == "N<4>-N<2>-N<2> N<2>:N<2>:N<2>"


def test_constant_value_keeps_literal_shape():
    """枚举值的形状就是它自己——没有占位符，这正是判枚举的依据。"""
    assert value_shape("汉族") == "汉族"
    assert value_shape("居民身份证") == "居民身份证"


# --------------------------------------------------------------------------- 闸门


def test_constant_column_is_dropped_as_enum():
    datasets = [
        _dataset(f"t{i}", [_field("mz", ["汉族", "汉族", "汉族"], distinct=9)])
        for i in range(4)
    ]
    kept, stats = candidate_key_columns(datasets)
    assert kept == []
    assert stats.constant_or_low_cardinality == 4


def test_unique_timestamp_column_is_dropped_despite_being_unique():
    """时间戳每行都唯一，区分度 1.0——但它不是键。第一版漏斗就是被这类列撑爆的。"""
    stamps = ["2026-09-07 14:30:00", "2026-09-07 14:31:00", "2026-09-08 09:00:00"]
    datasets = [
        _dataset(f"t{i}", [_field("blzz_sj", stamps, distinct=1000)]) for i in range(3)
    ]
    kept, stats = candidate_key_columns(datasets)
    assert kept == []
    assert stats.temporal == 3


def test_temporal_detected_by_physical_type_even_when_shape_is_opaque():
    datasets = [
        _dataset(f"t{i}", [_field("rk_sj", ["1757000000"], distinct=900, data_type="TIMESTAMP")])
        for i in range(3)
    ]
    _, stats = candidate_key_columns(datasets)
    assert stats.temporal == 3


def test_ubiquitous_column_is_dropped():
    """出现在绝大多数表里的列是 ETL/审计列，不可能是有区分度的实体键。"""
    datasets = [
        _dataset(
            f"t{i}",
            [
                _field("row_id", ["1", "2", "3"], distinct=1000),
                _field("logdate", ["20260907"], distinct=30),
            ],
        )
        for i in range(12)
    ]
    _, stats = candidate_key_columns(datasets)
    assert stats.ubiquitous == 24


def test_unstable_shape_is_dropped():
    """自由文本的形状对不齐，不构成稳定键空间。"""
    datasets = [
        _dataset(f"t{i}", [_field("bz", ["现场处置", "已完成核实", "待核"], distinct=500)])
        for i in range(3)
    ]
    _, stats = candidate_key_columns(datasets)
    assert stats.unstable_shape == 3


def test_measure_and_coordinate_columns_are_dropped():
    datasets = [
        _dataset(
            f"t{i}",
            [
                _field("af_jd", ["121.473701"], distinct=900),
                _field("bs_cnt", ["12"], distinct=900),
            ],
        )
        for i in range(3)
    ]
    _, stats = candidate_key_columns(datasets)
    assert stats.measure_like == 6


def test_no_profile_column_is_dropped():
    datasets = [_dataset(f"t{i}", [_field("ry_bh", [], distinct=0)]) for i in range(3)]
    _, stats = candidate_key_columns(datasets)
    assert stats.no_profile == 3


# --------------------------------------------------------------------------- 聚族


def test_same_value_shape_across_tables_forms_one_family_regardless_of_column_name():
    """52 种列名装同一套 ``RY%08d``——命名匹配连不起来，值形状能。"""
    datasets = [
        _dataset("aj_bl_zl", [_field("bl_bh", ["RY00000183", "RY00000330"], distinct=800)]),
        _dataset("aj_cyry_ql", [_field("ry_bh", ["RY00002417", "RY00002039"], distinct=700)]),
        _dataset("bj_zdry_zl", [_field("chujingr_bh", ["RY00001784"], distinct=600)]),
    ]
    families, _ = build_key_families(datasets)

    assert len(families) == 1
    family = families[0]
    assert family.value_shape == "A<2>N<8>"
    assert set(family.column_names) == {"bl_bh", "ry_bh", "chujingr_bh"}
    assert len(family.tables) == 3


def test_single_table_recurrence_is_not_a_family():
    """同一张表里出现两次不是关系，族至少要跨两张表。"""
    datasets = [
        _dataset(
            "aj_cyry_ql",
            [
                _field("zbr_bh", ["RY00000570"], distinct=800),
                _field("xbr_bh", ["RY00002740"], distinct=800),
            ],
        )
    ]
    families, _ = build_key_families(datasets)
    assert families == []


def test_family_anchors_flag_the_master_table():
    """近唯一的成员就是这个实体的候选主表；一个都没有 = 本域缺主数据表。"""
    datasets = [
        _dataset("ry_zl", [_field("ry_bh", ["RY00000183"], distinct=1000)], rows=1000),
        _dataset("aj_bl_zl", [_field("bl_bh", ["RY00000330"], distinct=700)], rows=1571),
    ]
    families, _ = build_key_families(datasets)

    assert [a.ref for a in families[0].anchors] == ["ry_zl.ry_bh"]


# --------------------------------------------------------------------------- 基数与方向


def test_cardinality_is_computed_from_distinctness():
    master = _column("ry_zl", "ry_bh", distinct=1000, rows=1000)
    detail = _column("aj_bl_zl", "bl_bh", distinct=700, rows=1571)

    assert cardinality_between(detail, master) == "many_to_one"
    assert cardinality_between(master, detail) == "one_to_many"
    assert cardinality_between(master, master) == "one_to_one"
    assert cardinality_between(detail, detail) == "many_to_many"


def test_orientation_puts_the_near_unique_side_as_target():
    master = _column("ry_zl", "ry_bh", distinct=1000, rows=1000)
    detail = _column("aj_bl_zl", "bl_bh", distinct=700, rows=1571)

    assert orient_reference(detail, master) == (detail, master)
    assert orient_reference(master, detail) == (detail, master)


def test_orientation_falls_back_to_row_count_then_name():
    """两端都不唯一时按行数（明细行多）；行数也相同则按名字，保证结果稳定。"""
    big = _column("b_table", "k", distinct=100, rows=900)
    small = _column("a_table", "k", distinct=100, rows=300)
    assert orient_reference(small, big) == (big, small)

    left = _column("a_table", "k", distinct=100, rows=500)
    right = _column("b_table", "k", distinct=100, rows=500)
    assert orient_reference(right, left) == (left, right)
