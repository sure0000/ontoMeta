"""⓪ 集群拓扑 Executor —— 交给 Bigtop Manager，ontoMeta 不自行 SSH。

三条硬约束：
1. **不自行 SSH**：部署由 BM Agent 完成，ontoMeta 不碰密钥分发，责任边界清晰。
2. **凭据只传引用进 Spec**：Spec 里只有 ``credential_ref``，没有明文。BM 的 API 却**要求内联
   SSH 明文口令/私钥**（无凭据引用模型），故下发时的真实密钥**只从运行时 ``context`` 取**
   （发布者在 execute 时提供），在 dispatch 边界直接转发给 BM，不落盘、不进 Spec / LLM 上下文。
3. **接口按源码核实，但未在真实实例验证**：BM 1.1.0 的 REST 接口已对照 ``release-1.1.0`` 源码
   实现（见 ``connectors/bigtop_manager.py``）。默认仍**只产载荷、不下发**；要真正下发须在 context
   显式置 ``allow_dispatch=True`` 且提供 ``bm_endpoint`` 与运行时凭据——把「下发」变成一次显式选择。
   **首次真实下发必须在非生产集群核实。**
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agents.executors.base import Executor
from app.connectors import bigtop_manager as bm


class ClusterExecutor(Executor):
    kind = "cluster"

    @staticmethod
    def _payload(spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "stack": spec.get("stack") or "bigtop-3.3.0",
            "hosts": spec.get("hosts") or [],
            "services": spec.get("services") or [],
            "layout": spec.get("layout") or {},
            # 只传引用，明文由 BM 侧按别名解析。
            "credential_ref": spec.get("credential_ref"),
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload(spec)
        hosts = payload["hosts"]
        services = payload["services"]
        return {
            "action": "deploy_cluster_via_bigtop_manager",
            "will_install": [
                {"service": s, "placement": payload["layout"].get(s, "all")}
                for s in services
            ],
            "host_count": len(hosts),
            "hosts": hosts,
            "payload": payload,
            "irreversible": True,
            "side_effects": "无（dry-run 不下发）；执行后将在目标主机安装服务，不可自动回滚",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload(spec)
        if not context.get("allow_dispatch") or not context.get("bm_endpoint"):
            return {
                "dispatched": False,
                "payload": payload,
                "handoff": "Bigtop Manager",
                "note": (
                    "未下发：需在 context 显式置 allow_dispatch=true 并提供 bm_endpoint、"
                    "BM 登录凭据（bm_username/bm_password）与 ssh_credentials 才会真实提交。"
                    "接口已按 BM 1.1.0 源码构造，但首次真实下发须在非生产集群核实。"
                ),
            }
        # 到这里说明发布者已显式确认下发。
        return asyncio.run(self._dispatch(payload, context))

    @staticmethod
    async def _dispatch(
        payload: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """把拓扑规格翻成 BM command 并提交。凭据全部来自 context（运行时），不进 Spec。"""
        username = context.get("bm_username")
        password = context.get("bm_password")
        ssh = context.get("ssh_credentials")
        missing = [
            name
            for name, value in (
                ("bm_username", username),
                ("bm_password", password),
                ("ssh_credentials", ssh),
            )
            if not value
        ]
        if missing:
            # 缺凭据宁可显式报错，也不下发一个连不上的作业（不静默降级）。
            raise ValueError(
                "BM 下发缺少运行时凭据（只能经 context 注入、不进 Spec）：" + "、".join(missing)
            )

        # 测试注入用；生产 context 无此键，客户端自建真实连接。
        client = bm.BigtopManagerClient(
            context["bm_endpoint"], client=context.get("bm_http_client")
        )
        try:
            await client.login(str(username), str(password))
            command = bm.build_cluster_command(
                name=context.get("cluster_name") or "ontometa-cluster",
                display_name=(
                    context.get("cluster_display_name")
                    or context.get("cluster_name")
                    or "ontoMeta 集群"
                ),
                hostnames=payload["hosts"],
                ssh=ssh,
                cluster_type=int(context.get("cluster_type", 1)),
            )
            job = await client.submit_command(command)
        finally:
            await client.aclose()

        cluster_id = int(job.get("clusterId") or job.get("id") or 0)
        return {
            "dispatched": True,
            "handoff": "Bigtop Manager",
            "bm_endpoint": context["bm_endpoint"],
            "cluster_job": job,
            # 服务安装为后续 command（configs 需发布者补全实际值，不臆造）。
            "service_command": bm.build_service_command(
                cluster_id, payload["services"], payload["layout"]
            ),
            "note": (
                "已提交建集群 command，返回 Job（用 clusters/{id}/jobs/{jobId} 轮询进度）；"
                "服务安装为后续 command，其 configs 需发布者按部署实际值补全。"
                "凭据仅经 context 注入、未落盘。首次真实下发须在非生产集群核实。"
            ),
        }

