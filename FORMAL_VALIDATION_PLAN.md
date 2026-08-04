# 形式化校验改造方案：让 Data Agent「宁可拒答，不可错答」

> 目标（唯一硬指标）：Data Agent 在查询语义时，**凡是不能被已发布本体形式化蕴含的
> 结论，一律拒答**（提示「本体中无明确定义」），绝不输出无法证明的答案。
>
> 一句话原则：**牺牲完备性（可能拒答本可回答的问题），换取可靠性（答出来的一定对）。**
> 用逻辑术语说，就是让整条问答链路满足 **soundness（可靠性）** 而不追求 completeness。

---

## 0. 为什么现在做不到「不错答」——错误从哪泄漏

当前 grounding 机制（`services/chat_bi.py`）的判定过于宽松：

```python
# _stream_agent_events：任何一个工具返回了东西，就置 grounded_hit=True
if not is_error and (search 命中 or get_object or get_domain_overview or run_sql):
    grounded_hit = True
...
# ask()：只有「什么都没命中」才拒答
grounded = bool(payload.pop("_grounded", False) or referenced_objects or ...)
if not grounded:
    return self._ungrounded_refusal(...)
```

这是**运行级（run-level）**的接地，不是**断言级（claim-level）**的接地。于是错误从
六个缝隙泄漏：

| 缝隙 | 现状 | 后果 |
| --- | --- | --- |
| **A. 部分接地即放行** | 命中任意一个对象就 `grounded=True`，最终作答轮（`_stream_final_answer`）不带工具、无校验 | LLM 在真实对象旁边编造不存在的字段/关系/口径，整段照发 |
| **B. 关键词假阳性** | `_match_objects` 用子串命中（`token in blob`） | 「订单」命中了描述里含「订单」的无关对象，被当作主对象 |
| **C. SQL 语义漂移** | `_apply_mapping` 正则整词替换表名/列名，无存在性校验 | 列名不在本体里 → 要么执行报错（还好），要么 join 错/映射错 → **静默返回错数** |
| **D. 基数导致的扇出** | `cardinality` 是自由文本，join 不校验多重性 | N:M 当 1:N join → SUM 翻倍，数值错得离谱且看不出来 |
| **E. 口径自由文本** | `expression_summary` 是字符串，`expression_json` 是未校验的 LLM 产物 | Agent 读摘要后按自己理解算指标，与权威口径不一致 |
| **F. 语义类型无约束** | `data_type`/`semantic_type` 自由文本 | 对一个分类编码字段做 `SUM()`，语法通过、语义荒谬 |

**根因**：本体层缺形式化 schema（前一轮分析已列明——基数、语义类型、图结构、唯一性
全无约束），问答层缺**断言级的可靠性闸门**。两者要一起补。

---

## 1. 形式化模型（把本体定义成可判定的类型化结构）

把「已发布本体」形式化为一个**有限、封闭世界（Closed-World）**的类型化结构：

```
O = (T, P, R, M, μ)
  T : 业务对象类型集合          （object_types，table_role=business_object）
  P : 属性，p : T → (DataType × SemanticType)
  R : 关系，r : T_src → T_tgt，带 κ(r)=基数, σ(r)=结构类型
  M : 业务逻辑/指标，m 绑定 subject(m)∈T，inputs(m)⊆P
  μ : 物理映射，μ_table : T → 物理表，μ_col : P → 物理列
```

封闭世界假设（CWA）是「不错答」的逻辑基石：**凡 O 中查不到的事实，即视为不成立**，
而不是「未知、可推测」。这与 SSOT「本体是语义权威表达层」完全一致。

可靠性目标（形式化表述）：
```
对答案 A 的每一条原子断言 c ∈ Claims(A)：必须 O ⊨ c（O 蕴含 c）
若 ∃ c 使 O ⊭ c  ⇒  拒答（或剥离该断言；核心断言不可证则整体降级为拒答）
```
因为 O 有限且封闭，`O ⊨ c` 是**可判定的成员/推导检查**，不是一阶定理证明——这正是它
能落地的原因。

---

## 2. 三层形式化闸门

```
┌─────────────────────────────────────────────────────────────┐
│ 闸门①  Schema 形式化      把自由文本字段升级为受控类型系统       │  发布前（一次性+持续）
│         （T-Box 类型化）    cardinality/semantic_type/data_type 枚举化 │
├─────────────────────────────────────────────────────────────┤
│ 闸门②  本体不变式校验      已发布本体必须满足的形式化不变式集       │  发布时（阻断）
│         （T-Box 一致性）    良类型关系图 / 基数一致 / 派生无环 / 口径可解析 │
├─────────────────────────────────────────────────────────────┤
│ 闸门③  问答可靠性证明      每条断言必须有「事实凭证」，SQL 必须过    │  查询时（拒答）
│         （A-Box 可靠性）    静态语义证明；不可证即拒答               │  ★核心
└─────────────────────────────────────────────────────────────┘
```

