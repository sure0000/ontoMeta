# 形式化校验详细实现设计（F1 / F3 / F4）

> 配套 `FORMAL_VALIDATION_PLAN.md`。本文给出可直接编码的模块级设计：数据结构、函数签名、
> 算法、与现有代码的精确接缝、测试用例。聚焦「不错答」的三块决定性改造：
> **F1 Schema 形式化地基 → F3 SQL 语义证明 → F4 断言级可靠性**。
>
> 已核对的现状事实（编码前提）：
> - SQL 解析现仅有 `sqlparse`（只分词，无列作用域）；**需新增 `sqlglot`** 做真 AST。
> - 指标 `expression_json` **已是结构化 AST**（`{type, refs[], body:{operation,args,filter,group_by}}`），
>   口径校验可直接吃它，无需另造。
> - 物理映射存 `DataSource.mapping_json = {"tables":{ontoName:physical}, "columns":{ontoName:physical}}`。
> - Agent 工具经 `chat_bi._dispatch_agent_tool` 分发；SQL 经 `_dispatch_run_sql` → `data_app_executor.execute_sql`。
> - 关系读模型 `RelationTypeOut` 带 `source_object_type_id/target_object_type_id/cardinality/structure_type/mapping_object_type_id`。

---

## 第一部分：F1 — Schema 形式化地基

形式化校验的一切都依赖「基数、语义类型是**枚举**而非自由文本」。先把地基做实。

### 1.1 新增 `app/ontology_types.py`（受控词表 + 语义代数）

单一模块集中定义枚举与「能力谓词」，供 F2/F3/F4 共用，杜绝各处各判。

```python
# app/ontology_types.py
from __future__ import annotations
import enum

class Cardinality(str, enum.Enum):
    ONE_TO_ONE   = "one_to_one"
    ONE_TO_MANY  = "one_to_many"
    MANY_TO_ONE  = "many_to_one"
    MANY_TO_MANY = "many_to_many"

class SemanticType(str, enum.Enum):
    IDENTIFIER  = "identifier"   # 主键/外键/业务编号
    MEASURE     = "measure"      # 金额/数量等可加度量
    TEMPORAL    = "temporal"     # 日期/时间
    CATEGORICAL = "categorical"  # 类别/状态/枚举
    TEXTUAL     = "textual"      # 自由文本/名称/描述
    TECHNICAL   = "technical"    # 技术字段（默认不入业务查询）
    UNKNOWN     = "unknown"      # 迁移期占位，进复核

# —— 语义代数：一个语义类型「能做什么」。SQL 证明器(F3)与不变式(F2)都查这里 ——
_AGGREGATABLE   = {SemanticType.MEASURE}
_GROUPABLE      = {SemanticType.IDENTIFIER, SemanticType.TEMPORAL, SemanticType.CATEGORICAL}
_JOINABLE       = {SemanticType.IDENTIFIER}
_FILTERABLE     = {SemanticType.IDENTIFIER, SemanticType.TEMPORAL,
                   SemanticType.CATEGORICAL, SemanticType.MEASURE}

def can_aggregate(st: SemanticType) -> bool: return st in _AGGREGATABLE
def can_group_by(st: SemanticType) -> bool:  return st in _GROUPABLE
def can_join_on(st: SemanticType) -> bool:   return st in _JOINABLE
def can_filter(st: SemanticType) -> bool:    return st in _FILTERABLE

# 归一化：把存量自由文本 / LLM 输出映射到枚举，认不出 → UNKNOWN（不抛错，进复核）
_CARD_ALIASES = {
    "1:1":"one_to_one","one-to-one":"one_to_one",
    "1:n":"one_to_many","1:m":"one_to_many","one-to-many":"one_to_many",
    "n:1":"many_to_one","m:1":"many_to_one","many-to-one":"many_to_one",
    "n:m":"many_to_many","n:n":"many_to_many","m:n":"many_to_many","many-to-many":"many_to_many",
}
def normalize_cardinality(v: str | None) -> Cardinality | None: ...
def normalize_semantic_type(v: str | None) -> SemanticType: ...   # 认不出返回 UNKNOWN
```

> `semantic_type` 的推断优先复用现有 `object_classifier` 的字段画像信号（它已经在算
> measure/descriptive/grain 占比），迁移脚本直接调它兜底，不重造。

### 1.2 DB 约束与列变更（alembic）

