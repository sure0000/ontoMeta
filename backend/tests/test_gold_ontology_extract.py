"""黄金本体抽取：Frappe / Odoo 元数据 → 评分答案结构。

不连实盘 ERPNext/Odoo，直接喂伪造行给纯函数——要钉住的是那些「错了会让分数失真」的
转换细节：布局字段不该算属性、子表关系方向、多态外键必须进 unscoreable（否则被算成
漏召回）、框架表不能删（噪声过滤率要拿它当分母）。
"""

from __future__ import annotations

from scripts.extract_gold_ontology import build_frappe_gold, build_odoo_gold

# ---------------------------------------------------------------- Frappe

FRAPPE_DOCTYPES = [
    {"name": "Sales Order", "module": "Selling", "istable": 0, "issingle": 0, "is_submittable": 1},
    {"name": "Sales Order Item", "module": "Selling", "istable": 1, "issingle": 0, "is_submittable": 0},
    {"name": "Customer", "module": "Selling", "istable": 0, "issingle": 0, "is_submittable": 0},
    {"name": "User", "module": "Core", "istable": 0, "issingle": 0, "is_submittable": 0},
    {"name": "Global Defaults", "module": "Setup", "istable": 0, "issingle": 1, "is_submittable": 0},
]

FRAPPE_DOCFIELDS = [
    {"parent": "Sales Order", "fieldname": "customer", "label": "客户", "fieldtype": "Link",
     "options": "Customer", "reqd": 1, "idx": 1},
    {"parent": "Sales Order", "fieldname": "column_break_1", "label": "", "fieldtype": "Column Break",
     "options": "", "reqd": 0, "idx": 2},
    {"parent": "Sales Order", "fieldname": "items", "label": "明细", "fieldtype": "Table",
     "options": "Sales Order Item", "reqd": 1, "idx": 3},
    {"parent": "Sales Order", "fieldname": "grand_total", "label": "总计", "fieldtype": "Currency",
     "options": "", "reqd": 0, "idx": 4},
    {"parent": "Sales Order", "fieldname": "transaction_date", "label": "单据日期", "fieldtype": "Date",
     "options": "", "reqd": 1, "idx": 5},
    {"parent": "Sales Order", "fieldname": "status", "label": "状态", "fieldtype": "Select",
     "options": "Draft\nSubmitted", "reqd": 0, "idx": 6},
    {"parent": "Sales Order", "fieldname": "party", "label": "关联方", "fieldtype": "Dynamic Link",
     "options": "party_type", "reqd": 0, "idx": 7},
    {"parent": "Sales Order", "fieldname": "sales_person", "label": "销售员", "fieldtype": "Link",
     "options": "User", "reqd": 0, "idx": 8},
    {"parent": "Sales Order Item", "fieldname": "item_code", "label": "物料", "fieldtype": "Link",
     "options": "Item", "reqd": 1, "idx": 1},
    {"parent": "Sales Order Item", "fieldname": "qty", "label": "数量", "fieldtype": "Float",
     "options": "", "reqd": 1, "idx": 2},
    # 父 DocType 是 Single，已被跳过——其字段不应带出幽灵对象
    {"parent": "Global Defaults", "fieldname": "default_company", "label": "默认公司",
     "fieldtype": "Link", "options": "Customer", "reqd": 0, "idx": 1},
]


def _frappe():
    return build_frappe_gold(FRAPPE_DOCTYPES, FRAPPE_DOCFIELDS)


def _obj(gold: dict, key: str) -> dict:
    return next(o for o in gold["objects"] if o["key"] == key)


def test_frappe_roles_and_doc_kinds():
    gold = _frappe()

    order = _obj(gold, "Sales Order")
    assert order["role"] == "business_object"
    assert order["doc_kind"] == "transaction"
    assert order["physical_table"] == "tabSales Order"  # 表名带空格，方言鲁棒性的来源
    assert order["primary_key"] == ["name"]  # 恒为 name，非 <对象>_id 约定
    assert "单据" in (order["curation_hint"] or "")

    item = _obj(gold, "Sales Order Item")
    assert item["role"] == "bridge"
    assert item["doc_kind"] == "detail"
    assert item["parent_object"] == "Sales Order"  # 由父表 Table 字段回填

    assert _obj(gold, "Customer")["doc_kind"] == "master"


