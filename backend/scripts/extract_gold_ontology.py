"""从 ERPNext(Frappe) / Odoo 的自带元数据抽出「黄金本体」，作为有效性验证的评分答案。

配套 `docs/EFFECTIVENESS_VALIDATION_PLAN.md`。两个系统都把自己的语义模型存在库里，
这是整套验证方案能算 P/R/F1 而不是「看着还行」的支点：

- Frappe：``tabDocType`` / ``tabDocField``。``fieldtype='Link'`` 的 ``options`` 是真外键、
  ``label`` 是业务中文名、``istable=1`` 标出明细行表（不该判为独立业务对象）、
  ``is_submittable=1`` 区分单据(事务)与主数据、物理主键恒为 ``name`` 列。
- Odoo：``ir_model`` / ``ir_model_fields``。``ttype`` 给关系类型与方向、``relation`` 给目标模型。

输出直接用 ontoMeta 自己的受控词表（``business_object``/``data_table``/``bridge``、
四种基数、六种语义类型），打分是同口径比较，不必再映射一次。

**这些元数据表必须从喂给 ontoMeta 的输入里排除**，只作评分答案；泄漏一次全部分数作废。
采集侧按表名黑名单过滤后，用 ``--check-leak`` 复核本体输入集。

**输出是 gold_v0，不是 gold_v1**：框架级模型不等于业务本体（指向 User/Role/File 的技术性
Link 不是业务关系、每张表都有的 owner/modified 不是业务属性、动词命名的单据该判事实还是
实体有争议）。脚本对这些位置打 ``curation_hint`` / ``is_framework`` / ``technical_target``
引导人工过一遍，净化冻结后才是 gold_v1。详见方案 §2.2。

未读 ``tabCustom Field``（客户化字段）：验证用的是标准实例，v1 不处理。

用法::

    cd backend && source .venv/bin/activate
    python -m scripts.extract_gold_ontology --system frappe \\
        --dsn mysql+pymysql://root:pwd@127.0.0.1:3306/_erpnext --out ../benchmark/gold_frappe.json
    python -m scripts.extract_gold_ontology --system odoo \\
        --dsn postgresql+psycopg2://odoo:pwd@127.0.0.1:5432/odoo --out ../benchmark/gold_odoo.json
    # 复核金标准有没有漏进本体输入集
    python -m scripts.extract_gold_ontology --check-leak
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# ---------------------------------------------------------------- 受控词表映射

ROLE_BUSINESS_OBJECT = "business_object"
ROLE_BRIDGE = "bridge"

# Frappe 框架模块：这些模块下的 DocType 是平台自身的表（Version/Comment/File/Role...），
# 不是业务对象。它们不从输出里删——「框架噪声表误判为业务对象 ≤5%」这条指标需要它们当分母。
FRAPPE_FRAMEWORK_MODULES = frozenset(
    {
        "Core", "Custom", "Desk", "Workflow", "Email", "Automation", "Website",
        "Printing", "Integrations", "Social", "Data Migration", "Bulk Transaction",
        "Recorder", "Sessions",
    }
)

# 布局型 fieldtype：不产生物理列，不能算属性。
FRAPPE_LAYOUT_FIELDTYPES = frozenset(
    {"Section Break", "Column Break", "Tab Break", "HTML", "Heading", "Fold", "Button", "Image"}
)

# 子表型 fieldtype：同样无物理列，但产生一条 one_to_many 关系（子行靠 parent 列回指）。
FRAPPE_TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})

FRAPPE_SEMANTIC_TYPES = {
    "Link": "identifier",
    "Dynamic Link": "identifier",
    "Currency": "measure",
    "Float": "measure",
    "Int": "measure",
    "Percent": "measure",
    "Rating": "measure",
    "Duration": "measure",
    "Date": "temporal",
    "Datetime": "temporal",
    "Time": "temporal",
    "Select": "categorical",
    "Check": "categorical",
    "Autocomplete": "categorical",
    "Data": "textual",
    "Small Text": "textual",
    "Text": "textual",
    "Long Text": "textual",
    "Text Editor": "textual",
    "Markdown Editor": "textual",
    "HTML Editor": "textual",
    "Password": "technical",
    "Code": "technical",
    "JSON": "technical",
    "Attach": "technical",
    "Attach Image": "technical",
    "Signature": "technical",
    "Geolocation": "technical",
    "Color": "technical",
    "Icon": "technical",
    "Barcode": "technical",
}

# 每张 Frappe 表都有的框架列。``name`` 是真实主键——当前主键推断只认 <对象>_id/id 约定，
# 必然漏判，这正是 benchmark 要照出来的东西（方案 §2.3）。
FRAPPE_FRAMEWORK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "identifier"),
    ("owner", "identifier"),
    ("creation", "temporal"),
    ("modified", "temporal"),
    ("modified_by", "identifier"),
    ("docstatus", "categorical"),
    ("idx", "measure"),
)
FRAPPE_CHILD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("parent", "identifier"),
    ("parenttype", "categorical"),
    ("parentfield", "categorical"),
)

# 指向这些 DocType 的 Link 是技术性外键（谁创建的/附件），不是业务关系；打标交人工批量剔除。
FRAPPE_TECHNICAL_TARGETS = frozenset({"User", "Role", "File", "DocType", "Print Format", "Language"})

# Odoo 框架模型前缀：平台自身模型。同样保留在输出里当噪声分母。
ODOO_FRAMEWORK_PREFIXES = ("ir.", "base.", "bus.", "report.", "wizard.", "mail.", "web.", "iap.")

ODOO_SEMANTIC_TYPES = {
    "many2one": "identifier",
    "integer": "measure",
    "float": "measure",
    "monetary": "measure",
    "date": "temporal",
    "datetime": "temporal",
    "selection": "categorical",
    "boolean": "categorical",
    "char": "textual",
    "text": "textual",
    "html": "textual",
    "binary": "technical",
    "json": "technical",
    "serialized": "technical",
    "reference": "technical",
    "many2one_reference": "technical",
    "properties": "technical",
    "properties_definition": "technical",
}

# 无物理列的 Odoo 字段类型：一对多是反向视图、多对多走独立中间表。
ODOO_VIRTUAL_TTYPES = frozenset({"one2many", "many2many"})

ODOO_TECHNICAL_TARGETS = frozenset({"res.users", "ir.attachment", "ir.model", "ir.ui.view"})


def _semantic_of(mapping: dict[str, str], key: str, unknown: set[str]) -> str:
    """查语义类型；未知类型记一笔交人工看，默认落 textual（最保守，不会被误当度量聚合）。"""
    hit = mapping.get(key)
    if hit is None:
        unknown.add(key)
        return "textual"
    return hit


# ---------------------------------------------------------------- Frappe


def build_frappe_gold(
    doctypes: Iterable[dict[str, Any]], docfields: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """把 tabDocType / tabDocField 的原始行转成黄金本体结构（纯函数，便于测试）。"""
    unknown_fieldtypes: set[str] = set()
    skipped_single = 0

    objects: dict[str, dict[str, Any]] = {}
    for row in doctypes:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        # Single DocType 没有自己的物理表（值存在 tabSingles），不参与打分。
        if int(row.get("issingle") or 0):
            skipped_single += 1
            continue

        module = (row.get("module") or "").strip()
        is_child = bool(int(row.get("istable") or 0))
        is_submittable = bool(int(row.get("is_submittable") or 0))
        scope = "framework" if module in FRAPPE_FRAMEWORK_MODULES else "business"

        hints: list[str] = []
        if is_submittable:
            # 单据(事务)天然是「一次业务事实」，分类器很可能判 bridge 而非实体——
            # 这条分歧要人工先定调，别让打分脚本替产品做决定。
            hints.append("可提交单据(事务型)：实体 or 事实关系需人工定调")
        if scope == "framework":
            hints.append(f"框架模块 {module}：默认非业务对象")

        cols = list(FRAPPE_FRAMEWORK_COLUMNS) + (list(FRAPPE_CHILD_COLUMNS) if is_child else [])
        objects[name] = {
            "key": name,
            "physical_table": f"tab{name}",
            "business_name": name,  # DocType 名即业务名；中文取 i18n，v1 不接
            "role": ROLE_BRIDGE if is_child else ROLE_BUSINESS_OBJECT,
            "scope": scope,
            "doc_kind": "detail" if is_child else ("transaction" if is_submittable else "master"),
            "module": module,
            "parent_object": None,  # 由父表的 Table 字段回填
            "primary_key": ["name"],
            "curation_hint": "；".join(hints) or None,
            "attributes": [
                {
                    "field": col,
                    "physical_column": col,
                    "business_name": col,
                    "semantic_type": sem,
                    "required": col == "name",
                    "is_framework": True,
                }
                for col, sem in cols
            ],
        }

    relations: list[dict[str, Any]] = []
    unscoreable: list[dict[str, Any]] = []

    for row in docfields:
        parent = (row.get("parent") or "").strip()
        fieldname = (row.get("fieldname") or "").strip()
        fieldtype = (row.get("fieldtype") or "").strip()
        obj = objects.get(parent)
        if obj is None or not fieldname:
            continue  # 父 DocType 是 Single / 已删除，跳过
        options = (row.get("options") or "").strip()
        label = (row.get("label") or "").strip() or fieldname

        if fieldtype in FRAPPE_LAYOUT_FIELDTYPES:
            continue  # 布局元素，无物理列

        if fieldtype in FRAPPE_TABLE_FIELDTYPES:
            child = objects.get(options)
            if child is None:
                unscoreable.append(
                    {"object": parent, "field": fieldname, "reason": f"子表目标 {options!r} 不存在"}
                )
                continue
            child["parent_object"] = parent
            relations.append(
                {
                    "source": parent,
                    "target": options,
                    "cardinality": "one_to_many",
                    "via_field": fieldname,
                    "business_name": label,
                    "kind": "child_table",
                    "technical_target": False,
                }
            )
            continue  # 子表字段本身无物理列，不算属性

        if fieldtype == "Dynamic Link":
            # 多态外键：目标由另一字段的运行时值决定，无静态答案。打分时从分母剔除，
            # 不能算作漏召回。
            unscoreable.append(
                {"object": parent, "field": fieldname, "reason": "Dynamic Link 多态外键，无静态目标"}
            )
        elif fieldtype == "Link":
            if options in objects:
                relations.append(
                    {
                        "source": parent,
                        "target": options,
                        "cardinality": "many_to_one",
                        "via_field": fieldname,
                        "business_name": label,
                        "kind": "link",
                        "technical_target": options in FRAPPE_TECHNICAL_TARGETS,
                    }
                )
            else:
                unscoreable.append(
                    {"object": parent, "field": fieldname, "reason": f"Link 目标 {options!r} 不是 DocType"}
                )

        obj["attributes"].append(
            {
                "field": fieldname,
                "physical_column": fieldname,
                "business_name": label,
                "semantic_type": _semantic_of(FRAPPE_SEMANTIC_TYPES, fieldtype, unknown_fieldtypes),
                "required": bool(int(row.get("reqd") or 0)),
                "is_framework": False,
            }
        )

    return _finalize(
        system="frappe",
        objects=objects,
        relations=relations,
        unscoreable=unscoreable,
        extra_stats={
            "skipped_single_doctypes": skipped_single,
            "unknown_fieldtypes": sorted(unknown_fieldtypes),
        },
    )


# ---------------------------------------------------------------- Odoo


def build_odoo_gold(
    models: Iterable[dict[str, Any]], fields: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """把 ir_model / ir_model_fields 的原始行转成黄金本体结构（纯函数，便于测试）。"""
    unknown_ttypes: set[str] = set()
    skipped_transient = 0

    objects: dict[str, dict[str, Any]] = {}
    for row in models:
        model = (row.get("model") or "").strip()
        if not model:
            continue
        # 向导/临时模型（transient）不落业务表，不参与打分。
        if bool(row.get("transient")):
            skipped_transient += 1
            continue

        scope = (
            "framework"
            if model.startswith(ODOO_FRAMEWORK_PREFIXES) or ".config.settings" in model
            else "business"
        )
        # Odoo 没有 istable 这样的显式标记，明细行按 .line 后缀识别——是启发式，标出来交人工核。
        is_line = model.endswith(".line")
        hints: list[str] = []
        if is_line:
            hints.append("按 .line 后缀推定为明细行表（启发式，需人工核）")
        if scope == "framework":
            hints.append("框架模型：默认非业务对象")

        objects[model] = {
            "key": model,
            "physical_table": model.replace(".", "_"),
            "business_name": (row.get("name") or model).strip(),
            "role": ROLE_BRIDGE if is_line else ROLE_BUSINESS_OBJECT,
            "scope": scope,
            "doc_kind": "detail" if is_line else "master",
            "module": model.split(".", 1)[0],
            "parent_object": None,
            "primary_key": ["id"],
            "curation_hint": "；".join(hints) or None,
            "attributes": [
                {
                    "field": "id",
                    "physical_column": "id",
                    "business_name": "id",
                    "semantic_type": "identifier",
                    "required": True,
                    "is_framework": True,
                }
            ],
        }

    field_rows = [dict(r) for r in fields]
    # many2one 是关系的规范表示；与之对应的反向 one2many 属重复，去掉以免虚增召回分母。
    m2o_keys = {
        ((r.get("model") or "").strip(), (r.get("name") or "").strip())
        for r in field_rows
        if (r.get("ttype") or "").strip() == "many2one"
    }

    relations: list[dict[str, Any]] = []
    unscoreable: list[dict[str, Any]] = []

    for row in field_rows:
        model = (row.get("model") or "").strip()
        name = (row.get("name") or "").strip()
        ttype = (row.get("ttype") or "").strip()
        obj = objects.get(model)
        if obj is None or not name:
            continue
        relation = (row.get("relation") or "").strip()
        label = (row.get("field_description") or "").strip() or name

        # 非存储字段（计算字段）没有物理列，本体也映射不到，不参与打分。
        if row.get("store") is not None and not row.get("store"):
            continue

        if ttype in ODOO_VIRTUAL_TTYPES:
            if relation not in objects:
                unscoreable.append(
                    {"object": model, "field": name, "reason": f"关系目标 {relation!r} 不存在或为 transient"}
                )
                continue
            if ttype == "one2many":
                inverse = (row.get("relation_field") or "").strip()
                if (relation, inverse) in m2o_keys:
                    continue  # 与对端 many2one 重复
                relations.append(
                    {
                        "source": model,
                        "target": relation,
                        "cardinality": "one_to_many",
                        "via_field": name,
                        "business_name": label,
                        "kind": "one2many",
                        "technical_target": relation in ODOO_TECHNICAL_TARGETS,
                    }
                )
            else:
                relations.append(
                    {
                        "source": model,
                        "target": relation,
                        "cardinality": "many_to_many",
                        "via_field": name,
                        "business_name": label,
                        "kind": "many2many",
                        "relation_table": (row.get("relation_table") or "").strip() or None,
                        "technical_target": relation in ODOO_TECHNICAL_TARGETS,
                    }
                )
            continue  # 两者都无物理列，不算属性

        if ttype == "many2one":
            if relation in objects:
                relations.append(
                    {
                        "source": model,
                        "target": relation,
                        "cardinality": "many_to_one",
                        "via_field": name,
                        "business_name": label,
                        "kind": "many2one",
                        "technical_target": relation in ODOO_TECHNICAL_TARGETS,
                    }
                )
            else:
                unscoreable.append(
                    {"object": model, "field": name, "reason": f"many2one 目标 {relation!r} 不存在或为 transient"}
                )

        obj["attributes"].append(
            {
                "field": name,
                "physical_column": name,
                "business_name": label,
                "semantic_type": _semantic_of(ODOO_SEMANTIC_TYPES, ttype, unknown_ttypes),
                "required": bool(row.get("required")),
                "is_framework": False,
            }
        )

    return _finalize(
        system="odoo",
        objects=objects,
        relations=relations,
        unscoreable=unscoreable,
        extra_stats={
            "skipped_transient_models": skipped_transient,
            "unknown_ttypes": sorted(unknown_ttypes),
        },
    )


# ---------------------------------------------------------------- 汇总 / 读库 / CLI


def _finalize(
    *,
    system: str,
    objects: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    unscoreable: list[dict[str, Any]],
    extra_stats: dict[str, Any],
) -> dict[str, Any]:
    ordered = [objects[k] for k in sorted(objects)]
    business = [o for o in ordered if o["scope"] == "business"]
    stats = {
        "objects_total": len(ordered),
        "objects_business": len(business),
        "objects_framework": len(ordered) - len(business),
        "objects_by_role": {
            role: sum(1 for o in business if o["role"] == role)
            for role in (ROLE_BUSINESS_OBJECT, ROLE_BRIDGE)
        },
        "objects_by_doc_kind": {
            kind: sum(1 for o in business if o["doc_kind"] == kind)
            for kind in ("master", "transaction", "detail")
        },
        "attributes_total": sum(len(o["attributes"]) for o in ordered),
        "attributes_business": sum(
            1 for o in ordered for a in o["attributes"] if not a["is_framework"]
        ),
        "relations_total": len(relations),
        "relations_technical": sum(1 for r in relations if r.get("technical_target")),
        "relations_needing_curation": sum(1 for r in relations if r.get("technical_target")),
        "objects_needing_curation": sum(1 for o in ordered if o["curation_hint"]),
        "unscoreable": len(unscoreable),
        **extra_stats,
    }
    return {
        "version": "gold_v0",
        "system": system,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vocabulary": {
            "roles": [ROLE_BUSINESS_OBJECT, ROLE_BRIDGE, "data_table"],
            "cardinalities": ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
            "semantic_types": [
                "identifier", "measure", "temporal", "categorical", "textual", "technical",
            ],
        },
        "stats": stats,
        "objects": ordered,
        "relations": sorted(relations, key=lambda r: (r["source"], r["via_field"])),
        "unscoreable": sorted(unscoreable, key=lambda u: (u["object"], u["field"])),
    }


_FRAPPE_DOCTYPE_SQL = text(
    "SELECT name, module, istable, issingle, is_submittable FROM `tabDocType`"
)
_FRAPPE_DOCFIELD_SQL = text(
    "SELECT parent, fieldname, label, fieldtype, options, reqd, idx "
    "FROM `tabDocField` ORDER BY parent, idx"
)
_ODOO_MODEL_SQL = text("SELECT model, name, transient FROM ir_model")
_ODOO_FIELD_SQL = text(
    "SELECT model, name, field_description, ttype, relation, relation_field, "
    "required, store, relation_table FROM ir_model_fields"
)
# 老版本 Odoo 的 ir_model_fields 没有 relation_table 列。
_ODOO_FIELD_SQL_FALLBACK = text(
    "SELECT model, name, field_description, ttype, relation, relation_field, "
    "required, store FROM ir_model_fields"
)


def _rows(conn, stmt) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(stmt).mappings()]


def _extract(system: str, dsn: str) -> dict[str, Any]:
    engine = create_engine(dsn)
    with engine.connect() as conn:
        if system == "frappe":
            gold = build_frappe_gold(
                _rows(conn, _FRAPPE_DOCTYPE_SQL), _rows(conn, _FRAPPE_DOCFIELD_SQL)
            )
        else:
            models = _rows(conn, _ODOO_MODEL_SQL)
            try:
                fields = _rows(conn, _ODOO_FIELD_SQL)
            except Exception:  # noqa: BLE001 — 老版本无 relation_table 列，退回精简列集
                fields = _rows(conn, _ODOO_FIELD_SQL_FALLBACK)
            gold = build_odoo_gold(models, fields)
    url = make_url(dsn)
    gold["source"] = {"host": url.host, "database": url.database}
    return gold


# 这些是评分答案，绝不能进本体输入集。
_GOLD_TABLE_MARKERS = ("tabdocfield", "tabdoctype", "ir_model_fields", "ir_model")


def _check_leak() -> int:
    """复核本体里有没有把金标准元数据表也建成了对象（泄漏则全部分数作废）。

    必须报出扫描基数：空本体上「未发现泄漏」是空跑通过，不是通过。
    """
    from app.database import SessionLocal
    from app.models import ObjectType

    hits: list[tuple[str, str]] = []
    scanned = 0
    with SessionLocal() as db:
        for obj in db.query(ObjectType).all():
            scanned += 1
            ref = (obj.source_ref or "").lower()
            if any(marker in ref for marker in _GOLD_TABLE_MARKERS):
                hits.append((obj.name, obj.source_ref or ""))

    if scanned == 0:
        print("!! 本体里一个对象都没有：本次复核未检验任何东西，不能当作通过 !!")
        print("   先跑完本体生成，再复核。")
        return 2
    if not hits:
        print(f"== 未发现泄漏：扫描 {scanned} 个对象，无一来自金标准元数据表 ==")
        return 0
    print(f"!! 泄漏 {len(hits)}/{scanned} 处：金标准元数据表进了本体，分数不可用 !!")
    for name, ref in hits:
        print(f"    {name}  <-  {ref}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="抽取 Frappe / Odoo 的黄金本体（验证评分答案）")
    parser.add_argument("--system", choices=("frappe", "odoo"), help="源系统")
    parser.add_argument("--dsn", help="源库连接串（SQLAlchemy 格式）")
    parser.add_argument("--out", help="输出 JSON 路径")
    parser.add_argument(
        "--check-leak", action="store_true", help="只复核本体输入集是否混入金标准表，不抽取"
    )
    args = parser.parse_args()

    if args.check_leak:
        raise SystemExit(_check_leak())

    if not (args.system and args.dsn and args.out):
        parser.error("--system / --dsn / --out 三者必填（或改用 --check-leak）")

    gold = _extract(args.system, args.dsn)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = gold["stats"]
    print(f"== {gold['system']} → {out} ==")
    for key, value in stats.items():
        print(f"    {key}: {value}")
    print(
        f"\n下一步：人工净化 {stats['objects_needing_curation']} 个对象提示 + "
        f"{stats['relations_technical']} 条技术性关系，冻结为 gold_v1（方案 §2.2）"
    )


if __name__ == "__main__":
    main()
