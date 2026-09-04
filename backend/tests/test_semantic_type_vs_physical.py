"""语义类型不得与采集到的物理类型矛盾。

由来：语义类型**决定目标列的物理类型**（datetime→TIMESTAMP、amount→DECIMAL、
flag→BOOLEAN，见各 Dialect Adapter 的 map_type）。而推断只看字段名时，ERP 域里
``date_format``（VARCHAR，存 "dd-mm-yyyy"）被判 datetime、``_user_tags``（TEXT，
存 ``["a"]``）被判 flag——物化出来的表装不下自己的源数据，搬运每次挂在类型转换上。
真实 ERP 本体里这样的属性有 495 个、涉及 404/734 个对象。

**名字是线索，物理类型与样例是事实**：物理是文本时，只有样例值确实长那样才认。
"""

from __future__ import annotations

import pytest

from app.schemas import FieldInput
from app.services.evidence_builder import EvidenceBuilder
from app.services.warehouse_generator import _safe_projection_semantic_type


def _infer(**kwargs) -> str:
    return EvidenceBuilder()._infer_semantic_type(FieldInput(**kwargs))


@pytest.mark.parametrize(
    "name,data_type,expected",
    [
        # 物理就是时间/数值/布尔 → 名字猜的算数
        ("created_at", "DATETIME(6)", "datetime"),
        ("total_amount", "DECIMAL(21, 9)", "amount"),
        ("is_group", "TINYINT(1)", "flag"),
        # 数值字段的时间命名不足以证明 epoch 秒，避免生成 Flink 非法转换
        ("event_time", "BIGINT", "attribute"),
        # 物理是文本、又没有样例 → 不认，退回 attribute
        ("date_format", "VARCHAR(140)", "attribute"),
        ("time_format", "VARCHAR(140)", "attribute"),
        ("_user_tags", "TEXT", "attribute"),
        ("base_amount", "VARCHAR(140)", "attribute"),
        # 不决定物理类型的语义不受影响
        ("status", "VARCHAR(140)", "category"),
        ("customer_id", "VARCHAR(140)", "identifier"),
        ("remark", "TEXT", "attribute"),
    ],
)
def test_text_physical_type_vetoes_name_guess(name, data_type, expected):
    assert _infer(name=name, data_type=data_type) == expected


def test_samples_rescue_a_genuine_date_stored_as_text():
    """源库把日期存进字符串是常见的，此时判 datetime 让目标列升级成 TIMESTAMP 是对的。"""
    assert _infer(
        name="order_date", data_type="VARCHAR(64)",
        sample_values=["2024-01-01", "2024-02-03"],
    ) == "datetime"


def test_samples_do_not_rescue_a_format_string():
    """"dd-mm-yyyy" 这种格式串名字里也有 date，但它不是日期。"""
    assert _infer(
        name="date_format", data_type="VARCHAR(140)",
        sample_values=["dd-mm-yyyy", "mm/dd/yyyy"],
    ) == "attribute"


def test_samples_rescue_boolean_like_text():
    assert _infer(name="is_active", data_type="VARCHAR(8)",
                  sample_values=["1", "0"]) == "flag"
    assert _infer(name="_user_tags", data_type="TEXT",
                  sample_values=['["vip"]', "[]"]) == "attribute"


def test_samples_rescue_amount_stored_as_text():
    assert _infer(name="grand_amount", data_type="VARCHAR(32)",
                  sample_values=["1200.50", "88"]) == "amount"


@pytest.mark.parametrize("data_type", ["TINYINT(4)", "BIGINT", "DECIMAL(21, 9)"])
def test_numeric_datetime_guess_is_safe_in_physical_projection(data_type):
    assert _safe_projection_semantic_type(data_type, "datetime") == "attribute"
    assert _safe_projection_semantic_type("DATETIME(6)", "datetime") == "datetime"
