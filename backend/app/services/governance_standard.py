"""规约自治理：版本化 + 落库 + 存量 re-lint（G3）。

**版本模型**：规约由代码定义版本常量并登记进 ``_REGISTRY``（判定逻辑住 lint.py/validation.py，
纯数据规则无从执行，故版本必须是代码）。「发布」= 在已登记版本里选一个置为生效；DB 记录
生效版本 + 审计历史（见 models/governance）。零发布记录时回落内置 ``DEFAULT_STANDARD``——
零配置也能跑，且与 G0/G1/G2 行为一致。

这实现了 draft→confirm→publish 的自治理：``available_versions`` 是候选（draft 面），
``publish`` 是确认+发布，``history`` 是审计。规约升级 = 上线新版本常量 + publish + ``relint`` 存量。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.governance import lint_logical_table
from app.governance.standard import DEFAULT_STANDARD, GovernanceStandard
from app.models.governance import GovernanceStandardRecord

# 版本注册表：{version: 代码定义的规约常量}。新版本上线时在此登记。
_REGISTRY: dict[str, GovernanceStandard] = {DEFAULT_STANDARD.version: DEFAULT_STANDARD}


def register_standard(standard: GovernanceStandard) -> None:
    """登记一个新版本规约常量（供未来版本迭代调用）。"""
    _REGISTRY[standard.version] = standard


class GovernanceStandardService:
    def available_versions(self) -> list[str]:
        return sorted(_REGISTRY)

    def get_active(self, db: Session) -> GovernanceStandard:
        """当前生效规约：取最近发布的记录 → 回注册表取代码常量；无记录则内置默认。

        绝不递归调用 ``active_standard``（那会绕回本方法）——直接读注册表。
        """
        rec = (
            db.query(GovernanceStandardRecord)
            .filter(GovernanceStandardRecord.status == "published")
            .order_by(GovernanceStandardRecord.activated_at.desc())
            .first()
        )
        if rec and rec.version in _REGISTRY:
            return _REGISTRY[rec.version]
        return DEFAULT_STANDARD

    def active_version(self, db: Session) -> str:
        return self.get_active(db).version

    def history(self, db: Session) -> list[GovernanceStandardRecord]:
        return (
            db.query(GovernanceStandardRecord)
            .order_by(GovernanceStandardRecord.created_at.desc())
            .all()
        )

    def publish(
        self, db: Session, version: str, *, note: str | None = None
    ) -> GovernanceStandardRecord:
        """把某个已登记版本置为生效。未登记的版本拒绝——不能发布代码里不存在的规约。"""
        if version not in _REGISTRY:
            raise ValueError(
                f"未知规约版本 {version}；可用版本：{sorted(_REGISTRY)}"
            )
        # 现有已发布记录降级为 superseded——任一时刻至多一个 published。
        for rec in (
            db.query(GovernanceStandardRecord)
            .filter(GovernanceStandardRecord.status == "published")
            .all()
        ):
            rec.status = "superseded"

        standard = _REGISTRY[version]
        record = GovernanceStandardRecord(
            version=version,
            status="published",
            payload_json=json.dumps(standard.to_dict(), ensure_ascii=False),
            note=note,
            activated_at=datetime.now(timezone.utc),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def relint(self, db: Session, ontology_id: str) -> dict:
        """按当前生效规约对某本体的物理产物做存量体检。

        规约升级（发布更严版本）后，用它批量找出「在新规约下不再合规」的历史表——
        产物由 WarehouseGenerator 从本体+契约编译，故 re-lint 与真实 DDL 咬同一份物理名。
        """
        from app.services.warehouse_generator import WarehouseGenerator

        standard = self.get_active(db)
        plan = WarehouseGenerator().build_logical_schema(db, ontology_id)
        violations: list[dict] = []
        for table in plan.schema.tables:
            for v in lint_logical_table(table, standard):
                violations.append({"table": table.qualified_name, **v.to_dict()})
        return {
            "standard_version": standard.version,
            "table_count": len(plan.schema.tables),
            "violation_count": len(violations),
            "violations": violations,
        }
