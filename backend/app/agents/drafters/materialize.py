"""⑤ 物化 Drafter —— 「把已发布本体一键落到目标数据源」。

与其它 Drafter 不同，物化由表单驱动而非自然语言：结构（建什么表、落哪层）已在
本体与物化契约里确定，Drafter 只把弹窗里的目标存储/引擎/勾选实体/覆盖项收敛成
声明式 Spec，交由 Executor 调 ``services/materialization_runner`` 落库。

**凭据不进 Spec**：只存目标数据源 id，实际 DSN 由 Executor 侧按 id 取
（比照 ``DataSource.dsn_secret_ref``「仅存引用」的既有做法）。
"""

from __future__ import annotations

from typing import Any

from app.agents.common import require_context
from app.agents.drafters.base import Drafter
from app.models.warehouse import MaterializationLayer


def _database_overrides(context: dict[str, Any]) -> dict[str, str]:
    """目标库：逐层的 ``database_overrides``，外加把标量 ``target_database`` 摊到各层。

    执行侧只认「层 → 库名」这一种形式。而人（和对话）说的是「物化到 dw 库」——一个库，
    不分层；物化弹窗里也是**一个**目标库输入框，提交时逐层填上同一个值。此前建数表单
    收上来的 ``target_database`` 在这里被静默丢掉，选中的库根本没生效，表落回默认库。

    逐层显式给的优先：``database_overrides`` 命中的层不被标量覆盖。
    """
    overrides = {str(k): str(v) for k, v in (context.get("database_overrides") or {}).items()}
    database = str(context.get("target_database") or "").strip()
    if database:
        for layer in MaterializationLayer:
            overrides.setdefault(layer.value, database)
    return overrides


class MaterializeDrafter(Drafter):
    kind = "materialize"
    # 目标数据源无从推导（本体不知道要落到哪个仓），必须由调用方选定。
    required_context = ("ontology_id", "target_datasource_id")

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        return {
            "ontology_id": context["ontology_id"],
            "target_datasource_id": context["target_datasource_id"],
            "engine": context.get("engine") or "hive",
            "database_prefix": context.get("database_prefix"),
            "database_overrides": _database_overrides(context),
            "table_overrides": dict(context.get("table_overrides") or {}),
            "load_strategy": context.get("load_strategy"),
            # 搬运工具（seatunnel/datax/flink）；空 = 默认 seatunnel。
            "sync_tool": context.get("sync_tool"),
            "selected_targets": list(context.get("selected_targets") or []) or None,
            "overrides": dict(context.get("overrides") or {}),
            # 整批调度：弹窗是逐实体配 cron（写进各自契约的 refresh_cron），要求调用方先知道
            # 契约 id。对话里「每天凌晨跑一次」说的是整批，故提到 Spec 顶层，由 Executor 展开
            # 到本次选中的各契约；``overrides`` 里显式给了 refresh_cron 的实体不受影响。
            "refresh_cron": (str(context.get("refresh_cron")).strip() or None)
            if context.get("refresh_cron") is not None
            else None,
        }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        engine = spec.get("engine") or "hive"
        return (intent or f"物化 → {engine}").strip()[:80]
