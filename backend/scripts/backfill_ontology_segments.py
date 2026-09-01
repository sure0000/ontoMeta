"""Backfill persisted ontology segments for an existing ontology.

The normal draft-generation path creates segments as part of a full merge. This
command is for legacy snapshots that already contain ObjectType/RelationType
rows but have never persisted the derived segment graph.

Usage:
    python scripts/backfill_ontology_segments.py --ontology-id <id> --dry-run
    python scripts/backfill_ontology_segments.py --ontology-id <id> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from app.database import SessionLocal
from app.models import ObjectType, Ontology, RelationType
from app.schemas import DraftObjectType, DraftRelationType
from app.services.common import make_async_http_client
from app.services.draft_checkpoint import DraftCheckpointStore, chunk_key
from app.services.ontology_merge import MergeReport, OntologyMergeService
from app.services.segment_generator import (
    _build_segment_naming_prompt,
    generate_segments,
)
from app.services.settings_service import SettingsService


def build_draft_rows(db, ontology_id: str):
    objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
    object_names = {obj.id: obj.name for obj in objects}
    relations = (
        db.query(RelationType)
        .filter(
            RelationType.ontology_id == ontology_id,
            RelationType.deleted_by_user == False,
        )
        .all()
    )
    draft_objects = [
        DraftObjectType(
            name=obj.name,
            display_name=obj.display_name,
            description=obj.description,
            source_ref=obj.source_ref,
            table_role=obj.table_role,
            role_confidence=obj.role_confidence or 0.5,
            role_reason=obj.role_reason,
            needs_review=bool(obj.needs_review),
            row_count=obj.row_count,
        )
        for obj in objects
    ]
    draft_relations = [
        DraftRelationType(
            name=rel.name,
            display_name=rel.display_name,
            description=rel.description,
            source_object_type_name=object_names[rel.source_object_type_id],
            target_object_type_name=object_names[rel.target_object_type_id],
            cardinality=rel.cardinality,
            structure_type=rel.structure_type,
            source_evidence=rel.source_evidence,
            confidence=rel.source_confidence or 0.5,
        )
        for rel in relations
        if rel.source_object_type_id in object_names
        and rel.target_object_type_id in object_names
    ]
    return draft_objects, draft_relations


async def name_with_runtime(db, segments, objects, relations, hubs, domain_context_id) -> None:
    runtime = SettingsService().get_llm_runtime(db)
    if not runtime.api_key or not runtime.model:
        raise RuntimeError("未配置可用的 LLM；请先使用 --dry-run 或完成 LLM 设置")
    client = AsyncOpenAI(
        api_key=runtime.api_key,
        base_url=runtime.api_base_url or None,
        timeout=60,
        max_retries=1,
        http_client=make_async_http_client(),
    )
    try:
        by_name = {obj.name: obj for obj in objects}
        prompts = []
        for index, segment in enumerate(segments):
            member_names = [
                by_name[name].display_name
                for name in segment.members[:15]
                if name in by_name
            ]
            verbs = sorted(
                {
                    rel.display_name
                    for rel in relations
                    if rel.source_object_type_name in segment.members
                    and rel.target_object_type_name in segment.members
                }
            )[:5]
            connected_hubs = sorted(
                {
                    by_name[hub].display_name
                    for rel in relations
                    for hub in hubs
                    if (
                        rel.source_object_type_name in segment.members
                        and rel.target_object_type_name == hub
                    )
                    or (
                        rel.target_object_type_name in segment.members
                        and rel.source_object_type_name == hub
                    )
                    if hub in by_name
                }
            )[:5]
            prompts.append(
                f"板块 {index}:\n"
                + _build_segment_naming_prompt(
                    member_names, verbs, connected_hubs, segment.machine_baseline or ""
                )
            )

        checkpoint = DraftCheckpointStore(domain_context_id)
        named_items = {}
        for start in range(0, len(segments), 4):
            batch_segments = segments[start : start + 4]
            batch_prompts = prompts[start : start + 4]
            checkpoint_key = chunk_key("\n\n".join(batch_prompts))
            cached = checkpoint.load(checkpoint_key)
            items = cached.get("items", []) if cached else None
            if items is None:
                completion = await client.chat.completions.create(
                    model=runtime.model,
                    messages=[
                        {"role": "system", "content": "你是数据治理专家，只输出合法 JSON 数组。"},
                        {
                            "role": "user",
                            "content": (
                                "请为下面每个板块返回一个对象，必须保留 index。"
                                "返回格式：[index, display_name, name, description]\n\n"
                                + "\n\n".join(batch_prompts)
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                result_text = (completion.choices[0].message.content or "").strip()
                if result_text.startswith("```"):
                    lines = result_text.splitlines()
                    result_text = "\n".join(lines[1:-1]).strip()
                payload = json.loads(result_text)
                items = payload if isinstance(payload, list) else payload.get("segments", [])
                checkpoint.save(checkpoint_key, {"items": items})
            by_index = {int(item["index"]): item for item in items if isinstance(item, dict)}
            for local_index, segment in enumerate(batch_segments):
                global_index = start + local_index
                item = by_index.get(global_index) or by_index.get(local_index)
                if item is None:
                    raise RuntimeError(f"LLM 未为板块 {global_index} 返回命名")
                named_items[global_index] = item

        for index, segment in enumerate(segments):
            item = named_items[index]
            display_name = str(item.get("display_name", "")).strip()
            name = str(item.get("name", "")).strip()
            if not display_name or not name or not name.replace("_", "").isalnum():
                raise RuntimeError(f"板块 {index} 命名不合法")
            segment.display_name = display_name
            segment.name = name
            segment.description = str(item.get("description", "")).strip() or None
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ontology-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 不能同时使用")

    db = SessionLocal()
    try:
        ontology = db.get(Ontology, args.ontology_id)
        if ontology is None:
            raise RuntimeError(f"本体不存在：{args.ontology_id}")
        objects, relations = build_draft_rows(db, args.ontology_id)
        segments, hubs = generate_segments(objects, relations)
        print(
            json.dumps(
                {
                    "ontology_id": args.ontology_id,
                    "objects": len(objects),
                    "relations": len(relations),
                    "segments": len(segments),
                    "hubs": len(hubs),
                    "segment_sizes": sorted(
                        (len(segment.members) for segment in segments), reverse=True
                    ),
                },
                ensure_ascii=False,
            )
        )
        if not args.apply:
            return 0

        asyncio.run(
            name_with_runtime(
                db, segments, objects, relations, hubs, ontology.domain_context_id
            )
        )
        merge = OntologyMergeService()
        report = MergeReport()
        merge.merge_segments(db, args.ontology_id, segments, "segment-backfill", report)
        merge._assign_segment_members(db, args.ontology_id, segments, list(hubs), objects)
        db.commit()
        print(json.dumps(report.to_dict(), ensure_ascii=False))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
