"""``services/source_ref`` 的判读 —— 本体两种来源的分界线。

这些用例同时钉住一件容易被"顺手统一"掉的事：``_extract_dataset_name`` 的宽松回吐是**故意**的
（它服务的是 DataHub GraphQL 响应解析），不能改；要严格语义的地方用 ``source_table_of``。
"""

import pytest

from app.connectors.datahub import _extract_dataset_name
from app.services.source_ref import (
    has_physical_source,
    is_dataset_urn,
    is_manual_source_ref,
    manual_dialect_of,
    provenance_of,
    source_platform_of,
    source_table_of,
)

# 真实形态各一：采集来的 URN，与 ManualCreationService 产的人工引用。
URN = "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp.tabCustomer,PROD)"
MANUAL = "manual:mysql:customer_order"


@pytest.mark.parametrize(
    "ref,expected",
    [
        (URN, "datahub"),
        (MANUAL, "manual"),
        (None, "none"),
        ("", "none"),
        ("随便一个串", "none"),
    ],
)
def test_provenance_of(ref, expected):
    assert provenance_of(ref) == expected


def test_urn_is_parsed_into_table_and_platform():
    assert is_dataset_urn(URN)
    assert not is_manual_source_ref(URN)
    assert source_table_of(URN) == "erp.tabCustomer"
    assert source_platform_of(URN) == "mariadb"
    assert has_physical_source(URN)


def test_manual_ref_yields_no_source_table():
    """人工建模对象没有物理源表——这正是它只能物化、不能同步的原因。"""
    assert is_manual_source_ref(MANUAL)
    assert not is_dataset_urn(MANUAL)
    assert source_table_of(MANUAL) is None
    assert source_platform_of(MANUAL) is None
    assert not has_physical_source(MANUAL)
    assert manual_dialect_of(MANUAL) == "mysql"


@pytest.mark.parametrize("ref", [None, "", "垃圾串", "urn:li:dataset:不带括号"])
def test_garbage_never_becomes_a_table_name(ref):
    assert source_table_of(ref) is None
    assert source_platform_of(ref) is None
    assert not has_physical_source(ref)


def test_loose_passthrough_of_extract_dataset_name_is_intentional():
    """``_extract_dataset_name`` 对非 URN 原样回吐——记录为**故意**行为，别去"修"它。

    它的三个调用方解析的是 DataHub GraphQL 响应（``entity.name or _extract_dataset_name(urn)``），
    回吐是对的。危险的只是拿它解析 ``ObjectType.source_ref``；那种地方改用 ``source_table_of``。
    """
    assert _extract_dataset_name(MANUAL) == MANUAL  # 回吐：会被误当表名
    assert source_table_of(MANUAL) is None  # 严格版：逼调用方处理