> 【实现决定】2025 落地时**暂不加硬唯一约束**（object_types/relation_types 的
> `(ontology_id, name)`）。原因：生成流水线 ``ontology_merge.merge_objects`` 在 dedup
> sweep **之前**会逐个 ``db.flush()`` 对象，此时同名对象的 DB 唯一约束会在 flush
> 时立即报错，与「先落库再消歧」的现有流程冲突；而该不变式已由
> ``validate_ontology``（发布前一致性）+ dedup sweep 在应用层保证。若未来把生成
> 改成「先消歧再 flush」，再补硬约束。**属性名** `(object_type_id, name)` 的唯一性
> 风险较小，可在后续单独评估。

若后续确需加约束，新增迁移 `xxxx_formalize_ontology_schema.py`：

```python
def upgrade():
    # 1) 唯一约束（现在只靠 Python 遍历，非原子；命名与 validate_ontology 的 code 对齐）
    op.create_unique_constraint("uq_object_ontology_name", "object_types", ["ontology_id", "name"])
    op.create_unique_constraint("uq_prop_object_name", "properties", ["object_type_id", "name"])
    op.create_unique_constraint("uq_rel_ontology_name", "relation_types", ["ontology_id", "name"])
    # 2) 语义类型/基数列本就存在(自由文本)，此处不改类型（SQLite 改列受限），
    #    改为「值层枚举化」：迁移脚本回填 + pydantic/服务层校验把关（见 1.3/1.4）。
```

> 说明：SQLite（测试/本地默认库）对 `ALTER COLUMN` 支持弱，故**不强改列类型**，而是
> 「值枚举化 + 应用层校验」。唯一约束 SQLite 支持，可加。迁移前需先跑清洗脚本消除既有
> 重复名（复用现成 `scripts/dedupe_ontology_duplicates.py` / `disambiguate_object_names.py`）。

### 1.3 存量清洗脚本 `scripts/formalize_ontology_fields.py`

```
对每个本体的关系/属性：
  cardinality  ← normalize_cardinality(旧值)      # 认不出 → 记 needs_review，值置 None
  semantic_type← normalize_semantic_type(旧值)     # 认不出 → 调 object_classifier 画像兜底
  data_type    ← normalize_data_type(旧值)         # 归一到受控物理类型
产出报告：{归一成功数, 置 unknown 数, 需复核清单}
```
幂等、可重跑；不删数据；`unknown` 项进复核队列（复用 role_reason 的 `[待复核]` 机制思路）。

### 1.4 校验接入点（迁移期 warn，清洗后切 error）

- **pydantic**：`RelationTypeCreate/Update`、`PropertyUpdate` 加 `field_validator`，
  非枚举值在 `warn` 期只告警、`error` 期拒绝。
- **服务层**：`edit.py::update_relation_type` 已校验 `structure_type`，同法加
  `cardinality`；`update_property` 加 `semantic_type`。
- 开关：`SettingsService` 加 `formal_enforcement = "off"|"warn"|"error"`，全局灰度。

---

## 第二部分：F3 — SQL 语义证明器（★ 堵住「静默错数」）

**这是投入产出比最高、最先做的一块。** 它是纯静态检查：给定 SQL + 已发布本体投影 +
物理映射，**在执行前**证明这条 SQL 语义合法，否则拒绝执行/建议。

### 2.1 依赖

`requirements.txt` 增 `sqlglot>=25.0.0`（纯 Python，带真 AST + `qualify` 列作用域解析）。
`sqlparse` 保留给现有只读校验/美化，不动。

### 2.2 本体投影（给证明器喂的只读快照）

新增 `app/services/ontology_projection.py`：把已发布本体压成一个查询友好的内存结构，
一次构建、多次校验（Agent 一轮问答里 SQL 可能多条）。

```python
# app/services/ontology_projection.py
@dataclass(frozen=True)
class PropView:
    name: str; object_name: str; semantic_type: SemanticType; data_type: str | None

@dataclass(frozen=True)
class ObjView:
    id: str; name: str; display_name: str
    props: dict[str, PropView]                 # prop_name -> PropView

@dataclass(frozen=True)
class RelView:
    id: str; name: str
    src_obj: str; tgt_obj: str                 # 对象 name
    cardinality: Cardinality | None
    structure_type: str

@dataclass(frozen=True)
class OntologyProjection:
    objects: dict[str, ObjView]                # obj_name -> ObjView
    # (obj_a, obj_b) 无序对 -> 关系（用于 join 合法性）
    relations_by_pair: dict[frozenset[str], list[RelView]]
    mapping_tables: dict[str, str]             # ontoName -> physical（来自 DataSource.mapping_json）
    mapping_columns: dict[str, str]

    def object_of_physical(self, physical: str) -> ObjView | None: ...
    def resolve_property(self, obj: ObjView, col: str) -> PropView | None: ...
    def relation_between(self, a: str, b: str) -> list[RelView]: ...

def build_projection(db, ontology_id, mapping) -> OntologyProjection:
    """只取 status=published 的对象/属性/关系；semantic_type 经 normalize_* 归一。"""
```

