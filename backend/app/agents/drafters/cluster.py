"""⓪ 集群拓扑 Drafter —— 「给出服务器登录信息与部署要求，智能体自动部署底层服务」。

**凭据绝不进 LLM 上下文，也绝不进 Spec。** 本 Drafter 只产出「角色 → 主机别名」
的拓扑规格；SSH 口令/私钥由独立密钥存储持有，执行侧按别名取。
Validation Gate 另有一道 ``credential_in_spec`` 兜底扫描。

组件白名单严格限定在 **Bigtop Manager 实际纳管**的范围内——
超出的组件（调度器、MPP/OLAP、SQL 网关、Ranger）BM 管不了，
在此拒绝比让它跑到执行期失败要好。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.common import require_context
from app.agents.drafters.base import Drafter

# Bigtop Manager v1.1.0 实际纳管的组件（Bigtop 3.3.0 + Infra 1.0.0 + Extra 1.0.0）。
# 来源：BIGTOP-4129 Roadmap。BM 版本升级后需同步核对本清单。
BM_MANAGED_SERVICES: frozenset[str] = frozenset(
    {
        # Bigtop 3.3.0 stack
        "zookeeper", "hdfs", "yarn", "mapreduce", "hive", "spark",
        "flink", "tez", "hbase", "kafka", "solr",
        # Infra 1.0.0
        "mysql", "prometheus", "grafana",
        # Extra 1.0.0
        "seatunnel",
    }
)

# BM 管不了、需独立部署运维的组件（双轨运维边界）。
NOT_BM_MANAGED: dict[str, str] = {
    "dolphinscheduler": "调度器",
    "airflow": "调度器",
    "kyuubi": "SQL 网关",
    "doris": "MPP/OLAP",
    "starrocks": "MPP/OLAP",
    "clickhouse": "MPP/OLAP",
    "trino": "MPP/OLAP",
    "ranger": "权限（Bigtop 3.3.0 stack 未含，需自行打包）",
}

_ALIASES = {"hadoop": ["hdfs", "yarn", "mapreduce"], "zk": ["zookeeper"]}

# PoC 最小集群的默认角色分布。
_DEFAULT_LAYOUT = {
    "zookeeper": "all",
    "hdfs": "namenode:first,datanode:all",
    "yarn": "resourcemanager:first,nodemanager:all",
    "hive": "metastore:first,hiveserver2:first",
    "spark": "client:all",
}


class ClusterDrafter(Drafter):
    kind = "cluster"
    required_context = ("hosts",)

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        hosts = [str(h).strip() for h in context["hosts"] if str(h).strip()]
        if not hosts:
            raise ValueError("hosts 为空")
        self._reject_credentials(context, hosts)

        services = self._resolve_services(intent, context.get("services"))
        unmanaged = [s for s in services if s in NOT_BM_MANAGED]
        if unmanaged:
            raise ValueError(
                "以下组件 Bigtop Manager 不纳管，需独立部署运维："
                + "、".join(f"{s}（{NOT_BM_MANAGED[s]}）" for s in unmanaged)
            )
        unknown = [s for s in services if s not in BM_MANAGED_SERVICES]
        if unknown:
            raise ValueError(f"未知组件：{'、'.join(unknown)}")
        if not services:
            raise ValueError("未能从意图中识别出待部署组件，请在 context.services 显式指定")

        return {
            # 只有主机别名，没有任何连接凭据。
            "hosts": hosts,
            "services": sorted(services),
            "layout": {s: _DEFAULT_LAYOUT.get(s, "all") for s in sorted(services)},
            "stack": context.get("stack") or "bigtop-3.3.0",
            "credential_ref": context.get("credential_ref") or "cluster_ssh_default",
        }

    @staticmethod
    def _reject_credentials(context: dict[str, Any], hosts: list[str]) -> None:
        """凭据一旦出现在上下文里就拒绝——这是安全边界，不能只靠事后扫描。"""
        bad_keys = [
            k
            for k in context
            if any(
                t in str(k).lower()
                for t in ("password", "passwd", "secret", "private_key", "token", "credential")
            )
            and k != "credential_ref"
        ]
        if bad_keys:
            raise ValueError(
                f"上下文中不得包含凭据字段：{'、'.join(bad_keys)}。"
                "请改用 credential_ref 指向独立密钥存储，凭据不进 LLM 上下文。"
            )
        # user@host:password 形式的内联凭据
        for host in hosts:
            if ":" in host and "@" in host:
                raise ValueError(f"主机项 {host} 疑似内联凭据，只允许填主机别名")

    @staticmethod
    def _resolve_services(intent: str, explicit: Any) -> list[str]:
        if explicit:
            raw = [str(s).strip().lower() for s in explicit]
        else:
            text = (intent or "").lower()
            raw = [
                name
                for name in sorted(BM_MANAGED_SERVICES | set(NOT_BM_MANAGED) | set(_ALIASES))
                if re.search(rf"\b{re.escape(name)}\b", text)
            ]
        expanded: list[str] = []
        for name in raw:
            expanded.extend(_ALIASES.get(name, [name]))
        return sorted(set(expanded))

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return f"集群 · {len(spec.get('hosts') or [])} 节点 · {'/'.join(spec.get('services') or [])}"