def test_frappe_framework_tables_kept_as_noise_denominator():
    gold = _frappe()
    user = _obj(gold, "User")
    assert user["scope"] == "framework"
    assert _obj(gold, "Sales Order")["scope"] == "business"
    # 框架表留在输出里，「噪声误判率 ≤5%」这条指标才有分母
    assert gold["stats"]["objects_framework"] == 1
    assert gold["stats"]["objects_business"] == 3


def test_frappe_single_doctype_skipped():
    gold = _frappe()
    assert all(o["key"] != "Global Defaults" for o in gold["objects"])
    assert gold["stats"]["skipped_single_doctypes"] == 1
    # Single 的字段不能凭空造出关系
    assert all(r["source"] != "Global Defaults" for r in gold["relations"])


def test_frappe_attributes_exclude_layout_and_child_table_fields():
    fields = {a["field"] for a in _obj(_frappe(), "Sales Order")["attributes"]}
    assert "column_break_1" not in fields  # 布局元素无物理列
    assert "items" not in fields  # 子表字段无物理列，只产生关系
    assert {"customer", "grand_total", "transaction_date", "status"} <= fields
    assert {"name", "owner", "modified", "docstatus"} <= fields  # 框架列自动补齐


def test_frappe_child_table_gets_parent_columns():
    fields = {a["field"] for a in _obj(_frappe(), "Sales Order Item")["attributes"]}
    assert {"parent", "parenttype", "parentfield"} <= fields


def test_frappe_semantic_types():
    attrs = {a["field"]: a for a in _obj(_frappe(), "Sales Order")["attributes"]}
    assert attrs["customer"]["semantic_type"] == "identifier"
    assert attrs["grand_total"]["semantic_type"] == "measure"
    assert attrs["transaction_date"]["semantic_type"] == "temporal"
    assert attrs["status"]["semantic_type"] == "categorical"
    assert attrs["customer"]["required"] is True
    assert attrs["name"]["is_framework"] is True
    assert attrs["customer"]["is_framework"] is False


def test_frappe_relations_direction_and_cardinality():
    gold = _frappe()
    rels = {(r["source"], r["via_field"]): r for r in gold["relations"]}

    fk = rels[("Sales Order", "customer")]
    assert (fk["target"], fk["cardinality"], fk["kind"]) == ("Customer", "many_to_one", "link")
    assert fk["technical_target"] is False

    child = rels[("Sales Order", "items")]
    assert (child["target"], child["cardinality"], child["kind"]) == (
        "Sales Order Item", "one_to_many", "child_table",
    )

    # 指向 User 的外键是技术性的，打标交人工批量剔除，不能算业务关系
    assert rels[("Sales Order", "sales_person")]["technical_target"] is True


def test_frappe_unscoreable_excluded_from_recall_denominator():
    gold = _frappe()
    unscoreable = {(u["object"], u["field"]) for u in gold["unscoreable"]}

    # 多态外键无静态目标：进 unscoreable，不算漏召回
    assert ("Sales Order", "party") in unscoreable
    assert all(r["via_field"] != "party" for r in gold["relations"])
    # 但它确实有物理列，属性照算
    assert any(a["field"] == "party" for a in _obj(gold, "Sales Order")["attributes"])

    # Link 目标不在本次抽取范围（Item 未建）：同样不计漏召回
    assert ("Sales Order Item", "item_code") in unscoreable
    assert any(a["field"] == "item_code" for a in _obj(gold, "Sales Order Item")["attributes"])


# ---------------------------------------------------------------- Odoo

ODOO_MODELS = [
    {"model": "sale.order", "name": "销售订单", "transient": False},
    {"model": "sale.order.line", "name": "销售订单行", "transient": False},
    {"model": "res.partner", "name": "业务伙伴", "transient": False},
    {"model": "ir.model", "name": "Models", "transient": False},
    {"model": "sale.advance.payment.inv", "name": "预付款向导", "transient": True},
]

