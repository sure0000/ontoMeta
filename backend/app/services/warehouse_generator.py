"""本体 → 物理正向生成器（M3）。

本体是一级源数据、物理表是二级投影。本模块把「本体 + 物化契约」编译成引擎无关的
LogicalSchema，再交给 Dialect Adapter 渲染 DDL；同时产出 ETL SQL、调度 DAG 与
物理映射文件。

设计约束：
- **本模块不得包含任何引擎特定逻辑**，全部下沉到 ``app/warehouse/adapters``。
- **不可生成的东西必须显式列进 ``unsupported``，绝不静默跳过。**
- **幂等**：同一本体重复生成，产出逐字节一致。

遍历模式与外键推断沿用 ``services/data_app.py`` 的 ``_build_cube_objects`` /
``_relation_to_join``——那是本仓库既有的「本体 → 可部署物理产物」先例。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, joinedload

from app.connectors.datahub import _extract_dataset_name
from app.models import (
    BusinessLogic,
    MaterializationContract,
    ObjectType,
    RelationType,
)
from app.models.warehouse import TargetKind
from app.warehouse import (
    CapabilityError,
    DEFAULT_ENGINE,
    LogicalColumn,
    LogicalConstraint,
    LogicalSchema,
    LogicalTable,
    get_adapter,
)

# 缺乏证据时的外键回退约定，与 connectors/cube.py 保持一致。
_DEFAULT_REF_COLUMN = "id"


@dataclass
class LogicalPlan:
    """编译结果。

    注：规格里 ``build_logical_schema`` 的签名是 ``-> LogicalSchema``，此处改为返回
    本对象——因为同一份规格同时要求「不可生成项必须显式返回」，二者无法只用
    LogicalSchema 承载。
    """

    schema: LogicalSchema
    unsupported: list[dict] = field(default_factory=list)

    def note(self, target: str, reason: str) -> None:
        self.unsupported.append({"target": target, "reason": reason})


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _comment_of(display_name: str | None, description: str | None) -> str | None:
    """业务语义由本体反补到物理层——源库通常零注释，这是本架构的关键价值点。"""
    parts = [p.strip() for p in (display_name, description) if p and p.strip()]
    if not parts:
        return None
    # 去重：display_name 与 description 相同时不重复拼接
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " · ".join(parts)


def _nodes_in_cycles(deps: dict[str, set[str]]) -> set[str]:
    """返回真正处于环中的节点（Tarjan 强连通分量，size≥2 或自环）。

    迭代实现而非递归：真实 ERP 本体有 734 个对象，且血缘存在 307 节点的强连通
    分量（见项目记忆 erp-lineage-is-cyclic-tangle），递归会撞 Python 栈深度上限。
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    in_cycle: set[str] = set()

    for root in sorted(deps):
        if root in index:
            continue
        # (node, 待处理邻居迭代器)
        work: list[tuple[str, list[str]]] = [(root, sorted(deps.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in deps:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(deps.get(nxt, ()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1 or node in deps.get(node, ()):
                        in_cycle.update(component)
    return in_cycle


class WarehouseGenerator:
    # ---------- 编译 ----------

    def build_logical_schema(
        self, db: Session, ontology_id: str, *, database_prefix: str | None = None
    ) -> LogicalPlan:
        """本体 + 物化契约 → 引擎无关的 LogicalSchema。"""
        objects = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .order_by(ObjectType.name)
            .all()
        )
        obj_by_id = {o.id: o for o in objects}
        relations = (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .order_by(RelationType.name)
            .all()
        )
        contracts = {
            (c.target_kind, c.target_id): c
            for c in db.query(MaterializationContract)
            .filter(MaterializationContract.ontology_id == ontology_id)
            .all()
        }

        plan = LogicalPlan(schema=LogicalSchema(ontology_id=ontology_id))
        tables: list[LogicalTable] = []

        # ---- 对象 → 表 ----
        fks_by_object = self._foreign_keys_by_object(
            relations, obj_by_id, contracts, database_prefix, plan
        )
        for obj in objects:
            contract = contracts.get((TargetKind.OBJECT_TYPE.value, obj.id))
            if contract is None:
                plan.note(obj.name, "缺物化契约，请先执行「按本体重新推导」")
                continue
            if not contract.materialized:
                continue
            tables.append(
                self._object_to_table(
                    obj, contract, fks_by_object.get(obj.id, []), database_prefix, plan
                )
            )

        # ---- 关系（事实表/桥表）→ 表 ----
        tables.extend(
            self._relation_tables(
                relations, obj_by_id, contracts, database_prefix, plan
            )
        )

        # ---- 业务逻辑 → ADS ----
        tables.extend(
            self._logic_tables(db, ontology_id, contracts, database_prefix, plan)
        )

        plan.schema = LogicalSchema(
            ontology_id=ontology_id,
            tables=tuple(sorted(tables, key=lambda t: (t.layer, t.name))),
        )
        return plan

    def _database_of(self, layer: str, prefix: str | None) -> str:
        return f"{layer}_{prefix}" if prefix else layer

    def _object_to_table(
        self,
        obj: ObjectType,
        contract: MaterializationContract,
        foreign_keys: list[LogicalConstraint],
        prefix: str | None,
        plan: LogicalPlan,
    ) -> LogicalTable:
        columns = tuple(
            LogicalColumn(
                name=p.name,
                data_type=p.data_type,
                semantic_type=p.semantic_type,
                comment=_comment_of(p.display_name, p.description),
                nullable=not p.required,
            )
            for p in sorted(obj.properties, key=lambda p: p.name)
        )
        constraints: list[LogicalConstraint] = []
        pk = self._primary_key_of(obj)
        if pk:
            constraints.append(LogicalConstraint("primary_key", (pk,)))
        else:
            # 不静默：本体没有可识别的身份属性，表照建但主键缺失需人工知晓。
            plan.note(f"{obj.name}.primary_key", "本体未声明可识别的身份属性，主键未生成")
        constraints.extend(foreign_keys)

        return LogicalTable(
            name=obj.name,
            database=self._database_of(contract.target_layer, prefix),
            layer=contract.target_layer,
            comment=_comment_of(obj.display_name, obj.description),
            columns=columns,
            constraints=tuple(constraints),
            partition_key=contract.partition_key,
            scd_type=contract.scd_type,
        )

    @staticmethod
    def _primary_key_of(obj: ObjectType) -> str | None:
        """身份属性：优先 ``<表名>_id``，其次首个 identifier 语义字段。"""
        by_name = {p.name: p for p in obj.properties}
        preferred = f"{obj.name}_id"
        if preferred in by_name:
            return preferred
        identifiers = sorted(
            (p.name for p in obj.properties if (p.semantic_type or "") == "identifier")
        )
        return identifiers[0] if identifiers else None

    def _foreign_keys_by_object(
        self,
        relations: list[RelationType],
        obj_by_id: dict[str, ObjectType],
        contracts: dict,
        prefix: str | None,
        plan: LogicalPlan,
    ) -> dict[str, list[LogicalConstraint]]:
        """外键型关系 → 源对象上的外键声明。

        字段推断沿用 ``connectors/cube.py``：从 ``source_evidence`` 取
        ``foreign_key``/``source_field``/``fk_column``，目标列取
        ``target_field``/``pk_column``，缺失时回退 ``<target>_id`` = ``id``。
        """
        out: dict[str, list[LogicalConstraint]] = {}
        for rel in relations:
            if (rel.structure_type or "") != "foreign_key":
                continue
            src = obj_by_id.get(rel.source_object_type_id)
            tgt = obj_by_id.get(rel.target_object_type_id)
            if not src or not tgt:
                continue
            card = (rel.cardinality or "").lower().replace(" ", "")
            if card in {"n:n", "m:n", "many_to_many", "many-to-many"}:
                plan.note(
                    rel.name,
                    "N:N 关系需桥接表，不自动生成（与 connectors/cube.py 约定一致）",
                )
                continue
            tgt_contract = contracts.get((TargetKind.OBJECT_TYPE.value, tgt.id))
            if tgt_contract is None or not tgt_contract.materialized:
                plan.note(rel.name, f"外键目标 {tgt.name} 未物化，外键声明未生成")
                continue

            ev = _loads(rel.source_evidence, {}) or {}
            fk = (
                ev.get("foreign_key")
                or ev.get("source_field")
                or ev.get("fk_column")
                or f"{tgt.name}_id"
            )
            ref_col = (
                ev.get("target_field")
                or ev.get("pk_column")
                or self._primary_key_of(tgt)
                or _DEFAULT_REF_COLUMN
            )
            ref_db = self._database_of(tgt_contract.target_layer, prefix)
            out.setdefault(src.id, []).append(
                LogicalConstraint(
                    kind="foreign_key",
                    columns=(str(fk),),
                    ref_table=f"{ref_db}.{tgt.name}",
                    ref_columns=(str(ref_col),),
                )
            )
        for key in out:
            out[key].sort(key=lambda c: c.columns)
        return out

    def _relation_tables(
        self,
        relations: list[RelationType],
        obj_by_id: dict[str, ObjectType],
        contracts: dict,
        prefix: str | None,
        plan: LogicalPlan,
    ) -> list[LogicalTable]:
        """事实表/桥表型关系 → DWD 明细表，列取自其实现表。"""
        tables: list[LogicalTable] = []
        claimed: dict[str, str] = {}  # mapping_object_type_id → 已占用的关系名

        for rel in relations:
            if (rel.structure_type or "") not in ("fact_table", "bridge_table"):
                continue
            contract = contracts.get((TargetKind.RELATION_TYPE.value, rel.id))
            if contract is None:
                plan.note(rel.name, "缺物化契约，请先执行「按本体重新推导」")
                continue
            if not contract.materialized:
                continue
            if not rel.mapping_object_type_id:
                plan.note(rel.name, "事实/桥表关系缺实现表（mapping_object_type_id），无法生成列")
                continue
            impl = obj_by_id.get(rel.mapping_object_type_id)
            if impl is None:
                plan.note(rel.name, "实现表不在本体内，无法生成列")
                continue
            # 粒度冲突：多条关系共用同一实现表，物理上无法各建一张同名表。
            if rel.mapping_object_type_id in claimed:
                plan.note(
                    rel.name,
                    f"粒度冲突：实现表 {impl.name} 已被关系 {claimed[rel.mapping_object_type_id]} 占用",
                )
                continue
            claimed[rel.mapping_object_type_id] = rel.name

            src = obj_by_id.get(rel.source_object_type_id)
            tgt = obj_by_id.get(rel.target_object_type_id)
            comment_bits = [_comment_of(rel.display_name, rel.description) or rel.name]
            if src and tgt:
                comment_bits.append(f"{src.display_name} → {tgt.display_name}")
            tables.append(
                LogicalTable(
                    name=impl.name,
                    database=self._database_of(contract.target_layer, prefix),
                    layer=contract.target_layer,
                    comment=" · ".join(comment_bits),
                    columns=tuple(
                        LogicalColumn(
                            name=p.name,
                            data_type=p.data_type,
                            semantic_type=p.semantic_type,
                            comment=_comment_of(p.display_name, p.description),
                            nullable=not p.required,
                        )
                        for p in sorted(impl.properties, key=lambda p: p.name)
                    ),
                    partition_key=contract.partition_key,
                    scd_type=contract.scd_type,
                )
            )
        return tables

    def _logic_tables(
        self,
        db: Session,
        ontology_id: str,
        contracts: dict,
        prefix: str | None,
        plan: LogicalPlan,
    ) -> list[LogicalTable]:
        """业务逻辑 → ADS 指标表。

        口径表达式（``expression_summary``）不在此翻译成 SQL——那属于 M6 的
        MetricSpec Executor。这里只把指标物化成一张可承载结果的表。
        """
        tables: list[LogicalTable] = []
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .order_by(BusinessLogic.name)
            .all()
        )
        for logic in logics:
            contract = contracts.get((TargetKind.BUSINESS_LOGIC.value, logic.id))
            if contract is None:
                plan.note(logic.name, "缺物化契约，请先执行「按本体重新推导」")
                continue
            if not contract.materialized:
                continue
            if not (logic.expression_summary or "").strip():
                plan.note(logic.name, "业务逻辑无口径表达式，指标结果表未生成")
                continue
            tables.append(
                LogicalTable(
                    name=logic.name,
                    database=self._database_of(contract.target_layer, prefix),
                    layer=contract.target_layer,
                    comment=_comment_of(logic.display_name, logic.expression_summary),
                    columns=(
                        LogicalColumn("stat_date", "date", "datetime", "统计日期"),
                        LogicalColumn(
                            "metric_value", "decimal", "amount", logic.display_name
                        ),
                    ),
                    partition_key="stat_date",
                    scd_type=contract.scd_type,
                )
            )
        return tables

    # ---------- 产物 ----------

    def generate_ddl(
        self,
        db: Session,
        ontology_id: str,
        engine: str,
        *,
        database_prefix: str | None = None,
    ) -> dict:
        plan = self.build_logical_schema(
            db, ontology_id, database_prefix=database_prefix
        )
        adapter = get_adapter(engine)
        statements: dict[str, str] = {}
        warnings: list[dict] = []
        for table in plan.schema.tables:
            try:
                gaps = adapter.guard(table)
            except CapabilityError as exc:
                # 引擎表达不了 → 显式列进 unsupported，绝不静默降级。
                plan.note(
                    table.qualified_name,
                    "；".join(f"{g.feature}: {g.detail}" for g in exc.gaps),
                )
                continue
            warnings.extend(
                {"target": table.qualified_name, "feature": g.feature, "detail": g.detail}
                for g in gaps
            )
            statements[table.qualified_name] = adapter.render_create_table(table)
        return {
            "engine": engine,
            "statements": statements,
            "warnings": warnings,
            "unsupported": plan.unsupported,
        }

    def generate_etl_sql(
        self,
        db: Session,
        ontology_id: str,
        engine: str,
        *,
        database_prefix: str | None = None,
    ) -> dict:
        """ODS → 目标层的字段映射 SQL。

        源表由 ``ObjectType.source_ref``（DataHub URN）定位；列映射用
        ``Property.source_field_ref``，缺失时回退同名字段（真实源常无此字段）。
        """
        plan = self.build_logical_schema(
            db, ontology_id, database_prefix=database_prefix
        )
        adapter = get_adapter(engine)
        source_refs = self._source_refs(db, ontology_id)
        field_refs = self._field_refs(db, ontology_id)

        statements: dict[str, str] = {}
        for table in plan.schema.tables:
            if table.layer == "ads":
                continue  # ADS 由 MetricSpec 生成（M6），非字段搬运
            source = source_refs.get(table.name)
            if not source:
                plan.note(table.qualified_name, "对象无 source_ref，无法定位源表")
                continue
            mapping = field_refs.get(table.name, {})
            select_lines = [
                f"  {adapter.quote_identifier(mapping.get(c.name) or c.name)} AS {adapter.quote_identifier(c.name)}"
                for c in table.columns
            ]
            sql = (
                f"INSERT OVERWRITE TABLE {table.qualified_name}\n"
                f"SELECT\n" + ",\n".join(select_lines) + f"\nFROM {source};"
            )
            statements[table.qualified_name] = adapter.translate_sql(sql)
        return {
            "engine": engine,
            "statements": statements,
            "unsupported": plan.unsupported,
        }

    @staticmethod
    def _hive_source(table: LogicalTable) -> str:
        """权威副本的跨引擎引用。``hive`` 为外部 Catalog 占位名，具体名由部署配置决定。"""
        return f"hive.{table.qualified_name}"

    def generate_derivation(
        self,
        db: Session,
        ontology_id: str,
        engine: str,
        *,
        database_prefix: str | None = None,
    ) -> dict:
        """从 Hive 权威副本派生到目标引擎的作业（单一写入路径）。

        Hive 是权威物理副本，其余引擎**从 Hive 派生**而非各自从 ODS 同步——否则多副本
        双写会不一致。本方法为每张表产出：目标引擎建表 DDL + 声明「源为 Hive 权威表」的
        装载作业。具体装载机制（外部 Catalog / Broker Load / INSERT…SELECT）随目标引擎与
        部署而异，交由调度器落地，**不在此臆造**。
        """
        if (engine or "").lower() == DEFAULT_ENGINE:
            return {
                "engine": engine,
                "authoritative": True,
                "derivations": {},
                "note": f"{DEFAULT_ENGINE} 是权威物理副本，无需派生",
                "unsupported": [],
            }

        plan = self.build_logical_schema(
            db, ontology_id, database_prefix=database_prefix
        )
        adapter = get_adapter(engine)
        derivations: dict[str, dict] = {}
        for table in plan.schema.tables:
            # ADS 指标表由 MetricSpec Executor 按引擎生成，非从 Hive 搬运。
            if table.layer == "ads":
                continue
            try:
                adapter.guard(table)
            except CapabilityError as exc:
                plan.note(
                    table.qualified_name,
                    "；".join(f"{g.feature}: {g.detail}" for g in exc.gaps),
                )
                continue
            source = self._hive_source(table)
            load_sql = adapter.translate_sql(
                f"INSERT INTO {table.qualified_name}\nSELECT *\nFROM {source};"
            )
            derivations[table.qualified_name] = {
                "source_table": source,
                "target_ddl": adapter.render_create_table(table),
                "load_sql": load_sql,
                "load_note": (
                    "源为 Hive 权威表；跨引擎读取需在目标引擎配置指向 Hive 的外部 Catalog，"
                    "具体装载方式由调度器按引擎能力落地"
                ),
            }
        return {
            "engine": engine,
            "authoritative": False,
            "derivations": derivations,
            "note": f"{engine} 从 {DEFAULT_ENGINE} 权威副本派生（单一写入路径）",
            "unsupported": plan.unsupported,
        }

    def _source_refs(self, db: Session, ontology_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for obj in (
            db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        ):
            if obj.source_ref:
                out[obj.name] = _extract_dataset_name(obj.source_ref)
        return out

    def _field_refs(self, db: Session, ontology_id: str) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for obj in (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        ):
            mapping = {
                p.name: p.source_field_ref for p in obj.properties if p.source_field_ref
            }
            if mapping:
                out[obj.name] = mapping
        return out

    def generate_dag(
        self, db: Session, ontology_id: str, *, database_prefix: str | None = None
    ) -> dict:
        """调度依赖 DAG。

        依赖来自两处：分层顺序（dim → dwd → dws → ads）与外键指向。
        **必须处理环**——真实 ERP 血缘存在大规模强连通分量，拓扑排序不能假设无环。
        """
        plan = self.build_logical_schema(
            db, ontology_id, database_prefix=database_prefix
        )
        by_qualified = {t.qualified_name: t for t in plan.schema.tables}
        layer_rank = {"dim": 0, "dwd": 1, "dws": 2, "ads": 3}

        deps: dict[str, set[str]] = {q: set() for q in by_qualified}
        for table in plan.schema.tables:
            for fk in table.foreign_keys:
                if fk.ref_table in by_qualified and fk.ref_table != table.qualified_name:
                    deps[table.qualified_name].add(fk.ref_table)
            for other in plan.schema.tables:
                if layer_rank.get(other.layer, 0) < layer_rank.get(table.layer, 0):
                    deps[table.qualified_name].add(other.qualified_name)

        declared = {q: set(d) for q, d in deps.items()}

        # Kahn 拓扑排序
        remaining = {q: set(d) for q, d in deps.items()}
        indegree = {q: len(d) for q, d in remaining.items()}
        ready = sorted(q for q, n in indegree.items() if n == 0)
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for other, d in remaining.items():
                if node in d:
                    d.discard(node)
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        ready.append(other)
            ready.sort()

        # 未能定序的节点里，必须区分「真在环里」与「只是被环挡住的下游」——
        # 后者报成循环依赖会让人去找根本不存在的环。
        unordered = set(by_qualified) - set(order)
        cyclic = sorted(_nodes_in_cycles(declared) & unordered)
        blocked = sorted(unordered - set(cyclic))

        for node in cyclic:
            plan.note(node, "处于循环依赖中，无法定序（需人工拆环或调整外键方向）")
        for node in blocked:
            plan.note(node, "依赖链上游存在环，被阻塞而无法定序")

        return {
            "nodes": [
                {
                    "id": q,
                    "layer": by_qualified[q].layer,
                    "depends_on": sorted(declared[q]),
                }
                for q in sorted(by_qualified)
            ],
            "order": order,
            "cyclic": cyclic,
            "blocked": blocked,
            "unsupported": plan.unsupported,
        }

    def generate_mapping(
        self, db: Session, ontology_id: str, *, database_prefix: str | None = None
    ) -> dict:
        """物理映射，可直接喂 ``DataSource.mapping_json``。

        列映射通常为空——物理列名就是本体属性名（名称对齐红利），只在确有差异时才出现。
        """
        plan = self.build_logical_schema(
            db, ontology_id, database_prefix=database_prefix
        )
        return {
            "tables": {t.name: t.qualified_name for t in plan.schema.tables},
            "columns": {},
            "unsupported": plan.unsupported,
        }

    def generate_bundle(
        self,
        db: Session,
        ontology_id: str,
        engines: list[str],
        *,
        database_prefix: str | None = None,
    ) -> dict:
        """多引擎产物包，遵循单一写入路径。

        Hive（权威）产 ODS→Hive 的 ETL；其余引擎产**从 Hive 派生**的作业，而非各自
        从 ODS 双写——这是避免多副本不一致的关键。
        """
        def _engine_block(engine: str) -> dict:
            block = {
                "ddl": self.generate_ddl(
                    db, ontology_id, engine, database_prefix=database_prefix
                )
            }
            if (engine or "").lower() == DEFAULT_ENGINE:
                block["etl"] = self.generate_etl_sql(
                    db, ontology_id, engine, database_prefix=database_prefix
                )
            else:
                block["derivation"] = self.generate_derivation(
                    db, ontology_id, engine, database_prefix=database_prefix
                )
            return block

        return {
            "ontology_id": ontology_id,
            "write_path": {
                "authoritative": DEFAULT_ENGINE,
                "note": f"{DEFAULT_ENGINE} 权威写入，其余引擎从其派生（单一写入路径）",
            },
            "engines": {engine: _engine_block(engine) for engine in engines},
            "dag": self.generate_dag(
                db, ontology_id, database_prefix=database_prefix
            ),
            "mapping": self.generate_mapping(
                db, ontology_id, database_prefix=database_prefix
            ),
        }
