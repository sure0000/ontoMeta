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


def _job(name: str, source_alias: str, source_table: str, sink_table: str, mode: str,
         partition_key: str | None) -> dict[str, Any]:
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
        "sink": [{"plugin": "Hive", "table": sink_table, "save_mode": "overwrite"}],
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

        jobs: dict[str, dict] = {
            f"sync_{spec.get('object_type') or 'job'}": _job(
                f"sync_{spec.get('object_type')}", alias, source, target, mode, partition_key
            )
        }

        preservation = spec.get("preservation") or {}
        if preservation.get("preserve"):
            stg_table = f"stg.{source.split('.')[-1]}"
            jobs[f"preserve_{spec.get('object_type') or 'job'}"] = _job(
                f"preserve_{spec.get('object_type')}", alias, source, stg_table, "full", None
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
        return {
            **self._artifacts(spec),
            "handoff": "SeaTunnel + DolphinScheduler",
            "note": "作业配置中只含数据源别名，连接串由 SeaTunnel 侧解析",
        }
