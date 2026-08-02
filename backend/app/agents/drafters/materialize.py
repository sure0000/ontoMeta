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


class MaterializeDrafter(Drafter):
    kind = "materialize"

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, "ontology_id", "target_datasource_id")
        return {
            "ontology_id": context["ontology_id"],
            "target_datasource_id": context["target_datasource_id"],
            "engine": context.get("engine") or "hive",
            "database_prefix": context.get("database_prefix"),
            "database_overrides": dict(context.get("database_overrides") or {}),
            "table_overrides": dict(context.get("table_overrides") or {}),
            "load_strategy": context.get("load_strategy"),
            # 搬运工具（seatunnel/datax/flink）；空 = 默认 seatunnel。
            "sync_tool": context.get("sync_tool"),
            "selected_targets": list(context.get("selected_targets") or []) or None,
            "overrides": dict(context.get("overrides") or {}),
        }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        engine = spec.get("engine") or "hive"
        return (intent or f"物化 → {engine}").strip()[:80]