关键：**只投影 published**（与 Agent 工具 `published_only=True` 一致），CWA 的封闭世界就是它。

### 2.3 证明器主体 `app/services/sql_soundness.py`

```python
# app/services/sql_soundness.py
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

@dataclass
class SqlRejection:
    code: str          # unknown_table | unknown_column | undeclared_join | fanout_risk | illegal_aggregation | ...
    message: str       # 面向用户、可照做
    detail: dict

@dataclass
class SqlCertificate:
    tables: list[str]; columns: list[str]; joins: list[str]
    aggregations: list[str]; notes: list[str]

def prove_sql_sound(
    sql: str, proj: OntologyProjection, *, dialect: str = "mysql",
) -> SqlCertificate | SqlRejection:
    # 0) 解析 + 列作用域限定。qualify 会把每个裸列绑定到其来源表。
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        tree = qualify(tree, schema=_schema_from_projection(proj))  # 提供 schema 才能解析裸列
    except Exception as e:
        return SqlRejection("unparseable", f"SQL 无法解析：{e}", {"sql": sql})

    # 1) 表存在性：每个物理表必须能反解到一个已发布对象
    table_to_obj: dict[str, ObjView] = {}
    for t in tree.find_all(exp.Table):
        obj = proj.object_of_physical(t.name)
        if obj is None:
            return SqlRejection("unknown_table",
                f"表「{t.name}」不对应任何已发布业务对象，拒绝执行。", {"table": t.name})
        table_to_obj[t.alias_or_name] = obj

    # 2) 列归属性：每个 (表.列) 必须是该对象的已发布属性
    col_sem: dict[str, PropView] = {}
    for c in tree.find_all(exp.Column):
        obj = table_to_obj.get(c.table)          # qualify 后 c.table 一定有值
        if obj is None:
            return SqlRejection("unknown_table", f"列引用了未知表别名 {c.table}", {...})
        pv = proj.resolve_property(obj, c.name)
        if pv is None:
            return SqlRejection("unknown_column",
                f"字段「{c.name}」不属于对象「{obj.display_name}」，拒绝臆造字段。",
                {"object": obj.name, "column": c.name})
        col_sem[f"{c.table}.{c.name}"] = pv

    # 3) JOIN 合法性 + 4) 扇出安全
    has_agg = bool(list(tree.find_all(exp.AggFunc)))
    for j in tree.find_all(exp.Join):
        pair_objs = _objects_touched_by_join(j, table_to_obj)      # 取 ON 两侧列所属对象
        if len(pair_objs) == 2:
            a, b = pair_objs
            rels = proj.relation_between(a.name, b.name)
            if not rels:
                return SqlRejection("undeclared_join",
                    f"对象「{a.display_name}」与「{b.display_name}」之间没有已声明的业务关系，"
                    "拒绝臆造 JOIN。", {"a": a.name, "b": b.name})
            # 扇出：有聚合时，若 join 沿 1:N 的「一」端向「多」端展开、或 N:M，则度量翻倍
            if has_agg and _fans_out(rels, a, b):
                return SqlRejection("fanout_risk",
                    f"该 JOIN 会使度量沿「{a.display_name}↔{b.display_name}」基数展开导致重复计数，"
                    "拒绝执行以免给出错误数值。", {"cardinality": [r.cardinality.value for r in rels]})

    # 5) 聚合合法性：SUM/AVG 只能作用于 measure；GROUP BY 只能维度/时间/标识
    for agg in tree.find_all(exp.AggFunc):
        for c in agg.find_all(exp.Column):
            pv = col_sem.get(f"{c.table}.{c.name}")
            if agg.key in ("sum", "avg") and not can_aggregate(pv.semantic_type):
                return SqlRejection("illegal_aggregation",
                    f"对非度量字段「{c.name}」({pv.semantic_type.value}) 做 {agg.key.upper()} 无意义，拒绝。",
                    {"column": c.name, "semantic_type": pv.semantic_type.value})
    for g in _group_by_columns(tree):
        pv = col_sem.get(g)
        if pv and not can_group_by(pv.semantic_type):
            return SqlRejection("illegal_group_by",
                f"按度量字段「{pv.name}」分组通常是口径错误，拒绝。", {...})

    return SqlCertificate(tables=[...], columns=[...], joins=[...], aggregations=[...], notes=[...])
```