ODOO_FIELDS = [
    {"model": "sale.order", "name": "partner_id", "field_description": "客户", "ttype": "many2one",
     "relation": "res.partner", "relation_field": None, "required": True, "store": True},
    {"model": "sale.order", "name": "order_line", "field_description": "订单行", "ttype": "one2many",
     "relation": "sale.order.line", "relation_field": "order_id", "required": False, "store": True},
    {"model": "sale.order", "name": "amount_total", "field_description": "总计", "ttype": "monetary",
     "relation": None, "relation_field": None, "required": False, "store": True},
    {"model": "sale.order", "name": "margin", "field_description": "毛利", "ttype": "float",
     "relation": None, "relation_field": None, "required": False, "store": False},
    {"model": "sale.order", "name": "tag_ids", "field_description": "标签", "ttype": "many2many",
     "relation": "res.partner", "relation_field": None, "required": False, "store": True,
     "relation_table": "sale_order_partner_rel"},
    {"model": "sale.order", "name": "date_order", "field_description": "订单日期", "ttype": "datetime",
     "relation": None, "relation_field": None, "required": True, "store": True},
    {"model": "sale.order", "name": "state", "field_description": "状态", "ttype": "selection",
     "relation": None, "relation_field": None, "required": False, "store": True},
    {"model": "sale.order.line", "name": "order_id", "field_description": "订单", "ttype": "many2one",
     "relation": "sale.order", "relation_field": None, "required": True, "store": True},
    {"model": "res.partner", "name": "name", "field_description": "名称", "ttype": "char",
     "relation": None, "relation_field": None, "required": True, "store": True},
]


def _odoo():
    return build_odoo_gold(ODOO_MODELS, ODOO_FIELDS)


def test_odoo_objects_and_scope():
    gold = _odoo()

    order = _obj(gold, "sale.order")
    assert order["physical_table"] == "sale_order"
    assert order["business_name"] == "销售订单"
    assert order["role"] == "business_object"
    assert order["primary_key"] == ["id"]

    line = _obj(gold, "sale.order.line")
    assert line["role"] == "bridge"
    assert line["doc_kind"] == "detail"
    assert "启发式" in (line["curation_hint"] or "")  # .line 后缀是推定，需人工核

    assert _obj(gold, "ir.model")["scope"] == "framework"
    assert _obj(gold, "res.partner")["scope"] == "business"


def test_odoo_transient_models_skipped():
    gold = _odoo()
    assert all(o["key"] != "sale.advance.payment.inv" for o in gold["objects"])
    assert gold["stats"]["skipped_transient_models"] == 1


def test_odoo_non_stored_and_virtual_fields_are_not_attributes():
    fields = {a["field"] for a in _obj(_odoo(), "sale.order")["attributes"]}
    assert "margin" not in fields  # 计算字段无物理列
    assert "order_line" not in fields  # 一对多是反向视图
    assert "tag_ids" not in fields  # 多对多走独立中间表
    assert {"partner_id", "amount_total", "date_order", "state", "id"} <= fields


def test_odoo_relations():
    gold = _odoo()
    rels = {(r["source"], r["via_field"]): r for r in gold["relations"]}

    fk = rels[("sale.order", "partner_id")]
    assert (fk["target"], fk["cardinality"]) == ("res.partner", "many_to_one")

    m2m = rels[("sale.order", "tag_ids")]
    assert m2m["cardinality"] == "many_to_many"
    assert m2m["relation_table"] == "sale_order_partner_rel"

    assert rels[("sale.order.line", "order_id")]["cardinality"] == "many_to_one"


def test_odoo_inverse_one2many_deduped():
    """order_line 与 sale.order.line.order_id 是同一条关系的两面，只能计一次。"""
    gold = _odoo()
    assert ("sale.order", "order_line") not in {(r["source"], r["via_field"]) for r in gold["relations"]}
    assert gold["stats"]["relations_total"] == 3


def test_odoo_semantic_types():
    attrs = {a["field"]: a for a in _obj(_odoo(), "sale.order")["attributes"]}
    assert attrs["partner_id"]["semantic_type"] == "identifier"
    assert attrs["amount_total"]["semantic_type"] == "measure"
    assert attrs["date_order"]["semantic_type"] == "temporal"
    assert attrs["state"]["semantic_type"] == "categorical"


# ---------------------------------------------------------------- 输出契约


def test_output_shape_is_stable_for_scorer():
    for gold in (_frappe(), _odoo()):
        assert gold["version"] == "gold_v0"  # 人工净化后才是 v1
        assert set(gold) >= {
            "version", "system", "extracted_at", "vocabulary", "stats",
            "objects", "relations", "unscoreable",
        }
        # 词表与 app/ontology_types.py、object_classifier 一致，打分不必再映射
        assert gold["vocabulary"]["cardinalities"] == [
            "one_to_one", "one_to_many", "many_to_one", "many_to_many",
        ]
        assert "business_object" in gold["vocabulary"]["roles"]
        for obj in gold["objects"]:
            assert obj["role"] in gold["vocabulary"]["roles"]
            for attr in obj["attributes"]:
                assert attr["semantic_type"] in gold["vocabulary"]["semantic_types"]
        for rel in gold["relations"]:
            assert rel["cardinality"] in gold["vocabulary"]["cardinalities"]
