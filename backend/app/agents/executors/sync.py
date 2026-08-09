"""① 同步作业 Executor —— 产出 SeaTunnel 作业配置。

**凭据不进产物**：作业配置里只写数据源别名，实际连接串由 SeaTunnel 侧按别名解析。
比照 ``DataSource.dsn_secret_ref``「仅存引用或加密串」的既有做法。

保全策略生效时额外产出一份贴源（STG）作业——保全是数据安全问题，
不能因为「省事」而悄悄跳过。
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.executors.base import Executor
from app.services.job_planner import DEFAULT_SOURCE_ALIAS
from app.warehouse.jobs.seatunnel import SINK_PLUGINS

# 装载方式 → sink 落地语义。**增量必须追加**：增量只读出一个时间切片，再按覆盖写
# 就是拿切片把整张表换掉——历史数据当场没了。此前这里恒为 overwrite，而真正执行的
# SeaTunnelAdapter._sink 早已按 mode 分档，两处就这样分叉了。
_SAVE_MODE = {"full": "overwrite", "incremental": "append", "cdc": "append"}


def _job(name: str, source_alias: str, source_table: str, sink_table: str, mode: str,
         partition_key: str | None, engine: str) -> dict[str, Any]:
    source: dict[str, Any] = {
        "plugin": "Jdbc",
        # 只读从库别名；连接串由 SeaTunnel 侧解析，绝不写入本产物。
        "datasource_alias": source_alias,
        "table": source_table,
    }
    if mode == "incremental" and partition_key:
        source["partition_column"] = partition_key
        source["query_hint"] = f"WHERE {partition_key} >= '${{start_time}}'"
    elif mode == "cdc":
        source["plugin"] = "MySQL-CDC"
    return {
        "job": {"name": name, "mode": "BATCH" if mode != "cdc" else "STREAMING"},
        "source": [source],
        "sink": [{
            # sink 插件随目标引擎走，不是恒 Hive——目标是 postgres/mysql 却渲染
            # Hive sink，人照着这份配置去跑只会一头雾水。专用连接器的映射复用
            # SeaTunnelAdapter 那一份，不在这里另抄一遍。
            "plugin": SINK_PLUGINS.get((engine or "").lower(), "Jdbc"),
            "table": sink_table,
            "save_mode": _SAVE_MODE.get(mode, "overwrite"),
        }],
    }


class SyncExecutor(Executor):
    kind = "sync"

    def _artifacts(self, spec: dict[str, Any]) -> dict[str, Any]:
        source = spec["source"]
        target = spec["target"]
        # 与 SyncDrafter / job_planner 同一个缺省别名；此前这里写的是 "default"，
        # 一个执行侧根本没有的 conn_id——真走到这条兜底只会在运行时找不到连接。
        alias = spec.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS
        mode = spec.get("mode") or "full"
        partition_key = spec.get("partition_key")
        engine = spec.get("engine") or "hive"

        jobs: dict[str, dict] = {
            f"sync_{spec.get('object_type') or 'job'}": _job(
                f"sync_{spec.get('object_type')}", alias, source, target, mode,
                partition_key, engine,
            )
        }

        preservation = spec.get("preservation") or {}
        if preservation.get("preserve"):
            stg_table = f"stg.{source.split('.')[-1]}"
            jobs[f"preserve_{spec.get('object_type') or 'job'}"] = _job(
                f"preserve_{spec.get('object_type')}", alias, source, stg_table,
                "full", None, engine,
            )

        return {
            "jobs": {k: json.dumps(v, ensure_ascii=False, indent=2) for k, v in jobs.items()},
            "source": source,
            "target": target,
            "mode": mode,
            "preserved": bool(preservation.get("preserve")),
            "preservation_reason": preservation.get("reason"),
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        artifacts = self._artifacts(spec)
        return {
            "action": "create_seatunnel_jobs",
            "job_names": sorted(artifacts["jobs"]),
            "source": artifacts["source"],
            "target": artifacts["target"],
            "mode": artifacts["mode"],
            "preserved": artifacts["preserved"],
            "preservation_reason": artifacts["preservation_reason"],
            "side_effects": "无（dry-run 仅渲染作业配置）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 未配 target_datasource，退回「仅产出」（与 transform/metric 同逻辑）
        target_datasource_id = spec.get("target_datasource_id") or context.get("target_datasource_id")
        if not target_datasource_id:
            return {
                **self._artifacts(spec),
                "handoff": "SeaTunnel + DolphinScheduler",
                "note": "未配置 target_datasource_id，ontoMeta 只生成作业配置，不执行（链上游会传入此字段）",
            }

        # 进链执行路径（P3-1）：sync = 对单对象跑搬运。复用 materialization_runner 的
        # 搬运通道（runner/docker），传 selected_targets=[对象名] 只搬那一个对象。
        # 搬运通道产 dag_id，使其可被 pipeline_compiler 串进链 DAG。
        object_type = spec.get("object_type")
        if not object_type:
            return {
                **self._artifacts(spec),
                "handoff": "SeaTunnel + DolphinScheduler",
                "note": "spec 缺 object_type，无法定位要搬的对象，退回仅产出",
            }

        try:
            from app.services import materialization_runner
        except ImportError as exc:
            return {
                **self._artifacts(spec),
                "handoff": "import_error",
                "note": f"搬运模块导入失败：{exc}，退回仅产出",
            }

        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺 ontology_id")
        engine = spec.get("engine") or "hive"

        from app.database import SessionLocal

        with SessionLocal() as db:
            try:
                receipt = materialization_runner.run(
                    db,
                    ontology_id,
                    target_datasource_id=target_datasource_id,
                    engine=engine,
                    database_prefix=spec.get("database_prefix"),
                    load_strategy=spec.get("mode"),
                    # 只搬这一个对象（按实体名裁剪）
                    selected_targets=[object_type],
                    artifact_id=context.get("artifact_id"),
                )
            except materialization_runner.MaterializationError as exc:
                # 未配 Airflow / 投递失败：退回仅产出，不静默假装执行了
                return {
                    **self._artifacts(spec),
                    "handoff": "SeaTunnel + DolphinScheduler",
                    "note": f"搬运未执行（{exc}），退回仅产出",
                }
            return receipt
