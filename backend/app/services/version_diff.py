"""发布版本快照与可读 diff。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import BusinessLogic, ObjectType, Property, RelationType, VersionRecord


def _obj_fingerprint(obj: ObjectType) -> dict[str, Any]:
    return {
        "id": obj.id,
        "name": obj.name,
        "display_name": obj.display_name,
        "description": obj.description or "",
        "status": obj.status,
        "source_ref": obj.source_ref or "",
    }


def _prop_fingerprint(prop: Property, object_name: str) -> dict[str, Any]:
    return {
        "id": prop.id,
        "object_name": object_name,
        "name": prop.name,
        "display_name": prop.display_name,
        "data_type": prop.data_type or "",
        "semantic_type": prop.semantic_type or "",
        "status": prop.status,
    }


def _rel_fingerprint(
    rel: RelationType,
    *,
    source_name: str,
    target_name: str,
    mapping_name: str | None,
) -> dict[str, Any]:
    return {
        "id": rel.id,
        "name": rel.name,
        "display_name": rel.display_name,
        "description": rel.description or "",
        "source_name": source_name,
        "target_name": target_name,
        "mapping_name": mapping_name or "",
        "cardinality": rel.cardinality or "",
        "structure_type": rel.structure_type or "",
        "status": rel.status,
    }


def _logic_fingerprint(logic: BusinessLogic) -> dict[str, Any]:
    return {
        "id": logic.id,
        "name": logic.name,
        "display_name": logic.display_name,
        "logic_type": logic.logic_type,
        "description": logic.description or "",
        "expression_summary": logic.expression_summary or "",
        "status": logic.status,
    }


def capture_ontology_snapshot(db: Session, ontology_id: str) -> dict[str, Any]:
    """捕获本体当前实体指纹，供下次发布对比与只读快照查看。"""
    objects = (
        db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
    )
    obj_by_id = {o.id: o for o in objects}
    props = (
        db.query(Property)
        .join(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    )
    relations = (
        db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
    )
    logics = (
        db.query(BusinessLogic).filter(BusinessLogic.ontology_id == ontology_id).all()
    )

    return {
        "object_types": {
            o.name: _obj_fingerprint(o) for o in objects if o.status != "deprecated"
        },
        "properties": {
            f"{obj_by_id[p.object_type_id].name}.{p.name}": _prop_fingerprint(
                p, obj_by_id[p.object_type_id].name
            )
            for p in props
            if p.object_type_id in obj_by_id and p.status != "deprecated"
        },
        "relation_types": {
            r.name: _rel_fingerprint(
                r,
                source_name=obj_by_id[r.source_object_type_id].name
                if r.source_object_type_id in obj_by_id
                else "",
                target_name=obj_by_id[r.target_object_type_id].name
                if r.target_object_type_id in obj_by_id
                else "",
                mapping_name=(
                    obj_by_id[r.mapping_object_type_id].name
                    if r.mapping_object_type_id and r.mapping_object_type_id in obj_by_id
                    else None
                ),
            )
            for r in relations
            if r.status != "deprecated"
        },
        "business_logics": {
            logic.name: _logic_fingerprint(logic)
            for logic in logics
            if logic.status != "deprecated"
        },
    }


def _diff_maps(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    compare_keys: list[str],
) -> dict[str, list[dict[str, Any]]]:
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []

    before_keys = set(before)
    after_keys = set(after)

    for key in sorted(after_keys - before_keys):
        item = after[key]
        added.append(
            {
                "key": key,
                "name": item.get("name") or key,
                "display_name": item.get("display_name") or item.get("name") or key,
            }
        )
    for key in sorted(before_keys - after_keys):
        item = before[key]
        removed.append(
            {
                "key": key,
                "name": item.get("name") or key,
                "display_name": item.get("display_name") or item.get("name") or key,
            }
        )
    for key in sorted(before_keys & after_keys):
        b, a = before[key], after[key]
        changes = {
            k: {"from": b.get(k), "to": a.get(k)}
            for k in compare_keys
            if b.get(k) != a.get(k)
        }
        if changes:
            modified.append(
                {
                    "key": key,
                    "name": a.get("name") or key,
                    "display_name": a.get("display_name") or a.get("name") or key,
                    "changes": changes,
                }
            )

    return {"added": added, "removed": removed, "modified": modified}


def compute_version_diff(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    prev = previous or {
        "object_types": {},
        "properties": {},
        "relation_types": {},
        "business_logics": {},
    }
    objects = _diff_maps(
        prev.get("object_types") or {},
        current.get("object_types") or {},
        compare_keys=["display_name", "description", "status", "source_ref"],
    )
    properties = _diff_maps(
        prev.get("properties") or {},
        current.get("properties") or {},
        compare_keys=["display_name", "data_type", "semantic_type", "status"],
    )
    relations = _diff_maps(
        prev.get("relation_types") or {},
        current.get("relation_types") or {},
        compare_keys=[
            "display_name",
            "description",
            "source_name",
            "target_name",
            "mapping_name",
            "cardinality",
            "structure_type",
            "status",
        ],
    )
    logics = _diff_maps(
        prev.get("business_logics") or {},
        current.get("business_logics") or {},
        compare_keys=[
            "display_name",
            "logic_type",
            "description",
            "expression_summary",
            "status",
        ],
    )
    return {
        "object_types": objects,
        "properties": properties,
        "relation_types": relations,
        "business_logics": logics,
    }


def summarize_diff(diff: dict[str, Any]) -> str:
    parts: list[str] = []
    labels = {
        "object_types": "对象",
        "relation_types": "关系",
        "business_logics": "逻辑",
        "properties": "属性",
    }
    for key, label in labels.items():
        section = diff.get(key) or {}
        a, r, m = (
            len(section.get("added") or []),
            len(section.get("removed") or []),
            len(section.get("modified") or []),
        )
        if a or r or m:
            bits = []
            if a:
                bits.append(f"新增{a}")
            if m:
                bits.append(f"修改{m}")
            if r:
                bits.append(f"删除{r}")
            parts.append(f"{label}{'/'.join(bits)}")
    if not parts:
        return "无实体变更（仅状态/元数据更新）"
    return "；".join(parts)


def load_previous_snapshot(
    db: Session, ontology_id: str, *, before_version: int
) -> dict[str, Any] | None:
    """取上一发布版本的快照（version < before_version 的最新一条）。"""
    record = (
        db.query(VersionRecord)
        .filter(
            VersionRecord.entity_type == "ontology",
            VersionRecord.entity_id == ontology_id,
            VersionRecord.version < before_version,
        )
        .order_by(VersionRecord.version.desc())
        .first()
    )
    if not record or not getattr(record, "snapshot_json", None):
        return None
    try:
        return json.loads(record.snapshot_json)
    except (TypeError, json.JSONDecodeError):
        return None


def parse_diff_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None
