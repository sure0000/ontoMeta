"""① 同步作业 Drafter —— 「给出数据同步需求，智能体自动同步数据到数仓」。

同步不是照搬源表，而是**按本体结构做映射搬运**：源表由 ``ObjectType.source_ref``
定位，目标结构由本体决定。

内含**关键源保全判定**（决策：关键源保全、其余不保）——判定结果作为 SyncSpec
的一个字段，由智能体起草、人工确认，不另立机制。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.common import require_context, select_by_intent
from app.agents.drafters.base import Drafter
from app.connectors.datahub import _extract_dataset_name
from app.database import SessionLocal
from app.models import MaterializationContract, ObjectType
from app.models.warehouse import TargetKind
from app.services.job_planner import DEFAULT_SOURCE_ALIAS

# 关键源保全判定规则。命中任一 → 需在 STG 留原始副本。
_PRESERVE_RULES: tuple[tuple[str, str], ...] = (
    (r"日志|流水|审计|log|audit|journal", "有保留期/会被清理"),
    (r"cdc|binlog|消息|流|stream|kafka", "CDC/消息流，一次性不可重放"),
    (r"状态|status|原地更新|覆盖写", "状态被原地更新且无历史快照"),
    (r"归档|archive|冷备", "源库有归档策略且归档不可访问"),
)


def decide_preservation(intent: str, table_name: str) -> dict[str, Any]:
    """关键源保全判定。可随时全量重拉的源不保全，以省存储。"""
    haystack = f"{intent or ''} {table_name or ''}"
    for pattern, reason in _PRESERVE_RULES:
        if re.search(pattern, haystack, re.IGNORECASE):
            return {"preserve": True, "reason": reason}
    return {
        "preserve": False,
        "reason": "可随时全量重拉（主数据/配置表/码表），不留 STG 副本",
    }


class SyncDrafter(Drafter):
    kind = "sync"
    required_context = ("ontology_id",)

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        ontology_id = context["ontology_id"]
        with SessionLocal() as db:
            objects = (
                db.query(ObjectType)
                .filter(ObjectType.ontology_id == ontology_id)
                .all()
            )
            explicit = context.get("object_type")
            target = (
                next((o for o in objects if o.name == explicit), None)
                if explicit
                else select_by_intent(
                    intent, objects, key=lambda o: (o.name, o.display_name, o.description)
                )
            )
            if target is None:
                raise ValueError("未在本体中找到匹配的对象；请在 context.object_type 指定")
            if not target.source_ref:
                raise ValueError(f"对象 {target.name} 无 source_ref，无法定位源表")

            contract = (
                db.query(MaterializationContract)
                .filter(
                    MaterializationContract.ontology_id == ontology_id,
                    MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
                    MaterializationContract.target_id == target.id,
                )
                .first()
            )
            source_table = _extract_dataset_name(target.source_ref)
            layer = contract.target_layer if contract else "dim"
            prefix = context.get("database_prefix")
            database = f"{layer}_{prefix}" if prefix else layer

            return {
                "source": source_table,
                "target": f"{database}.{target.name}",
                "object_type": target.name,
                "ontology_id": ontology_id,
                "mode": (contract.load_strategy if contract else "full"),
                "partition_key": contract.partition_key if contract else None,
                "engine": context.get("engine")
                or (contract.engines[0] if contract and contract.engines else "hive"),
                "preservation": decide_preservation(intent, source_table),
                # 凭据不入 Spec：只放连接别名（= Airflow conn_id），执行侧按别名取连接串。
                # 默认值取 job_planner 的同一常量——三处各写各的 "erp_readonly" /
                # "default" 会让「改默认连接」这件事漏掉其中一处。
                "source_ref_alias": context.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS,
            }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return f"同步 · {spec.get('source')} → {spec.get('target')}"
