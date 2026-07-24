"""DataHub 字段样例值(sample values)解析单测。

覆盖:datasetProfiles 有数据时正确填充 FieldInput.sample_values;
profiling 未开启/无采集结果时优雅降级为空列表,不报错。
"""

from __future__ import annotations

from app.connectors.datahub import (
    _SAMPLE_VALUE_MAX_LENGTH,
    _SAMPLE_VALUES_PER_FIELD,
    _parse_field_profiles,
    _parse_glossary_terms,
    _parse_row_count,
    _parse_schema_fields,
)


def test_parse_field_profiles_extracts_sample_values():
    raw = {
        "datasetProfiles": [
            {
                "fieldProfiles": [
                    {"fieldPath": "customer_level", "sampleValues": ["普通", "黄金", "铂金"]},
                    {"fieldPath": "customer_id", "sampleValues": ["1001", "1002"], "uniqueCount": 2},
                ]
            }
        ]
    }
    profiles = _parse_field_profiles(raw)
    assert profiles["customer_level"]["sample_values"] == ["普通", "黄金", "铂金"]
    assert profiles["customer_id"]["sample_values"] == ["1001", "1002"]
    assert profiles["customer_id"]["unique_count"] == 2
    assert profiles["customer_level"]["unique_count"] is None


def test_parse_field_profiles_handles_nested_field_path():
    raw = {
        "datasetProfiles": [
            {"fieldProfiles": [{"fieldPath": "table.field_a", "sampleValues": ["x"]}]}
        ]
    }
    profiles = _parse_field_profiles(raw)
    assert profiles["field_a"]["sample_values"] == ["x"]


def test_parse_field_profiles_caps_count_and_length():
    long_value = "a" * 200
    raw = {
        "datasetProfiles": [
            {
                "fieldProfiles": [
                    {
                        "fieldPath": "wide_field",
                        "sampleValues": [long_value] * (_SAMPLE_VALUES_PER_FIELD + 5),
                    }
                ]
            }
        ]
    }
    profiles = _parse_field_profiles(raw)
    vals = profiles["wide_field"]["sample_values"]
    assert len(vals) == _SAMPLE_VALUES_PER_FIELD
    assert all(len(v) == _SAMPLE_VALUE_MAX_LENGTH for v in vals)


def test_parse_field_profiles_missing_or_empty_degrades_gracefully():
    assert _parse_field_profiles({}) == {}
    assert _parse_field_profiles({"datasetProfiles": []}) == {}
    assert _parse_field_profiles({"datasetProfiles": [{"fieldProfiles": []}]}) == {}
    assert _parse_field_profiles({"datasetProfiles": [{}]}) == {}


def test_parse_schema_fields_merges_sample_values():
    schema_metadata = {
        "fields": [
            {"fieldPath": "customer_level", "type": "STRING", "nativeDataType": "string"},
        ],
        "primaryKeys": [],
        "foreignKeys": [],
    }
    fields = _parse_schema_fields(
        schema_metadata,
        {"customer_level": {"sample_values": ["普通", "黄金"], "unique_count": 2}},
    )
    assert len(fields) == 1
    assert fields[0].sample_values == ["普通", "黄金"]
    assert fields[0].unique_count == 2


def test_parse_row_count_and_glossary_terms():
    assert _parse_row_count({}) is None
    assert _parse_row_count({"datasetProfiles": [{"rowCount": 1000}]}) == 1000
    assert _parse_glossary_terms({}) == []
    raw = {
        "glossaryTerms": {
            "terms": [
                {"term": {"urn": "urn:li:glossaryTerm:cust", "properties": {"name": "客户"}}},
                {"term": {"urn": "urn:li:glossaryTerm:cust", "properties": {"name": "客户"}}},
            ]
        }
    }
    assert _parse_glossary_terms(raw) == ["客户"]


def test_parse_schema_fields_defaults_to_empty_sample_values():
    schema_metadata = {
        "fields": [{"fieldPath": "order_id", "type": "LONG", "nativeDataType": "bigint"}],
        "primaryKeys": ["order_id"],
        "foreignKeys": [],
    }
    fields = _parse_schema_fields(schema_metadata)
    assert fields[0].sample_values == []
