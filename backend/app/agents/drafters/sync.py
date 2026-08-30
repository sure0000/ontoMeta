"""① 同步作业 Drafter —— 「给出数据同步需求，智能体自动同步数据到数仓」。

同步不是照搬源表，而是**按本体结构做映射搬运**：源表由 ``ObjectType.source_ref``
定位，目标结构由本体决定。

内含**关键源保全判定**（决策：关键源保全、其余不保）——判定结果作为 SyncSpec
的一个字段，由智能体起草、人工确认，不另立机制。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.common import require_context, resolve_spec_engine, select_by_intent
from app.agents.drafters.base import Drafter
from app.database import SessionLocal
from app.models import MaterializationContract, ObjectType
from app.models.warehouse import TargetKind
from app.services import flink_params
from app.services.job_planner import DEFAULT_SOURCE_ALIAS
from app.services.ods_naming import ODS_DATABASE, target_ods_table_name
from app.services.source_ref import (
    NO_SOURCE_NOTE_BY_PROVENANCE,
    has_physical_source,
    provenance_of,
    source_table_of,
)

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
            if not has_physical_source(target.source_ref):
                # 三种成因对搬运是同一件事（没有源库表可搬），但对使用者不是同一件事：
                # 人工建模对象该去物化，派生对象该去清洗——它有上游，只是上游在数仓里。
                # 一律回一句「没有物理源表」会把人推去补一个根本不存在的数据源。
                raise ValueError(
                    f"对象「{target.name}」不能建同步任务："
                    f"{NO_SOURCE_NOTE_BY_PROVENANCE[provenance_of(target.source_ref)]}。"
                    "同步只适用于由数据源采集而来、背后有真实源库表的对象。"
                )

            contract = (
                db.query(MaterializationContract)
                .filter(
                    MaterializationContract.ontology_id == ontology_id,
                    MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
                    MaterializationContract.target_id == target.id,
                )
                .first()
            )
            # 严格解析：上面的 has_physical_source 已保证解得出，这里不会是 None。
            source_table = source_table_of(target.source_ref)
            # 落点整个不给选：库恒为 ODS_DATABASE，表名按 ods_{数据域}_{原始表名} 生成。
            # 调用方传入的 target_ods_database / database_prefix / target_ods_table
            # 一律不参与——同步就是「源头数据 → 数仓 ODS」，分层是加工任务的事。
            ods_database = ODS_DATABASE
            ods_table = target_ods_table_name(db, ontology_id, target)

            return {
                "source": source_table,
                "target": f"{ods_database}.{ods_table}",
                "object_type": target.name,
                # 业务名派生一次、存进 Spec：任务名要写「同步 · 客户分组」而不是
                # 「同步 · _d71df877e93eac81.tabCustomer Group」，而 ``name_from_spec``
                # 只拿得到 Spec（手工结构化起草那条路径没有 db 会话可查对象）。
                "object_display_name": target.display_name or target.name,
                "ontology_id": ontology_id,
                # 目标数据源：执行器缺它就退回「只渲染作业配置、不真跑」。链上游会在
                # execute 的 context 里传，但手工建的独立任务没有上游——不带进 Spec 的话
                # 人在界面上选了目标仓也白选，任务会「成功」却什么都没搬。
                "source_datasource_id": context.get("source_datasource_id") or None,
                "target_datasource_id": context.get("target_datasource_id") or None,
                "target_ods_database": ods_database,
                "target_ods_table": ods_table,
                # 表单显式选的装载方式/分区键优先；否则回退契约，再回退默认。
                "mode": context.get("mode")
                or (contract.load_strategy if contract else "full"),
                "primary_keys": list(context.get("primary_keys") or []),
                "sequence_column": context.get("sequence_column"),
                "incremental_column": context.get("incremental_column")
                or context.get("partition_key")
                or (contract.partition_key if contract else None),
                "initial_watermark": context.get("initial_watermark"),
                "late_arrival_policy": context.get("late_arrival_policy") or "strict",
                "idempotency_strategy": context.get("idempotency_strategy")
                or "primary_key_upsert",
                "partition_key": context.get("partition_key")
                or (contract.partition_key if contract else None),
                "delete_policy": context.get("delete_policy") or "ignore",
                # 调度频率：**同步任务最该有的一个参数**。入仓作业跑一次不叫管道，
                # 而此前 Spec 里根本没有这个键——执行器 `spec.get("refresh_cron")` 恒为
                # None，产出的 DAG 一律 schedule=None（只能手动点）。想让同步定时跑，
                # 只能绕到物化弹窗里逐实体改契约的 refresh_cron，没人找得到。
                # 留空 = 仅手动触发（与加工/聚合任务的口径一致）。
                "refresh_cron": (
                    str(context.get("refresh_cron") or context.get("schedule") or "").strip()
                    or None
                ),
                # 引擎随目标数据源走（人显式选的除外）：选了 postgres 目标仓却产
                # Hive DDL/sink，建表那一步必挂。
                "engine": resolve_spec_engine(db, context, contract),
                "preservation": decide_preservation(intent, source_table),
                # 凭据不入 Spec：只放连接别名（= Airflow conn_id），执行侧按别名取连接串。
                # 默认值取 job_planner 的同一常量——三处各写各的 "erp_readonly" /
                # "default" 会让「改默认连接」这件事漏掉其中一处。
                "source_ref_alias": context.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS,
                # 任务级 Flink 执行参数（并行度/队列/提交目标/checkpoint/额外 -D）。
                # 只落人真填了的项——留空 = 跟随设置页默认，不在 Spec 里凝固成快照。
                **flink_params.from_context(context),
            }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return self.name_from_spec(spec)

    def name_from_spec(self, spec: dict[str, Any]) -> str:
        """任务名用**业务名**，不用物理坐标。

        此前是 ``同步 · {源库.源表} → {ods 库.ods 表}``，于是任务列表里整屏都是
        ``同步 · _d71df877e93eac81.tabCustomer Group → ods.ods_erpnext_tab_customer_group``
        ——源库名是个哈希、源表名是 doctype 原样，一眼看不出这条任务在同步什么业务对象。
        落点恒为数仓 ODS（后端固定规则），写出来也不构成区分度；真正的物理坐标在任务
        详情的「源表 / 目标表」两行里，核对不丢。
        """
        label = spec.get("object_display_name") or spec.get("object_type")
        if not label:
            raise ValueError("同步 Spec 缺少 object_type，无法生成任务名称")
        return f"同步 · {label} → 数仓 ODS"