扇出判定（核心正确性）：

```python
def _fans_out(rels: list[RelView], a: ObjView, b: ObjView) -> bool:
    """聚合场景下，从 a 关联到 b 是否会放大 a 的行（导致度量重复计数）。
    规则（保守：拿不准即判会扇出，宁可拒答）：
      - 任一关系为 many_to_many → 一定扇出
      - a 是「一」端、b 是「多」端（a one_to_many b，或 b many_to_one a）→ 扇出
      - 只有 b 是「一」端（多对一，b 不放大 a）→ 不扇出
      - 基数缺失/unknown → 判扇出（拒答优先）
    """
```

### 2.4 接入 `chat_bi._dispatch_run_sql`（执行前拦截）

```python
def _dispatch_run_sql(self, db, *, args, proj: OntologyProjection):   # 多传入 proj
    sql = ...
    ok, reason = data_app_executor.is_read_only(sql)     # 现有只读校验保留
    if not ok: return {...被只读校验拒绝...}

    # ★ 新增：语义证明。不过则不执行、不建议，返回结构化拒绝。
    verdict = prove_sql_sound(sql, proj, dialect=_dialect_of(source))
    if isinstance(verdict, SqlRejection):
        return ({"executed": False, "sql": sql, "rejected": True,
                 "reason": verdict.message, "code": verdict.code},
                f"SQL 语义证明未通过：{verdict.code}", True)   # is_error=True → 不入接地

    source = self._resolve_domain_data_source(db)
    ...（原有执行逻辑不变）
```

`proj` 在 `_stream_agent_events` 起始处构建一次（`build_projection(db, ontology.id, mapping)`），
透传给 `_dispatch_run_sql`。构建失败（无 mapping）时，`run_sql` 直接降级为「仅建议且不校验列」
——但**建议 SQL 也要过表/对象存在性证明**，只是跳过物理映射相关项。

### 2.5 F3 测试 `tests/test_sql_soundness.py`

| 用例 | 输入 | 期望 |
| --- | --- | --- |
| 未知表 | `SELECT * FROM ghost_table` | `unknown_table` 拒绝 |
| 臆造列 | `SELECT fake_col FROM 订单` | `unknown_column` 拒绝 |
| 臆造 JOIN | `订单 JOIN 天气 ON ...`（无关系） | `undeclared_join` 拒绝 |
| N:M 扇出 | `SUM(订单.金额) ... JOIN 标签（N:M）` | `fanout_risk` 拒绝 |
| 1:N 正确 | `SUM(订单.金额) FROM 订单 JOIN 客户(多对一)` | 通过（客户不放大订单） |
| 非度量聚合 | `SUM(订单.状态)`（categorical） | `illegal_aggregation` 拒绝 |
| 合法查询 | `SELECT city, SUM(金额) FROM 订单 GROUP BY city` | 返回 `SqlCertificate` |

---

## 第三部分：F4 — 断言级可靠性（★ 根治幻觉）

把 `chat_bi.py` 的**运行级布尔 `grounded_hit`** 换成**断言级凭证账本 + 事后校验**。

### 3.1 事实账本 `app/services/agent_grounding.py`

```python
# app/services/agent_grounding.py
@dataclass(frozen=True)
class ObjectFact:  id: str; name: str; display_name: str
@dataclass(frozen=True)
class PropFact:    object_id: str; name: str; display_name: str; semantic_type: str
@dataclass(frozen=True)
class RelationFact:id: str; name: str; src: str; tgt: str; cardinality: str | None
@dataclass(frozen=True)
class MetricFact:  id: str; name: str; display_name: str; ast: dict | None  # expression_json
@dataclass
class DataCell:    column: str; value: object

class FactLedger:
    """本轮问答工具**实际返回**的全部事实。CWA：不在账本 = 不成立 = 不可引用。"""
    def __init__(self):
        self.objects: dict[str, ObjectFact] = {}
        self.props: dict[tuple[str, str], PropFact] = {}
        self.relations: dict[str, RelationFact] = {}
        self.metrics: dict[str, MetricFact] = {}
        self.cells: list[DataCell] = []
        self.name_index: dict[str, str] = {}      # 归一名 -> 实体 id（含 display_name/name）

    # —— 登记：只喂工具真实返回的字段 ——
    def add_object_detail(self, detail): ...       # get_object 返回，连带登记其 props
    def add_relation(self, rel): ...               # search_relations / get_object 的关系
    def add_metric(self, logic_detail): ...        # get_logic 返回，含 expression_json AST
    def add_cells(self, columns, rows): ...        # run_sql 实际结果

    # —— 判定（供 verifier）——
    def has_entity_named(self, token: str) -> bool: ...      # 具名实体是否在账本
    def has_numeric(self, value: str) -> bool: ...           # 数值是否对得到某个 cell
    def metric_matches(self, logic_id: str, claimed_ast) -> bool: ...  # 口径结构等价
```

