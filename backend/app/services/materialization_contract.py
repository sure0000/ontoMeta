"""物化契约服务：机器推导默认值 + 人工覆盖 + 三方合并。

契约的核心不变量：**机器每次重新推导，不得覆盖人工钉住的字段。**
钉住的判定复用 ``services/edit.py`` 的 ``_mark_overridden``（overridden_fields
是 JSON 字符串数组），与 ObjectType / Property 的溯源语义保持一致——若在此处
另写一套，两份合并逻辑迟早会分叉。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.governance import active_standard
from app.models import (
    BusinessLogic,
    MaterializationContract,
    ObjectType,
    Property,
    RelationType,
)
from app.models.warehouse import (
    LoadStrategy,
    ScdType,
    TargetKind,
)
from app.services.edit import _mark_overridden

# 机器推导可写的列。refresh_cron 不在其中——刷新频率是业务决策，机器无判定依据。
_MACHINE_FIELDS = (
    "target_layer",
    "target_engines",
    "load_strategy",
    "partition_key",
    "scd_type",
    "materialized",
)

# 人工 patch 的字段名 → 数据库列名。
_PATCH_TO_COLUMN = {
    "target_layer": "target_layer",
    "engines": "target_engines",
    "load_strategy": "load_strategy",
    "partition_key": "partition_key",
    "scd_type": "scd_type",
    "refresh_cron": "refresh_cron",
    "materialized": "materialized",
}

DEFAULT_ENGINES = ["doris"]

# 时间语义字段作为分区键的候选（见 evidence_builder._infer_semantic_type）。
_TIME_SEMANTIC = "datetime"


class MaterializationContractService:
    # ---------- 推导 ----------

    def derive(self, db: Session, ontology_id: str) -> list[dict]:
        """按本体实体推导物化契约默认值。纯计算，不写库。

        分层规则（分层只是契约的一个属性，不是建模范式）：

        | 实体 | 判据 | 层 | 是否物化 |
        |---|---|---|---|
        | ObjectType | table_role=business_object | dim | 是 |
        | ObjectType | table_role=bridge | dwd | 是（关系实现表） |
        | ObjectType | table_role=data_table/technical | — | 否 |
        | RelationType | structure_type=fact_table/bridge_table | dwd | 是 |
        | RelationType | structure_type=foreign_key | — | 否（外键是列声明，不是独立表） |
        | RelationType | 其它（derivation 等） | — | 否 |
        | BusinessLogic | — | ads | 是 |
        """
        out: list[dict] = []
        # 落层判据来自治理规约（app.governance），此处不再自持层字面值——层的定义归规约，
        # 「是否物化 / derivation_reason」是本服务的派生语义，仍在此决定。
        layering = active_standard(db).layering

        for obj in (
            db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        ):
            role = obj.table_role or "business_object"
            if role == "business_object":
                layer, materialized, reason = (
                    layering.role_to_layer.get("business_object", "dim"),
                    True,
                    "table_role=business_object → 维度表",
                )
            elif role == "bridge":
                layer, materialized, reason = (
                    layering.role_to_layer.get("bridge", "dwd"),
                    True,
                    "table_role=bridge → 关系实现表，落 DWD",
                )
            else:
                layer, materialized, reason = (
                    layering.role_to_layer.get(role, "dim"),
                    False,
                    f"table_role={role} → 非业务对象，不落物理表",
                )
            strategy, partition_key = self._load_strategy_for_object(db, obj.id)
            out.append(
                {
                    "target_kind": TargetKind.OBJECT_TYPE.value,
                    "target_id": obj.id,
                    "target_layer": layer,
                    "target_engines": json.dumps(DEFAULT_ENGINES, ensure_ascii=False),
                    "load_strategy": strategy if materialized else LoadStrategy.FULL.value,
                    "partition_key": partition_key if materialized else None,
                    "scd_type": ScdType.NONE.value,
                    "materialized": materialized,
                    "derivation_reason": reason,
                }
            )

        for rel in (
            db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
        ):
            structure = rel.structure_type or "other"
            if structure in ("fact_table", "bridge_table"):
                layer, materialized, reason = (
                    layering.structure_to_layer.get(structure, "dwd"),
                    True,
                    f"structure_type={structure} → 明细事实表",
                )
            elif structure == "foreign_key":
                layer, materialized, reason = (
                    layering.structure_to_layer.get("foreign_key", "dwd"),
                    False,
                    "structure_type=foreign_key → 外键是列上的声明，不是独立表",
                )
            else:
                layer, materialized, reason = (
                    layering.structure_to_layer.get(structure, "dwd"),
                    False,
                    f"structure_type={structure} → 非实体化关系，不落物理表",
                )
            strategy, partition_key = (
                self._load_strategy_for_object(db, rel.mapping_object_type_id)
                if materialized and rel.mapping_object_type_id
                else (LoadStrategy.FULL.value, None)
            )
            out.append(
                {
                    "target_kind": TargetKind.RELATION_TYPE.value,
                    "target_id": rel.id,
                    "target_layer": layer,
                    "target_engines": json.dumps(DEFAULT_ENGINES, ensure_ascii=False),
                    "load_strategy": strategy,
                    "partition_key": partition_key,
                    "scd_type": ScdType.NONE.value,
                    "materialized": materialized,
                    "derivation_reason": reason,
                }
            )

        for logic in (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .all()
        ):
            out.append(
                {
                    "target_kind": TargetKind.BUSINESS_LOGIC.value,
                    "target_id": logic.id,
                    "target_layer": layering.business_logic_layer,
                    "target_engines": json.dumps(DEFAULT_ENGINES, ensure_ascii=False),
                    "load_strategy": LoadStrategy.FULL.value,
                    "partition_key": None,
                    "scd_type": ScdType.NONE.value,
                    "materialized": True,
                    "derivation_reason": "业务逻辑 → ADS 指标物化",
                }
            )

        return out

    def _load_strategy_for_object(
        self, db: Session, object_type_id: str | None
    ) -> tuple[str, str | None]:
        """有时间语义字段 → 增量 + 以该字段为分区键；否则全量。"""
        if not object_type_id:
            return LoadStrategy.FULL.value, None
        prop = (
            db.query(Property)
            .filter(
                Property.object_type_id == object_type_id,
                Property.semantic_type == _TIME_SEMANTIC,
            )
            .order_by(Property.name)
            .first()
        )
        if prop is None:
            return LoadStrategy.FULL.value, None
        return LoadStrategy.INCREMENTAL.value, prop.name

    # ---------- 同步（三方合并） ----------

    def sync(self, db: Session, ontology_id: str) -> dict:
        """把推导结果写入库；已存在的契约只更新**未被人工钉住**的字段。"""
        derived = self.derive(db, ontology_id)
        existing = {
            (c.target_kind, c.target_id): c
            for c in db.query(MaterializationContract)
            .filter(MaterializationContract.ontology_id == ontology_id)
            .all()
        }
        created = updated = skipped_pinned = 0
        seen: set[tuple[str, str]] = set()

        for item in derived:
            key = (item["target_kind"], item["target_id"])
            seen.add(key)
            baseline = json.dumps(
                {f: item.get(f) for f in _MACHINE_FIELDS}, ensure_ascii=False
            )
            contract = existing.get(key)
            if contract is None:
                contract = MaterializationContract(
                    ontology_id=ontology_id,
                    target_kind=item["target_kind"],
                    target_id=item["target_id"],
                    machine_baseline=baseline,
                    **{f: item[f] for f in _MACHINE_FIELDS},
                    derivation_reason=item["derivation_reason"],
                )
                db.add(contract)
                created += 1
                continue

            pinned = set(contract.pinned_fields)
            changed = False
            for field in _MACHINE_FIELDS:
                if field in pinned:
                    skipped_pinned += 1
                    continue
                if getattr(contract, field) != item[field]:
                    setattr(contract, field, item[field])
                    changed = True
            # 外键关系永远不物化：即使人工曾把 materialized 钉成 True（旧版未拦），
            # 机器重推导时也要强制纠正回来，否则它会被选进物化批次却无逻辑表。
            if (
                item.get("materialized") is False
                and contract.target_kind == TargetKind.RELATION_TYPE.value
                and contract.materialized is True
            ):
                rel = db.get(RelationType, contract.target_id)
                if rel is not None and (rel.structure_type or "") == "foreign_key":
                    contract.materialized = False
                    _mark_overridden(contract, ["materialized"])
                    changed = True
            contract.machine_baseline = baseline
            contract.derivation_reason = item["derivation_reason"]
            contract.upstream_removed = False
            if changed:
                updated += 1

        # 本体实体已消失的契约：标记而非删除，保留人工配置以便实体回来时复用。
        for key, contract in existing.items():
            if key not in seen and not contract.upstream_removed:
                contract.upstream_removed = True
                updated += 1

        db.commit()
        return {
            "ontology_id": ontology_id,
            "created": created,
            "updated": updated,
            "skipped_pinned": skipped_pinned,
            "total": len(derived),
        }

    # ---------- 查询与编辑 ----------

    def list_contracts(
        self,
        db: Session,
        ontology_id: str,
        *,
        target_kind: str | None = None,
        materialized_only: bool = False,
    ) -> list[MaterializationContract]:
        q = db.query(MaterializationContract).filter(
            MaterializationContract.ontology_id == ontology_id
        )
        if target_kind:
            q = q.filter(MaterializationContract.target_kind == target_kind)
        if materialized_only:
            q = q.filter(MaterializationContract.materialized.is_(True))
        rows = q.order_by(
            MaterializationContract.target_kind, MaterializationContract.target_id
        ).all()
        # 防御性深度过滤：外键关系是列上的声明，永远不应作为物化源表。
        # 即使 materialized 标志因遗留数据/直接改库而为 True，也在这里挡住，
        # 不让它进入同步工具解析、批次计数、cron 分组、Chat BI 实体列表等下游。
        if materialized_only:
            rows = self._exclude_foreign_key_relations(db, rows)
        return rows

    @staticmethod
    def _exclude_foreign_key_relations(
        db: Session, contracts: list[MaterializationContract]
    ) -> list[MaterializationContract]:
        """从契约列表中剔除外键关系契约（防御性，不依赖 materialized 标志正确性）。"""
        rel_ids = [
            c.target_id
            for c in contracts
            if c.target_kind == TargetKind.RELATION_TYPE.value
        ]
        if not rel_ids:
            return contracts
        fk_ids: set[str] = set()
        for rel in db.query(RelationType).filter(RelationType.id.in_(rel_ids)).all():
            if (rel.structure_type or "") == "foreign_key":
                fk_ids.add(rel.id)
        if not fk_ids:
            return contracts
        return [c for c in contracts if c.target_id not in fk_ids]

    def list_selected(
        self,
        db: Session,
        ontology_id: str,
        selected_targets: list[str] | None = None,
    ) -> list[MaterializationContract]:
        """物化弹窗本次勾选的、且标记为物化的契约。

        ``selected_targets`` 是**本体实体名**（不是物理表名），与物化请求同口径——
        改过表名的实体不会因此被误裁。空/None = 全部可物化实体。
        """
        contracts = self.list_contracts(db, ontology_id, materialized_only=True)
        if not selected_targets:
            return contracts
        names = self.resolve_target_names(db, contracts)
        wanted = set(selected_targets)
        return [
            c
            for c in contracts
            if (names.get(c.target_id) or (None,))[0] in wanted
        ]

    def get_for_target(
        self, db: Session, ontology_id: str, target_kind: str, target_id: str
    ) -> MaterializationContract | None:
        return (
            db.query(MaterializationContract)
            .filter(
                MaterializationContract.ontology_id == ontology_id,
                MaterializationContract.target_kind == target_kind,
                MaterializationContract.target_id == target_id,
            )
            .first()
        )

    def update(
        self, db: Session, contract_id: str, patch: dict
    ) -> MaterializationContract | None:
        """人工编辑：提交的字段被钉住，此后机器推导不再覆盖。"""
        contract = (
            db.query(MaterializationContract)
            .filter(MaterializationContract.id == contract_id)
            .first()
        )
        if contract is None:
            return None

        # 外键关系是列上的声明，不是独立表，永远不应被物化为源表。
        # 即使人工显式 patch materialized=True 也要拦住——否则它会被 list_selected
        # 选进物化批次却无对应逻辑表，产生误导性的「已物化」状态。
        if (
            patch.get("materialized") is True
            and contract.target_kind == TargetKind.RELATION_TYPE.value
        ):
            rel = db.get(RelationType, contract.target_id)
            if rel is not None and (rel.structure_type or "") == "foreign_key":
                raise ValueError(
                    "外键关系是列上的声明，不是独立表，不能标记为物化"
                )

        touched: list[str] = []
        for patch_key, value in patch.items():
            column = _PATCH_TO_COLUMN.get(patch_key)
            if column is None or value is None:
                continue
            if patch_key == "engines":
                value = json.dumps(list(value), ensure_ascii=False)
            if getattr(contract, column) != value:
                setattr(contract, column, value)
            touched.append(column)

        if touched:
            _mark_overridden(contract, touched)
        db.commit()
        db.refresh(contract)
        return contract

    def resolve_target_names(
        self, db: Session, contracts: list[MaterializationContract]
    ) -> dict[str, tuple[str | None, str | None]]:
        """批量取本体实体的 name / display_name，供列表展示（避免 N+1）。"""
        by_kind: dict[str, set[str]] = {}
        for c in contracts:
            by_kind.setdefault(c.target_kind, set()).add(c.target_id)

        names: dict[str, tuple[str | None, str | None]] = {}
        model_by_kind = {
            TargetKind.OBJECT_TYPE.value: ObjectType,
            TargetKind.RELATION_TYPE.value: RelationType,
            TargetKind.BUSINESS_LOGIC.value: BusinessLogic,
        }
        for kind, ids in by_kind.items():
            model = model_by_kind.get(kind)
            if model is None or not ids:
                continue
            for row in db.query(model).filter(model.id.in_(ids)).all():
                names[row.id] = (row.name, row.display_name)
        return names