闸门①②让本体**本身**可被机器推理；闸门③是「不错答」的**直接执行点**。

---

## 3. 闸门①：Schema 形式化（把约束变成机器可判定）

### 3.1 受控词表（替换自由文本）

| 字段 | 现状 | 改为 | 落点 |
| --- | --- | --- | --- |
| `RelationType.cardinality` | 自由文本 | 枚举 `one_to_one/one_to_many/many_to_one/many_to_many`，**NOT NULL** | 模型 + alembic + pydantic validator |
| `Property.semantic_type` | 自由文本 | 枚举语义格（lattice）：`identifier / measure / temporal / categorical / textual / technical` | 同上 |
| `Property.data_type` | 自由文本 | 受控物理类型表（int/decimal/varchar/date/timestamp/bool…），带归一化函数 | 同上 |
| `RelationType.structure_type` | 已是枚举 | 保持，补 NOT NULL | — |

语义格（semantic_type）是 SQL 语义校验的关键——它决定一个字段**能否被聚合、能否做维度**：

```
measure     → 可 SUM/AVG/MIN/MAX，不可 GROUP BY 主维
identifier  → 可 JOIN、可 COUNT DISTINCT，不可 SUM
temporal    → 可做时间窗过滤/GROUP BY，不可 SUM
categorical → 可 GROUP BY / WHERE 等值，不可 SUM
textual     → 仅可 LIKE/展示，不可聚合
technical   → 默认不进业务查询（需显式放行）
```

### 3.2 DB 级约束（应用层校验不是原子的）

在 `object_types`/`relation_types`/`properties` 上补 DB 约束（alembic）：

```sql
-- 对象名在本体内唯一（现在只靠 Python 遍历 validate_ontology）
ALTER TABLE object_types ADD CONSTRAINT uq_object_ontology_name UNIQUE (ontology_id, name);
-- 属性名在对象内唯一
ALTER TABLE properties  ADD CONSTRAINT uq_prop_object_name    UNIQUE (object_type_id, name);
-- 关系名在本体内唯一
ALTER TABLE relation_types ADD CONSTRAINT uq_rel_ontology_name UNIQUE (ontology_id, name);
```

### 3.3 平滑迁移（存量自由文本 → 枚举）

- 写 `scripts/formalize_ontology_fields.py`：把存量 `cardinality`（`1:N`/`many_to_one`…）
  归一到枚举；`semantic_type` 用现有 `object_classifier` 的推断兜底；无法归一的置
  `unknown` 并标 `needs_review`，**不阻断存量、但进复核队列**。
- 迁移期枚举校验设为 `warn`，全量清洗后切 `error`（灰度，避免一刀切炸历史数据）。

---

## 4. 闸门②：本体不变式校验（发布前的 T-Box 一致性）

把 `services/draft_consistency.py::validate_ontology` 从「引用完整性检查」升级为
**形式化不变式检查器**。新增 `services/ontology_formal.py`，产出结构化 `FormalIssue`
（error 级阻断发布，warn 级进复核）：

| 不变式 | 形式化含义 | 级别 |
| --- | --- | --- |
| **良类型关系** | ∀r∈R: dom(r),ran(r) ∈ T ∧ 均为 business_object | error（已部分有） |
| **基数一致性** | 声明 κ(r) 必须与物理外键方向/唯一性证据相容（`source_evidence` 里的 FK 方向） | warn→error |
| **派生无环** | σ(r)=derivation 的子图必须是 DAG（PROV-O：wasDerivedFrom 不可成环） | error |
| **指标良绑定** | ∀m∈M: subject(m)∈T ∧ inputs(m)⊆P(可解析) ∧ 聚合字段 semantic_type=measure | error |
| **口径可解析** | `expression_json` 必须能解析为 AST，且所有引用 resolve 到已绑定 Property | error |
| **语义类型自洽** | 被指标当 measure 用的字段，其 semantic_type 必须=measure | error |
| **业务图连通** | 每个已发布 business_object 至少参与一条关系（无孤岛，SSOT §拓扑信号） | warn |
| **桥表已落地** | table_role=bridge 必须被某 r 作 mapping_object（现有 `bridge_object_not_materialized`） | error |

关键点：**这些不变式让本体「可推理」**——闸门③要用的「关系是否存在」「基数多少」
「字段能否聚合」都从这里得到形式化保证，而不是运行时猜。

派生无环检测（新增，纯函数便于单测）：