登记点：`_dispatch_agent_tool` 内每个 `get_object/get_logic/search_*/run_sql` 分支，返回前
把结构化结果登记进 `ledger`（`ledger` 随本轮 `_stream_agent_events` 生命周期存在）。

### 3.2 答案校验器 `app/services/answer_verifier.py`

```python
# app/services/answer_verifier.py
@dataclass
class Verdict:
    ok: bool
    unverified: list[str]        # 不可证的断言片段（面向用户解释「哪一句不可证」）

def verify_answer(answer: str, ledger: FactLedger, *, strict_numbers=True) -> Verdict:
    unverified: list[str] = []

    # 1) 具名实体：正文里的 `code` 反引号标识符 & 「」书名号名词，必须在账本
    for tok in _extract_named_entities(answer):     # 反引号 + 中文书名号
        if not ledger.has_entity_named(tok):
            unverified.append(f"提及了本体中不存在的「{tok}」")

    # 2) 数值：正文里的数字断言，必须对得到 run_sql 的 cell（strict 模式）
    if strict_numbers:
        for num in _extract_numeric_claims(answer):  # 排除年份/步骤序号等噪声（保守白名单）
            if not ledger.has_numeric(num):
                unverified.append(f"给出了未经查询证实的数值 {num}")

    # 3) 口径断言：识别「口径/计算为/等于 …」句式，若涉及某指标，须与其 AST 结构等价
    for logic_id, claimed in _extract_caliber_claims(answer, ledger):
        if not ledger.metric_matches(logic_id, claimed):
            unverified.append(f"对指标口径的描述与权威定义不一致")

    return Verdict(ok=not unverified, unverified=unverified)
```

抽取器采用**保守策略**（宁可多判为「需凭证」，触发拒答，也不放过）：
- `_extract_named_entities`：正则 `` `([^`]+)` `` 和 `「([^」]+)」`；
- `_extract_numeric_claims`：`\d[\d,\.]*\s*(万|亿|%|元|单|个|条)?`，用**噪声白名单**排除
  「近 7 天」「第 2 步」「Top 10」这类非结论数字；
- `_extract_caliber_claims`：命中「口径」「计算为」「定义为」「=」后，把出现的指标名对到账本。

### 3.3 改写 grounding 判定（`_stream_agent_events` + `ask`/`ask_stream`）

```python
# _stream_agent_events：done 之前插入校验
verdict = verify_answer(answer, ledger, strict_numbers=bool(ledger.cells or _asks_number(question)))
yield {"type": "done", "payload": {
    ...,
    "_grounded": len(ledger.objects)+len(ledger.metrics) > 0 and verdict.ok,   # 换成断言级
    "_unverified": verdict.unverified,     # 供拒答解释
}}
```

```python
# ask() / ask_stream()：拒答改为「可解释」
grounded = payload.pop("_grounded", False)
if not grounded:
    unverified = payload.get("_unverified") or []
    return self._ungrounded_refusal(..., reasons=unverified)   # 拒答里列出「哪几句不可证」
```

`_ungrounded_refusal` 增参 `reasons`，把 `unverified` 拼进答案：

```
无法基于「XX域」已发布本体可靠回答该问题：
  · 提及了本体中不存在的「毛利率」
  · 给出了未经查询证实的数值 1234 万
