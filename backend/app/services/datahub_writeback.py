"""本体 → DataHub 回写（M7）：元数据闭环的另一半。

DataHub 提供技术元数据，ontoMeta 产出业务元数据——但此前是**单向**的：
本体成果回不到 DataHub，别的消费方（BI 工具、其它 Agent、数据目录搜索）
看不到业务语义。本模块把业务命名、描述、术语、域回灌，闭合这个环。

三条安全约束：

1. **只回写已发布本体**——草稿态的命名还会变，推到 DataHub 会污染全域元数据。
2. **preview / apply 分离**——先看清楚将要改什么，再决定是否落。
   mutation 结构已对照 DataHub 开源 GraphQL schema 核实一致，但目标实例的具体版本
   仍未核实，preview 是必要防线，首次 apply 须在非生产实例。
3. **绝不用空值覆盖**——本体没写描述、而 DataHub 已有描述时跳过；
   回写是补充语义，不是清空别人的成果。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.connectors import datahub as dh
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)


@dataclass
class WritebackChange:
    """一条待回写的变更。``current`` 为 None 表示 DataHub 侧当前无值。"""

    operation: str  # dataset_description / field_description / domain / terms
    urn: str
    target: str  # 人可读的目标（表名 / 表名.字段名）
    new_value: str
    current: str | None = None
    skipped_reason: str | None = None

    @property
    def will_apply(self) -> bool:
        return self.skipped_reason is None


@dataclass
class WritebackPlan:
    ontology_id: str
    changes: list[WritebackChange] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def applicable(self) -> list[WritebackChange]:
        return [c for c in self.changes if c.will_apply]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ontology_id": self.ontology_id,
            "blocked_reason": self.blocked_reason,
            "total": len(self.changes),
            "applicable": len(self.applicable),
            "skipped": len(self.changes) - len(self.applicable),
            "changes": [asdict(c) for c in self.changes],
        }


def _business_text(display_name: str | None, description: str | None) -> str:
    parts = [p.strip() for p in (display_name, description) if p and p.strip()]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " · ".join(parts)


class DataHubWritebackService:
    def build_plan(self, db: Session, ontology_id: str) -> WritebackPlan:
        """列出将要回写的全部变更。纯读，不触碰 DataHub。"""
        ontology = db.query(Ontology).filter(Ontology.id == ontology_id).first()
        if ontology is None:
            raise LookupError("本体不存在")

        plan = WritebackPlan(ontology_id=ontology_id)
        if ontology.status != OntologyStatus.PUBLISHED.value:
            plan.blocked_reason = (
                f"仅已发布本体可回写（当前 {ontology.status}）——"
                "草稿态命名仍会变动，推到 DataHub 会污染全域元数据"
            )
            return plan

        domain = (
            db.query(DomainContext)
            .filter(DomainContext.id == ontology.domain_context_id)
            .first()
        )
        domain_urn = domain.datahub_domain_id if domain else None

        objects = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .order_by(ObjectType.name)
            .all()
        )
        for obj in objects:
            if not obj.source_ref:
                plan.changes.append(
                    WritebackChange(
                        operation="dataset_description",
                        urn="",
                        target=obj.name,
                        new_value="",
                        skipped_reason="对象无 source_ref，无法定位 DataHub 数据集",
                    )
                )
                continue

            text = _business_text(obj.display_name, obj.description)
            plan.changes.append(
                WritebackChange(
                    operation="dataset_description",
                    urn=obj.source_ref,
                    target=obj.name,
                    new_value=text,
                    skipped_reason=None if text else "本体未填写业务语义，跳过（不用空值覆盖）",
                )
            )

            if domain_urn:
                plan.changes.append(
                    WritebackChange(
                        operation="domain",
                        urn=obj.source_ref,
                        target=obj.name,
                        new_value=domain_urn,
                    )
                )

            if obj.canonical_term_id:
                plan.changes.append(
                    WritebackChange(
                        operation="terms",
                        urn=obj.source_ref,
                        target=obj.name,
                        new_value=obj.canonical_term_id,
                    )
                )

            for prop in sorted(obj.properties, key=lambda p: p.name):
                field_text = _business_text(prop.display_name, prop.description)
                if not field_text:
                    continue
                plan.changes.append(
                    WritebackChange(
                        operation="field_description",
                        urn=obj.source_ref,
                        target=f"{obj.name}.{prop.name}",
                        new_value=field_text,
                    )
                )
        return plan

    async def apply(
        self, db: Session, ontology_id: str, *, connector=None
    ) -> dict[str, Any]:
        """执行回写。失败逐条记录，不因单条失败而中断整体。"""
        plan = self.build_plan(db, ontology_id)
        if plan.blocked_reason:
            return {**plan.to_dict(), "applied": 0, "failed": 0, "errors": []}

        owns_connector = connector is None
        if owns_connector:
            connector = dh.DataHubConnector()

        applied = 0
        errors: list[dict[str, str]] = []
        try:
            for change in plan.applicable:
                try:
                    await self._apply_one(connector, change)
                    applied += 1
                except dh.DataHubWriteError as exc:
                    errors.append(
                        {
                            "operation": change.operation,
                            "target": change.target,
                            "error": str(exc.cause),
                        }
                    )
        finally:
            if owns_connector:
                await connector.aclose()

        return {
            **plan.to_dict(),
            "applied": applied,
            "failed": len(errors),
            "errors": errors,
        }

    @staticmethod
    async def _apply_one(connector, change: WritebackChange) -> None:
        if change.operation == "dataset_description":
            await dh.update_dataset_description(connector, change.urn, change.new_value)
        elif change.operation == "field_description":
            field_path = change.target.split(".", 1)[1]
            await dh.update_field_description(
                connector, change.urn, field_path, change.new_value
            )
        elif change.operation == "domain":
            await dh.set_domain(connector, change.urn, change.new_value)
        elif change.operation == "terms":
            await dh.add_glossary_terms(connector, change.urn, [change.new_value])
        else:  # pragma: no cover —— 防御：新增操作类型必须显式实现
            raise ValueError(f"未实现的回写操作：{change.operation}")

    def apply_sync(self, db: Session, ontology_id: str, *, connector=None) -> dict:
        """同步入口，供 FastAPI 同步路由调用。"""
        return asyncio.run(self.apply(db, ontology_id, connector=connector))