```python
# services/ontology_formal.py
def assert_derivation_acyclic(relations) -> list[FormalIssue]:
    """σ(r)=derivation 的边构成的有向图必须无环（PROV-O wasDerivedFrom）。"""
    graph = build_digraph(r for r in relations if r.structure_type == "derivation")
    cycles = find_cycles(graph)          # Tarjan SCC
    return [FormalIssue("derivation_cycle", ...) for c in cycles]
```

---

## 5. 闸门③：问答可靠性证明（★「不错答」的执行点）

这是最核心的改造。把 `chat_bi.py` 的**运行级布尔接地**换成**断言级凭证账本 + 事后可靠性
校验**。

### 5.1 事实凭证账本（Fact Ledger）

Agent 每次工具调用的返回，都登记为**基本事实（ground atom）**，构成本轮的封闭世界 F：

```python
# services/agent_grounding.py（新增）
@dataclass(frozen=True)
class Fact: ...

class FactLedger:
    """本轮问答中，工具实际返回的、可被引用的全部事实。CWA：不在账本 = 不成立。"""
    objects:   dict[str, ObjectFact]     # id -> {name, display_name}
    props:     dict[tuple[str,str], PropFact]  # (obj_id, prop_name) -> {data_type, semantic_type}
    relations: dict[str, RelationFact]   # rel_id -> {src, tgt, cardinality, structure}
    metrics:   dict[str, MetricFact]     # logic_id -> {expression_ast, inputs}
    cells:     list[DataCell]            # run_sql 实际返回的 (col,row,value)
```

登记点就在现有的 `_dispatch_agent_tool` 返回处——**只把工具真实返回的字段入账**，
LLM 说的一律不入账。

### 5.2 事后可靠性校验（Answer Verifier）

在 `_stream_agent_events` 产出 `done` 之前，插入**校验 pass**：把答案正文解析成原子断言，
逐条对账本 F 做成员检查。

```python
# services/answer_verifier.py（新增）
def verify_answer(answer: str, refs: References, ledger: FactLedger) -> Verdict:
    claims = extract_claims(answer)          # 结构化断言抽取（见 5.4）
    unverified = [c for c in claims if not ledger.entails(c)]
    if unverified:
        return Verdict.REFUSE(reason="以下结论无法由本体证明", claims=unverified)
    return Verdict.OK
```

判定规则（对齐硬指标）：
- **数值类断言**（「共 1234 单」「金额 5.6 万」）：必须能对到 `ledger.cells` 里的具体单元格，
  否则整答拒绝——LLM 不得凭空说数。
- **实体/字段/关系类断言**：必须命中 `ledger.objects/props/relations`。命中不了的**具名实体**
  出现在正文 → 判为幻觉。
- **口径类断言**：必须与 `ledger.metrics[id].expression_ast` 一致（结构等价），不一致 → 拒绝。
- 全部通过才走现有的 `done`；否则返回强化版 `_ungrounded_refusal`，明确指出「哪一句不可证」。

### 5.3 SQL 静态语义证明（在执行/建议前，架构级保证）

现有 `execute_message_sql` 的注释已经点破方向：「准确性是**架构保证**的而非提示词保证的」。
把这句话变成真正的**静态校验器**，放在 `_dispatch_run_sql` 执行**之前**：

```python
# services/sql_soundness.py（新增）
def prove_sql_sound(sql, ontology, mapping) -> SqlCertificate | Rejection:
    ast = sqlparse→结构化(tables, columns, joins, aggs, group_by, filters)
    # 1) 表存在性：每张表 = 已发布对象且有物理映射
    for t in ast.tables:
        if t not in μ.tables or resolve_object(t) is None: return Reject("表 %s 不在本体")
    # 2) 列归属性：每列 = 其对象的已发布属性
    for c in ast.columns:
        if resolve_property(c.table, c.name) is None: return Reject("列 %s 不属于对象")
    # 3) JOIN 合法性：每个 join 谓词必须对应一条已声明 RelationType
    for j in ast.joins:
        r = find_relation(j.left_obj, j.right_obj)
        if r is None: return Reject("对象 %s 与 %s 间无已声明关系，拒绝臆造 JOIN")
        # 4) 扇出安全：有聚合时，join 多重性不得放大度量（缺陷 D）
        if ast.has_aggregation and fans_out(r, j.direction):  # 1:N 错向 / N:M
            return Reject("该 JOIN 会使度量扇出翻倍（基数 %s），拒绝" % r.cardinality)
    # 5) 聚合合法性：SUM/AVG 只能作用于 semantic_type=measure；GROUP BY 只能维度/时间
    for agg in ast.aggs:
        if semantic_type(agg.col) != "measure": return Reject("对非度量字段聚合")
    return SqlCertificate(...)   # 通过 → 携证放行执行
```

