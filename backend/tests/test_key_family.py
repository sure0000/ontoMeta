"""键族聚类测试。

钉住的行为（每条都对着真实 jwsp 域的形态写，不是编的）：

1. 形状签名一次扫完——分两次替换会把 ``A<2>`` 里的 2 也当数字段，变成 ``A<N<1>>``；
2. 不透明标识符（md5/uuid）整体成形，不按字母数字段拆——拆了每个样例签名都不同；
3. 六道闸门各自的剔除对象：跨表过于普遍、无画像、形状不稳定、时间、度量、常量/低基数；
4. 闸门 1 对高区分度列的豁免：域级实体主键天然无处不在，不能因此判它没区分度；
5. 常量族（``汉族``/``中国``/``居民身份证``）必须被「形状里没有占位符」这一条清掉；
6. 时间戳列即使 distinct/rows = 1.0 也不能当键——这是第一版漏斗炸成 26178 对的根因；
7. 基数按 distinct/rows 算，不是硬编码；
8. 方向按区分度定：近唯一的一端是被引用的主数据端。
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


def test_opaque_identifier_gets_one_whole_shape():
    """md5/uuid 按段拆会给出每个样例都不同的签名，必须整体成形。

    这是 ``gid`` 整类不可见的根因：逐段拆 ``66e31ba2…`` 给 ``N<2>A<1>N<2>A<2>…``、
    ``e92d66fa…`` 给 ``A<1>N<2>A<1>N<2>…``，5 个样例 5 种形状 → 撞「形状不稳定」闸门。
    """
    assert value_shape("66e31ba2a55eb1e8ff66f0be0db24af8") == "H<32>"
    assert value_shape("e92d66fa5cbd7141e9562878ce4bd54b") == "H<32>"
    assert value_shape("da39a3ee5e6b4b0d3255bfef95601890afd80709") == "H<40>"
    assert value_shape("550e8400-e29b-41d4-a716-446655440000") == "U<36>"
    assert value_shape("6C6F0B36-8D6B-44F5-B004-5EFB3D22314E") == "U<36>"


def test_opaque_shape_is_not_mistaken_for_an_enum():
    """``H<32>`` 里没有 ``A``/``N``，占位符正则若不认它，整族会被当常量清掉。"""
    datasets = [
        _dataset(
            f"t{i}",
            [_field("gid", ["66e31ba2a55eb1e8ff66f0be0db24af8"], distinct=900)],
            rows=1200,
        )
        for i in range(3)
    ]
    kept, stats = candidate_key_columns(datasets)

    assert stats.constant_or_low_cardinality == 0
    assert len(kept) == 3


def test_long_digit_run_is_not_a_digest():
    """纯数字长串是号码不是摘要——十六进制归一必须同时要求含字母和数字。"""
    assert value_shape("3101042026457051") == "N<16>"
    assert value_shape("RY00000183") == "A<2>N<8>"
    assert value_shape("ABC123") == "A<3>N<3>"


def test_hex_columns_across_tables_form_one_family():
    """两张表的 md5 值形状归一后才聚得成族——``family_id`` 是形状的哈希。"""
    datasets = [
        _dataset(
            "dmp_device_portrait_merge_center",
            [_field("gid", ["66e31ba2a55eb1e8ff66f0be0db24af8"], distinct=915)],
            rows=1215,
        ),
        _dataset(
            "dmp_gid_ids",
            [_field("gid", ["53583bcbd9ff6a63e6d88a7c0dce63b4"], distinct=1016)],
            rows=1413,
        ),
    ]
    families, _ = build_key_families(datasets)

    assert len(families) == 1
    assert families[0].value_shape == "H<32>"
    assert len(families[0].tables) == 2


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


def test_ubiquitous_low_cardinality_column_is_dropped():
    """出现在绝大多数表里、且取值来回重复的列是码值/审计列，不是实体键。"""
    datasets = [
        _dataset(
            f"t{i}",
            [
                _field("local_code", ["370102", "110105"], distinct=20),
                _field("sj_ly", ["公安内网采集", "互联网抓取"], distinct=5),
            ],
        )
        for i in range(12)
    ]
    _, stats = candidate_key_columns(datasets)
    assert stats.ubiquitous == 24


def test_ubiquitous_column_is_exempt_when_it_carries_row_identity():
    """域级实体主键天然无处不在——不能因为出现在很多表里就判它没区分度。

    jwsp 的 ``gid``（设备标识）在 44/138 张表里，超过阈值 34.5，改前被当成 ETL 列整列
    剔掉，于是这个域最重要的那根关联主干在画布上一根线都连不出来。
    """
    digests = [
        "66e31ba2a55eb1e8ff66f0be0db24af8",
        "e92d66fa5cbd7141e9562878ce4bd54b",
    ]
    datasets = [
        _dataset(f"t{i}", [_field("gid", digests, distinct=900)], rows=1200)
        for i in range(12)
    ]
    kept, stats = candidate_key_columns(datasets)

    assert stats.ubiquitous == 0
    assert len(kept) == 12


def test_row_id_survives_ubiquity_exemption_but_dies_to_the_short_code_gate():
    """豁免不等于放行：``row_id`` 区分度 1.0 会绕过闸门 1，但形状 ``N<1>`` 撞短码闸。

    实测把闸门 1 整个关掉重跑 jwsp 全域，9 个「普遍列」里只有 ``local_code`` 能活到聚族，
    其余 8 个都由其余五道闸各自拦下——这是豁免敢开的前提。
    """
    datasets = [
        _dataset(f"t{i}", [_field("row_id", ["1", "2", "3"], distinct=1000)])
        for i in range(12)
    ]
    _, stats = candidate_key_columns(datasets)

    assert stats.ubiquitous == 0
    assert stats.constant_or_low_cardinality == 12


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
