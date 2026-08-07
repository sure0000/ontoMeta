"""V5 T4 单测：引用归一（`chat_bi_references`）——拆出来之后可脱离推理循环单独测。

不建库、不起 LLM：`_ReferenceResolver` 只读实体的 id/name/display_name，
故用轻量对象喂进去，断言「LLM 编的 id 被校回真实 id、校不回的引用被丢掉」这条契约。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import chat_bi
from app.services.chat_bi_references import (
    _ObjectSnapshot,
    _ReferenceResolver,
    _loads_payload,
)


def _entity(id_: str, name: str, display_name: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, name=name, display_name=display_name)


def _resolver() -> _ReferenceResolver:
    amount = _entity("prop-1", "amount", "金额")
    order = _ObjectSnapshot(
        id="obj-order",
        name="order",
        display_name="订单",
        description=None,
        properties=[amount],
    )
    return _ReferenceResolver(
        objects=[order],
        relations=[_entity("rel-1", "placed_by", "下单人")],
        logics=[_entity("logic-1", "gmv", "GMV")],
    )


def test_reexported_from_chat_bi_is_the_same_object():
    """拆模块不得改对外契约：chat_bi.* 与新模块必须是同一个对象。"""
    assert chat_bi._ObjectSnapshot is _ObjectSnapshot
    assert chat_bi._ReferenceResolver is _ReferenceResolver
    assert chat_bi._loads_payload is _loads_payload


def test_fake_id_is_corrected_by_display_name():
    payload = _resolver().resolve_payload(
        {"referenced_objects": [{"id": "order", "display_name": "订单"}]}
    )
    assert payload["referenced_objects"] == [
        {"id": "obj-order", "display_name": "订单", "name": "order"}
    ]


def test_reference_outside_the_ontology_is_dropped():
    """校不回真实实体的引用不能留给前端——那会跳到一个不存在的详情页。"""
    payload = _resolver().resolve_payload(
        {
            "referenced_objects": [{"id": "ghost", "name": "不存在的对象"}],
            "referenced_logics": [{"id": "ghost-logic", "name": "不存在的口径"}],
        }
    )
    assert payload["referenced_objects"] == []
    assert payload["referenced_logics"] == []


def test_caliber_references_resolve_per_kind():
    payload = _resolver().resolve_payload(
        {
            "caliber_decomposition": [
                {
                    "references": [
                        {"kind": "object_type", "name": "order"},
                        {"kind": "business_logic", "display_name": "GMV"},
                        {"kind": "relation_type", "name": "placed_by"},
                        {"kind": "property", "display_name": "金额"},
                        {"kind": "unknown_kind", "id": "keep-me"},
                    ]
                }
            ]
        }
    )
    ids = [r["id"] for r in payload["caliber_decomposition"][0]["references"]]
    assert ids == ["obj-order", "logic-1", "rel-1", "prop-1", "keep-me"]


def test_loads_payload_tolerates_junk():
    assert _loads_payload(None) == {}
    assert _loads_payload("") == {}
    assert _loads_payload("not json") == {}
    assert _loads_payload("[1, 2]") == {}
    assert _loads_payload('{"a": 1}') == {"a": 1}