任何一步失败 → **不执行、不建议**，直接回「本体中无明确定义支撑该查询」。这样即使 LLM
写出语法正确但语义错误的 SQL，也进不了数据库、更不会把错数喂回答案。

### 5.4 断言抽取（extract_claims）的务实实现

不追求 NLP 完美解析，用**保守抽取**（宁可多判为「需凭证」）：
- 具名实体：正文里出现的 `` `code` `` 反引号标识符、「」书名号里的名词 → 必须在账本；
- 数字：正则抽取数值 token → 必须对到 cells；
- 口径动词（「计算为」「口径是」「等于」后接表达式）→ 必须对到 metric AST。
- 抽取不确定的句子，采用**引用完整性回填**（现有 `_enforce_grounded_refs` 的强化版）：
  答案只能引用账本内实体，账本外引用一律剥离，剥离后若核心断言塌空 → 拒答。

### 5.5 与现有代码的接缝（最小侵入）

| 新增/改动 | 位置 | 说明 |
| --- | --- | --- |
| `FactLedger` 登记 | `chat_bi._dispatch_agent_tool` 返回处 | 工具结果入账，替代散落的 `grounded_hit` 布尔 |
| `prove_sql_sound` | `chat_bi._dispatch_run_sql` 执行前 | 不过则不 `execute_sql`，返回结构化拒绝 |
| `verify_answer` | `_stream_agent_events` 产 `done` 前 | 不过则改发 `_ungrounded_refusal` |
| 硬化 grounded 判定 | `ask()`/`ask_stream()` | 由「命中过任意工具」改为「通过 verify_answer」 |
| Mock 路径 | `_mock_answer` 已较保守 | 复用同一 `verify_answer`，统一可靠性语义 |

---

## 6. 分阶段落地（灰度、不阻断存量）

| 阶段 | 内容 | 交付物 | 风险闸门 |
| --- | --- | --- | --- |
| **F0 基线** | 建 `formal` 测试集：一批「应拒答」的诱导性问题 + 「应答对」的黄金问题 | `tests/test_formal_grounding.py` | 用现状跑出泄漏基线数字 |
| **F1 Schema 形式化** | 枚举化 + DB 约束 + 迁移脚本；枚举校验 `warn` | 闸门① | 存量零阻断 |
| **F2 本体不变式** | `ontology_formal.py` + 发布时接入；派生无环/指标良绑定/口径可解析 | 闸门② | 发布前给报告，先 warn 后 error |
| **F3 SQL 语义证明** | `sql_soundness.py` 接入 `_dispatch_run_sql` | 闸门③-SQL | 错向 join / 非度量聚合被拦 |
| **F4 断言级可靠性** | `agent_grounding.py`+`answer_verifier.py`，替换布尔接地 | 闸门③-Answer | 泄漏基线→0 |
| **F5 切 error + 硬化** | 枚举/不变式转 error；grounded 判定完全走 verifier | — | 回归黄金集不掉点 |

每阶段独立可回滚；F3/F4 是「不错答」的决定性阶段，F1/F2 是它们的形式化前提。

---

## 7. 验收标准（可度量）

1. **可靠性（硬指标）**：诱导性问题集里，**零错答**——要么正确、要么显式拒答。
   错答（自信地给出本体中不存在的字段/关系/口径/错数）计为**严重缺陷**。
2. **拒答可解释**：每次拒答必须指明「哪条断言 / 哪张表 / 哪个 join 不可证」，
   而非笼统「无法回答」。
3. **SQL 架构保证**：任何进入 `execute_sql` 的语句都携带 `SqlCertificate`；
   无证书语句 100% 被拦。
4. **完备性不塌方**：黄金问题集的回答率下降 ≤ 可接受阈值（可靠性优先，但需可观测）。
5. **发布门槛**：不满足闸门②不变式的本体无法发布（error 级）。

---

## 8. 为什么这套是「形式化」而非又一层启发式

- **封闭世界 + 有限结构** ⇒ `O ⊨ c` 可判定，校验是**推导/成员检查**而非概率；
- **凭证账本** ⇒ 每条断言有**可复核的来源**，可靠性是**可证明的**，不是提示词祈使；
- **SQL 静态证明** ⇒ 语义正确性由**类型系统 + 基数代数**保证，与 LLM 表现解耦；
- **soundness 优先** ⇒ 系统行为可预测：**答出来的可信，答不了的显式拒绝**，正是本项目
  「语义权威表达层」定位要求的性质。

> 落地顺序建议：先做 F0 基线 + F3（SQL 证明）——投入最小、堵住「静默错数」这个最危险
> 的泄漏；再做 F4（断言级可靠性）根治幻觉；F1/F2 作为让二者「有据可依」的形式化地基
> 同步推进。
