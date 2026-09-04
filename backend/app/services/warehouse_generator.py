"""本体 → 物理正向生成器（M3）。

本体是一级源数据、物理表是二级投影。本模块把「本体 + 物化契约」编译成引擎无关的
LogicalSchema，再交给 Dialect Adapter 渲染 DDL；同时产出 ETL SQL、调度 DAG 与
物理映射文件。

设计约束：
- **本模块不得包含任何引擎特定逻辑**，全部下沉到 ``app/warehouse/adapters``。
- **不可生成的东西必须显式列进 ``unsupported``，绝不静默跳过。**
- **幂等**：同一本体重复生成，产出逐字节一致。

遍历模式与外键推断沿用 Data App 的对象建模约定，
``_relation_to_join``——那是本仓库既有的「本体 → 可部署物理产物」先例。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session, joinedload

from app.connectors.datahub import _field_path
from app.governance import active_standard
from app.governance.lint import lint_logical_table
from app.models import (
    BusinessLogic,
    MaterializationContract,
    ObjectType,
    RelationType,
)
from app.models.warehouse import TargetKind
from app.services.metric_compiler import effective_logic_type, result_column_specs
from app.services.ontology_projection import (
    foreign_key_names,
    primary_key_is_confident,
    primary_key_name,
)
from app.services.source_ref import (
    NO_PHYSICAL_SOURCE_NOTE,
    source_platform_of,
    source_table_of,
)
from app.warehouse import (
    CapabilityError,
    DEFAULT_ENGINE,
    LogicalColumn,
    LogicalConstraint,
    LogicalSchema,
    LogicalTable,
    get_adapter,
)

# 生成期只 surface 的规约码：命名类（违规罕见、信号高）。comment/pk 等 advisory 不逐表刷。
_GENERATOR_SURFACED_CODES = frozenset({"naming_snake_case", "naming_reserved_word"})

# 缺乏证据时的外键回退约定。
_DEFAULT_REF_COLUMN = "id"


def _physical_field(source_field_ref: str | None) -> str | None:
    """``Property.source_field_ref`` → 物理列名。

    回采写入的是 DataHub 的 schemaField 标识，形如
    ``urn:li:dataset:(urn:li:dataPlatform:mariadb,db.tab,PROD)#doctype``——
    直接当列名用会生成 ``SELECT `urn:li:dataset:(...)#doctype` AS ...`` 这种必然报错的 SQL。
    取 ``#`` 之后的字段路径，再按 ``connectors/datahub._field_path`` 的既有约定取末段。
    """
    ref = (source_field_ref or "").strip()
    if not ref:
        return None
    if "#" in ref:
        ref = ref.rpartition("#")[2]
    return _field_path(ref) or None


_NUMERIC_TYPES = frozenset(
    {
        "tinyint",
        "smallint",
        "int",
        "integer",
        "bigint",
        "decimal",
        "numeric",
        "float",
        "double",
    }
)


def _safe_projection_semantic_type(
    data_type: str | None, semantic_type: str | None
) -> str | None:
    """Prevent stale semantic guesses from changing an incompatible physical type."""
    if (semantic_type or "").lower() != "datetime":
        return semantic_type
    base = re.split(r"[(\s]", (data_type or "").strip().lower(), maxsplit=1)[0]
    return "attribute" if base in _NUMERIC_TYPES else semantic_type


@dataclass(frozen=True)
class TargetNaming:
    """目标物理命名策略：库名与表名怎么定。

    默认沿用「层[_前缀].实体名」的机器约定；物化弹窗里人工选的目标库、改的表名以
    ``databases`` / ``tables`` 覆盖之——覆盖只作用于本次生成（不写回契约），故 DDL、
    ETL、外键引用都由同一个 plan 派生，三者名字必然一致。
    """

    prefix: str | None = None
    # 层（dim/dwd/ads）→ 目标库名。命中则完全取代「层[_前缀]」。
    databases: dict[str, str] = field(default_factory=dict)
    # 物化契约 id → 物理表名。命中则取代实体技术名。
    tables: dict[str, str] = field(default_factory=dict)

    def database_of(self, layer: str) -> str:
        override = (self.databases.get(layer) or "").strip()
        if override:
            return override
        return f"{layer}_{self.prefix}" if self.prefix else layer

    def table_of(self, contract: MaterializationContract, default: str) -> str:
        return (self.tables.get(contract.id) or "").strip() or default


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
        self,
        db: Session,
        ontology_id: str,
        *,
        database_prefix: str | None = None,
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
    ) -> LogicalPlan:
        """本体 + 物化契约 → 引擎无关的 LogicalSchema。

        ``database_overrides``（层 → 库名）与 ``table_overrides``（契约 id → 表名）
        是物化弹窗里人工指定的落库位置，见 {@link TargetNaming}。
        """
        naming = TargetNaming(
            prefix=database_prefix,
            databases=dict(database_overrides or {}),
            tables=dict(table_overrides or {}),
        )
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
            relations, obj_by_id, contracts, naming, plan
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
                    obj, contract, fks_by_object.get(obj.id, []), naming, plan
                )
            )

        # ---- 关系（事实表/桥表）→ 表 ----
        tables.extend(
            self._relation_tables(relations, obj_by_id, contracts, naming, plan)
        )

        # ---- 业务逻辑 → ADS ----
        tables.extend(self._logic_tables(db, ontology_id, contracts, naming, plan))

        plan.schema = LogicalSchema(
            ontology_id=ontology_id,
            tables=tuple(sorted(tables, key=lambda t: (t.layer, t.name))),
        )
        # 生成期治理体检：在**真实物理名**上查命名规约，违规记进 plan.note（advisory，
        # 不阻断生成——延续 G1「呈现而非拦截」的姿态）。只 surface 命名类；comment/pk advisory
        # 在零注释源（见真实 DataHub）上会刷屏，留给 G3 存量 re-lint 聚合呈现，不在此逐表刷。
        standard = active_standard(db)
        for table in plan.schema.tables:
            for v in lint_logical_table(table, standard):
                if v.code in _GENERATOR_SURFACED_CODES:
                    plan.note(table.qualified_name, f"治理·{v.code}：{v.fix}")
        return plan

    def _object_to_table(
        self,
        obj: ObjectType,
        contract: MaterializationContract,
        foreign_keys: list[LogicalConstraint],
        naming: TargetNaming,
        plan: LogicalPlan,
    ) -> LogicalTable:
        columns = tuple(
            LogicalColumn(
                name=p.name,
                data_type=p.data_type,
                semantic_type=_safe_projection_semantic_type(
                    p.data_type, p.semantic_type
                ),
                comment=_comment_of(p.display_name, p.description),
                nullable=not p.required,
            )
            for p in sorted(obj.properties, key=lambda p: p.name)
        )
        constraints: list[LogicalConstraint] = []
        pk = self._primary_key_of(obj)
        if pk and self._primary_key_is_confident(obj):
            constraints.append(LogicalConstraint("primary_key", (pk,)))
        elif pk:
            # 猜出来的身份属性**不发强制约束**：本体没有唯一性证据（真实源零 PK、
            # 也没开 profiling），多个 identifier 里按字典序挑中的那个，很可能既不唯一
            # 也大量为空。postgres 会把它建成真 PRIMARY KEY，于是这张表永远装不进数据
            # ——把一条没把握的推断变成了硬失败。语义导航/去重仍照用这个身份属性。
            plan.note(
                f"{obj.name}.primary_key",
                f"身份属性 {pk} 未命中 {obj.name}_id / id 命名约定、且无唯一性证据"
                "（真实源零 PK 声明、未开 profiling），未生成强制主键约束"
                "；如确为主键，请在对象上确认后重新生成",
            )
        else:
            # 不静默：本体没有可识别的身份属性，表照建但主键缺失需人工知晓。
            plan.note(f"{obj.name}.primary_key", "本体未声明可识别的身份属性，主键未生成")
        constraints.extend(foreign_keys)

        return LogicalTable(
            name=naming.table_of(contract, obj.name),
            entity_name=obj.name,
            database=naming.database_of(contract.target_layer),
            layer=contract.target_layer,
            comment=_comment_of(obj.display_name, obj.description),
            columns=columns,
            constraints=tuple(constraints),
            partition_key=contract.partition_key,
            scd_type=contract.scd_type,
            load_strategy=contract.load_strategy,
        )

    @staticmethod
    def _primary_key_of(obj: ObjectType) -> str | None:
        """身份属性：优先 ``<表名>_id``，其次首个 identifier 语义字段。

        命名约定本身由 ``ontology_projection.primary_key_name`` 固化——语义导航器
        （P1.2）推 JOIN 键用的是同一份规则，两处必须一致，否则「导航器给的 ON」
        和「物化建的外键」会指向不同的列。语义类型判定仍留在本侧（物化按原始字符串
        精确匹配 identifier，不走归一别名），以保持既有产物不变。
        """
        return primary_key_name(*WarehouseGenerator._pk_inputs(obj))

    @staticmethod
    def _primary_key_is_confident(obj: ObjectType) -> bool:
        """身份属性是约定命中还是矮子里拔将军——决定要不要发强制约束。"""
        return primary_key_is_confident(*WarehouseGenerator._pk_inputs(obj))

    @staticmethod
    def _pk_inputs(obj: ObjectType):
        return (
            obj.name,
            [p.name for p in obj.properties],
            [p.name for p in obj.properties if (p.semantic_type or "") == "identifier"],
        )

    def _foreign_keys_by_object(
        self,
        relations: list[RelationType],
        obj_by_id: dict[str, ObjectType],
        contracts: dict,
        naming: TargetNaming,
        plan: LogicalPlan,
    ) -> dict[str, list[LogicalConstraint]]:
        """外键型关系 → 源对象上的外键声明。

        字段推断从 ``source_evidence`` 取
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
                    "N:N 关系需桥接表，不自动生成",
                )
                continue
            tgt_contract = contracts.get((TargetKind.OBJECT_TYPE.value, tgt.id))
            if tgt_contract is None or not tgt_contract.materialized:
                plan.note(rel.name, f"外键目标 {tgt.name} 未物化，外键声明未生成")
                continue

            # 目标表没有强制主键时不发外键：postgres 要求被引用列上有唯一约束，
            # 否则 add_constraints 直接报「no unique constraint matching given keys」——
            # 把「主键没把握」换成了另一种硬失败。声明式引擎（Hive 写 TBLPROPERTIES）
            # 本可容忍，但两边产出保持一致更省事，且这条关系本来就没有可依的身份。
            if not self._primary_key_is_confident(tgt):
                plan.note(
                    rel.name,
                    f"外键目标 {tgt.name} 无强制主键（身份属性未命中命名约定、无唯一性证据），"
                    "外键声明未生成",
                )
                continue

            ev = _loads(rel.source_evidence, {}) or {}
            fk, ref_col = foreign_key_names(
                ev, target_name=tgt.name, target_pk=self._primary_key_of(tgt)
            )
            ref_db = naming.database_of(tgt_contract.target_layer)
            ref_table = naming.table_of(tgt_contract, tgt.name)
            out.setdefault(src.id, []).append(
                LogicalConstraint(
                    kind="foreign_key",
                    columns=(str(fk),),
                    ref_table=f"{ref_db}.{ref_table}",
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
        naming: TargetNaming,
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
                    name=naming.table_of(contract, impl.name),
                    entity_name=impl.name,
                    database=naming.database_of(contract.target_layer),
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
                    load_strategy=contract.load_strategy,
                )
            )
        return tables

    def _logic_tables(
        self,
        db: Session,
        ontology_id: str,
        contracts: dict,
        naming: TargetNaming,
        plan: LogicalPlan,
    ) -> list[LogicalTable]:
        """业务逻辑 → ADS 指标表。

        口径表达式不在此翻译成 SQL——那属于 M6 的 MetricSpec Executor。
        这里只把指标物化成一张可承载结果的表。

        **M6 落地时请直接调用 ``metric_compiler.compile_metric``**（P3 已交付），
        不要在这里另写一套口径→SQL 的翻译：口径一致性的全部意义就在于
        问数、数据应用、物化三处**共用同一个编译器**；各写一套，同一个「GMV」
        迟早算出三个数，而这种偏差在生产里几乎不可能被发现。
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
                    name=naming.table_of(contract, logic.name),
                    entity_name=logic.name,
                    database=naming.database_of(contract.target_layer),
                    layer=contract.target_layer,
                    comment=_comment_of(logic.display_name, logic.expression_summary),
                    # 结果表形状按口径类型分叉，且与指标任务读**同一份**权威
                    # （metric_compiler.result_column_specs）。两处各写各的后果是：
                    # 物化建出 stat_date+metric_value，指标任务却按 tag 形状
                    # INSERT tag_value+row_count——列对不上，跑到执行才炸。
                    columns=(
                        LogicalColumn("stat_date", "date", "datetime", "统计日期"),
                        *(
                            LogicalColumn(cname, dtype, stype, comment)
                            for cname, dtype, stype, comment in result_column_specs(
                                effective_logic_type(logic), logic.display_name
                            )
                        ),
                    ),
                    partition_key="stat_date",
                    scd_type=contract.scd_type,
                    load_strategy=contract.load_strategy,
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
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
        storage_nodes: int | None = None,
    ) -> dict:
        """本体 → 目标引擎的建表 DDL。

        ``storage_nodes`` 是**目标实例实测的存储节点数**（调用方在目标仓上探得，探不到
        传 None）。建表里有一类值既不由本体决定、也不由方言决定，而由「这套实例有多大」
        决定——最典型的是副本数：Doris 不写 ``replication_num`` 时取 FE 默认 3，单 BE
        的实例上每条 DDL 都会被拒。换算成什么属性是引擎知识，故只把数字递给 Adapter。
        """
        plan = self.build_logical_schema(
            db,
            ontology_id,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
        )
        adapter = get_adapter(engine).for_storage_nodes(storage_nodes)
        statements: dict[str, str] = {}
        warnings: list[dict] = []
        rendered: list[LogicalTable] = []
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
            rendered.append(table)

        # 两段式 DDL 的第二段：外键在所有表建完之后统一补。只有真正强制外键的引擎
        # （postgres）会产出内容，其余引擎 render_foreign_keys 返回空、行为逐字节不变。
        #
        # **被引用表不在本次范围内的外键要丢掉并报出来**：物化一部分是常规用法（弹窗就
        # 支持勾选），而 postgres 建约束时会校验被引用表存在——不筛掉的话，选了子表没选
        # 父表就整批红。丢是必须的，但绝不能默默丢，故进 warnings。
        in_scope = {t.qualified_name for t in rendered}
        constraints: dict[str, list[dict]] = {}
        for table in rendered:
            for fk in table.foreign_keys:
                if not fk.columns or not fk.ref_table:
                    continue
                # 外键列必须真的在表上。关系投影按「被引用实体名_id」的约定推列名，而真实
                # 源表（如 Frappe 系的 ERP）根本没有这种列——`dim.account` 的 5 条外键
                # 无一命中它自己的 29 个列。这种约束在任何引擎上都是非法 DDL，
                # 只是此前内联在 CREATE TABLE 里，被更早的报错盖住了。
                missing = [c for c in fk.columns if table.column(c) is None]
                if missing:
                    warnings.append(
                        {
                            "target": table.qualified_name,
                            "feature": "foreign_key_column_missing",
                            "detail": (
                                f"外键 → {fk.ref_table} 未创建：表上没有列 "
                                f"{', '.join(missing)}（关系投影按命名约定推出的列名，"
                                f"源表并无此列）。"
                            ),
                        }
                    )
                    continue
                if fk.ref_table not in in_scope:
                    warnings.append(
                        {
                            "target": table.qualified_name,
                            "feature": "foreign_key_out_of_scope",
                            "detail": (
                                f"外键 {', '.join(fk.columns)} → {fk.ref_table} 未创建："
                                f"被引用表未能生成。"
                            ),
                        }
                    )
                    continue
                # 逐条渲染：调用方（物化按勾选裁剪）要能按被引用表筛掉单条约束，
                # 故 sql 必须与它的 ref 成对存着，不能只给一堆分不出来源的语句。
                only_this_fk = replace(
                    table,
                    constraints=tuple(
                        c for c in table.constraints if c.kind != "foreign_key" or c is fk
                    ),
                )
                for sql in adapter.render_foreign_keys(only_this_fk):
                    constraints.setdefault(table.qualified_name, []).append(
                        {"ref": fk.ref_table, "sql": sql}
                    )
        return {
            "engine": engine,
            "statements": statements,
            "constraints": constraints,
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
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
        load_strategy: str | None = None,
        per_contract_strategy: bool = False,
    ) -> dict:
        """ODS → 目标层的字段映射 SQL。

        源表由 ``ObjectType.source_ref``（DataHub URN）定位；列映射用
        ``Property.source_field_ref``，缺失时回退同名字段（真实源常无此字段）。

        装载方式决定装载语句：``full`` → ``INSERT OVERWRITE``（权威覆盖）；
        ``incremental`` → ``INSERT INTO``（追加；有分区键则加分区谓词占位，水位由调度器注入）；
        ``cdc`` → 同库 INSERT 无法表达变更捕获，回退为覆盖并在 ``warnings`` 明示应写同步作业。

        取哪个方式，按优先级：显式传入的 ``load_strategy``（全局覆盖）→
        ``per_contract_strategy`` 时各表契约自身的策略 → ``full``。
        默认不读契约策略，是为保住正向生成端点「缺省即覆盖」的既有语义；物化弹窗
        逐实体选同步方式，故由 runner 显式开启 ``per_contract_strategy``。
        """
        plan = self.build_logical_schema(
            db,
            ontology_id,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
        )
        adapter = get_adapter(engine)
        source_refs = self._source_refs(db, ontology_id)
        field_refs = self._field_refs(db, ontology_id)

        override = (load_strategy or "").strip().lower() or None
        warnings: list[dict] = []
        statements: dict[str, str] = {}
        for table in plan.schema.tables:
            if table.layer == "ads":
                continue  # ADS 由 MetricSpec 生成（M6），非字段搬运
            strategy = override or (
                (table.load_strategy or "full").strip().lower()
                if per_contract_strategy
                else "full"
            )
            source = source_refs.get(table.source_name)
            if not source:
                plan.note(table.qualified_name, NO_PHYSICAL_SOURCE_NOTE)
                continue
            mapping = field_refs.get(table.source_name, {})
            select_lines = [
                f"  {adapter.quote_identifier(mapping.get(c.name) or c.name)} AS {adapter.quote_identifier(c.name)}"
                for c in table.columns
            ]
            # 源表名是真实源里的原名（Frappe 的 tabAccount Category 带空格），
            # 必须按引擎规则引用——直接拼进 FROM 会产出无法解析的 SQL。
            select_body = (
                "SELECT\n"
                + ",\n".join(select_lines)
                + f"\nFROM {adapter.quote_table_ref(source)}"
            )
            if strategy == "incremental":
                where = ""
                if table.partition_key:
                    pk = adapter.quote_identifier(table.partition_key)
                    where = f"\nWHERE {pk} >= :watermark"
                    plan.note(
                        table.qualified_name,
                        f"增量装载：分区键 {table.partition_key} 的水位 :watermark 需由调度器注入",
                    )
                else:
                    warnings.append(
                        {
                            "target": table.qualified_name,
                            "feature": "incremental",
                            "detail": "增量装载但契约未配分区键，退化为无谓词追加，"
                            "可能产生重复，请配置分区键或去重逻辑",
                        }
                    )
                # 装载语句的写法归 Adapter：postgres 没有 INSERT OVERWRITE，
                # INSERT INTO 后面也不能跟 TABLE（都是 Hive 家族的写法）。
                sql = adapter.render_load(
                    table.qualified_name, f"{select_body}{where}", overwrite=False
                )
            else:
                if strategy == "cdc":
                    warnings.append(
                        {
                            "target": table.qualified_name,
                            "feature": "cdc",
                            "detail": "CDC 无法用同库 INSERT 表达，本次按全量覆盖执行；"
                            "如需变更捕获请配置 Flink CDC 同步作业",
                        }
                    )
                sql = adapter.render_load(
                    table.qualified_name, select_body, overwrite=True
                )
            statements[table.qualified_name] = adapter.translate_sql(sql)
        return {
            "engine": engine,
            "statements": statements,
            "warnings": warnings,
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
        """对象名 → 源物理表名（``库.表``）。**没有物理源表的对象不进这个表。**

        用严格解析（``source_ref.source_table_of``）而非 ``_extract_dataset_name``：后者对
        非 URN 输入原样回吐，人工建模对象的 ``manual:mysql:foo`` 会被当成表名一路拼进
        ``INSERT … SELECT … FROM manual:mysql:foo``。人工建模对象本就没有源可搬，
        缺席即正确——``JobPlanner`` 见不到它就不产搬运作业，``_assign_ddl`` 仍会把它的建表
        语句归进孤儿批，该建的表照建。
        """
        out: dict[str, str] = {}
        for obj in (
            db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        ):
            table = source_table_of(obj.source_ref)
            if table:
                out[obj.name] = table
        return out

    def _field_refs(self, db: Session, ontology_id: str) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for obj in (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        ):
            mapping = {}
            for p in obj.properties:
                physical = _physical_field(p.source_field_ref)
                if physical:
                    mapping[p.name] = physical
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
            # 键是本体实体名（表名被人工改写时仍以实体名为源侧键），值是物理全名。
            "tables": {t.source_name: t.qualified_name for t in plan.schema.tables},
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