请换用本体中已有实体提问，或先补充相关建模。
```

### 3.4 强化 `_enforce_grounded_refs`（引用剥离已有雏形）

现有 `_enforce_grounded_refs` 只清 `referenced_objects/logics`。扩展为：**答案正文里
账本外的具名引用直接剥离**；剥离后若核心断言塌空（无任何账本内实体 + 无 cell）→ 判未接地
→ 拒答。这样即便 verifier 漏判，引用层再兜一道。

### 3.5 F4 测试 `tests/test_formal_grounding.py`

| 用例 | 场景 | 期望 |
| --- | --- | --- |
| 部分接地幻觉 | 命中「订单」，但答案编造字段「毛利率」 | verifier 捕获 → 拒答，列出「毛利率」 |
| 凭空数值 | 未 run_sql 却答「共 1234 单」 | `strict_numbers` 捕获 → 拒答 |
| 口径漂移 | 指标 AST 是 sum(金额)，答案说「按次数计」 | `metric_matches` 失败 → 拒答 |
| 真实接地 | run_sql 返回 3 行，答案复述这 3 行 | 通过，正常作答 |
| 概览问题 | 「有哪些对象」，get_domain_overview 命中 | 通过（实体来自账本） |
| 噪声数值不误杀 | 答案含「近 7 天」「第 2 步」 | 不判为未证数值 |

---

## 第四部分：F2 增量（发布时不变式，作为 F3/F4 的「有据可依」前提）

F3 的扇出判定依赖**基数可信**，F4 的口径校验依赖**AST 可解析**。故 F2 至少先落两条
error 级不变式，其余按 `FORMAL_VALIDATION_PLAN.md` §4 逐步补。

新增 `app/services/ontology_formal.py`，被 `publish.py::PublishService.publish` 前置调用
（`formal_enforcement=error` 时阻断）：

```python
def check_formal_invariants(db, ontology_id) -> list[FormalIssue]:
    issues = []
    issues += _check_relation_well_typed(...)     # 复用 draft_consistency 已有项
    issues += _check_metric_ast_resolvable(...)    # expression_json 能解析 & refs 全 resolve
    issues += _check_derivation_acyclic(...)       # σ=derivation 子图 DAG（Tarjan）
    issues += _check_semantic_type_coherence(...)  # 被指标当 measure 用的字段须 semantic_type=measure
    return issues
```

`_check_metric_ast_resolvable`：遍历 `expression_json.body`，收集所有 `{"ref": rN}`，
确认每个 `rN` 在 `refs[]` 里、且其 `property_id`/`object_type_id` 指向已发布实体
（逻辑与 `expression_formatter._resolve_refs` 同源，直接复用其解析）。

---

## 第五部分：落地顺序与工作量估算

| 步 | 模块 | 依赖 | 估算 | 价值 |
| --- | --- | --- | --- | --- |
| 1 | `ontology_types.py`（枚举+代数） | 无 | 0.5d | F3/F4/F2 共同地基 |
| 2 | `sqlglot` 依赖 + `ontology_projection.py` | 1 | 1d | 证明器输入 |
| 3 | **`sql_soundness.py` + 接入 `_dispatch_run_sql`** | 2 | 2d | ★ 堵静默错数 |
| 4 | `tests/test_sql_soundness.py` | 3 | 1d | 锁死回归 |
| 5 | `agent_grounding.py`（账本）+ 登记点 | 1 | 1.5d | F4 输入 |
| 6 | **`answer_verifier.py` + 改 grounding 判定** | 5 | 2d | ★ 根治幻觉 |
| 7 | `tests/test_formal_grounding.py` + 诱导集基线 | 6 | 1.5d | 验收硬指标 |
| 8 | `ontology_formal.py`（2 条 error 不变式）+ publish 接入 | 1 | 1.5d | F3/F4 前提 |
| 9 | F1 迁移：alembic 唯一约束 + `formalize_ontology_fields.py` + 灰度开关 | 1 | 2d | 长期地基 |

**关键路径：步 1→2→3→4（先上 SQL 证明），再 1→5→6→7（再上断言校验）。** 步 8、9 并行推进。
步 3 与步 6 各自独立可回滚、可灰度（`formal_enforcement` 开关 + `strict_numbers` 开关）。

---

## 附：两个开关（灰度与可回滚）

```python
# SettingsService
formal_enforcement: "off" | "warn" | "error"   # 控 F1 枚举校验 + F2 发布不变式
agent_soundness:    "off" | "on"               # 控 F3 SQL 证明 + F4 断言校验
```

- `off`：完全回到现状，零风险回滚。
- `warn`：F3/F4 只记录「本应拒答」到日志/回执，不真拒（用于对照泄漏基线、观测误杀率）。
- `on/error`：正式生效，达成「宁可拒答不错答」。

上线策略：先 `agent_soundness=warn` 跑一周，用诱导集+黄金集量化「拦截了多少真错答、误杀
了多少真能答」，调稳抽取器/扇出阈值后切 `on`。
