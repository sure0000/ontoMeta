# Data Agent V2 改造方案：让语义层从「否决者」变成「生成器」

> 状态：**全部交付** —— P0 / P1 / P2 / P3 / P4 五期完结（见 §8.1）
> 前序：[DATA_AGENT_REDESIGN.md](./DATA_AGENT_REDESIGN.md)（V1：单发问答 → 多步工具编排）、
> [FORMAL_VALIDATION_PLAN.md](./FORMAL_VALIDATION_PLAN.md)（F1–F4：宁可拒答不可错答）
> 主战场：`backend/app/services/chat_bi.py`

**当前进度一览**

| | 起点 | 现在 |
| --- | --- | --- |
| Agent 工具数 | 7（全是 CRUD 包装） | **12**（+3 语义能力、+1 澄清、+1 子 agent） |
| 检索 | 纯 ILIKE，同义词全落空 | **混合**：ILIKE 优先 + 向量补召回 |
| system prompt | 2590 字符 / 40 行铁律 | **963 字符 / 11 行**（只剩工具选择策略） |
| 后端测试 | 755 | **867**（+112，全绿） |
| golden 回归用例 | 无 | **20 例 / 7 类**（LLM stub，CI 确定性） |
| 每问 LLM 调用 | 3.5 | **2.6**（−26%，P4.5） |
| 绝对拒答数 | — | 原有 3 条拒答用例行为**六期未变** |

---

## 0. 一句话结论（改造前的诊断）

> §0–§1 记录的是**改造启动时**的判断，保留原文不改写——它是后面所有决策的依据。
> 各项当前状态见 §8.1，能力维度的进展见 §1.2。

改造前的 Data Agent 是**「把本体当文档来检索的通用 agent」**，不是**「以语义层为执行引擎的 agent」**。

语义层里最有价值的三样东西——**关系图（基数/结构类型）、语义类型（measure/dimension/
identifier）、指标表达式（`expression_json`）**——在架构里只承担了**事后否决**的角色
（`sql_soundness` 拒 JOIN、拒聚合，`answer_verifier` 拒答），**从未参与事前生成**。

模型看到的工具全是 `search_x` / `get_x` 的 CRUD 包装，它必须自己「读懂」语义再手写 SQL，
然后被形式化守卫毙掉。于是 `_AGENT_SYSTEM_PROMPT` 膨胀成 40 行铁律——
**架构缺陷正在用 prompt 打补丁**。

> **改造总纲**：把 `chat_bi.py` 里「模型自己想办法」的部分，逐段替换成「语义层确定性给答案」。
> **进度指标**：prompt 行数。启动时 40 行 → 目标 ≤10 行。凡是能由构造保证的，不写进 prompt。
>
> **✅ 已达成**：741 字符 / 10 行（P2 交付后），且剩下的全是工具选择策略——
> 「该先调谁」这类判断守卫拦得住结果却教不会顺序，只能靠提示词。

---

## 1. 与 Claude Code / Codex / Cursor 的架构差距（启动时诊断）

最后一列「当前」为改造后状态。

| 维度 | CC / Codex / Cursor | 本项目 Data Agent（改造前） | 差距性质 | 当前 |
| --- | --- | --- | --- | --- |
| 常驻上下文 | CLAUDE.md / AGENTS.md 常驻，项目先验免费 | 每轮从零；域概览靠模型主动调 `get_domain_overview`，还常被截断 | 架构缺失 | ✅ 域语义卡常驻（P2.1） |
| 上下文压缩 | 自动 compaction、按语义摘要 | `result_text[:8000]` **字符硬截断**（`chat_bi.py:1603`），会把 JSON 截在半路 | 架构缺失 | ✅ 语义降级阶梯，回灌恒为合法 JSON（P2.2） |
| 外部记忆 | 文件系统 = 无限、持久、可 diff | 无。`payload.steps` 只读回放，下一轮无法引用上轮结果集 | 架构缺失 | ⬜ 未做（结果集寄存器属 P4 之后） |
| 检索 | 语义检索（embedding）+ ripgrep 双通道 | 纯 `ILIKE`。prompt 被迫写「关键词优先用中文」——同义词一个都命不中 | 能力缺失 | ✅ 四层齐备：词面 + 向量（P1.5）+ 图结构（P1.2）+ 口径（P3） |
| 控制流 | 主 agent + Task 子 agent（上下文隔离）+ plan mode + TodoWrite | **单层 6 步扁平循环**（`_AGENT_MAX_STEPS=6`），一个模型一根线 | 架构缺失 | ✅ 预算分离（P4.4）+ 检索子 agent 上下文隔离（P4.2，隔离比 28×） |
| 失败自愈 | 报错回灌 → 模型改 → 重试，天然收敛 | soundness 拒绝**只说「拒绝臆造字段」不给候选**；verifier 失败是**终局拒答** | 设计缺陷 | ✅ 拒绝带修复信号（P1.4）+ 校验不过先重写一次（P4.3） |
| 工具抽象层次 | primitive 可组合（Read/Edit/Bash） | CRUD 包装，无一个工具封装「语义计算」 | **核心差距** | ✅ 3 个语义能力工具（P1.2/P1.3/P3） |
| 产物 | 文件、可 diff、可复用 | 一次性 payload；无写侧出口 | 飞轮缺失 | ⬜ 未做（写侧提案飞轮属 P4 之后） |
| 评测 | eval harness | 无 golden set。发布新本体会静默改变 Agent 行为 | 工程缺失 | ✅ golden set 20 例 + 遥测基线（P0） |

### 1.1 三个最要命的具体问题

**A. 734 对象 × 6 步 = 必然走不完。** ✅ **已解决（P4.4 + P4.2）**
ERP 全量域下「检索 → 读细节 → 写 SQL → 被 soundness 拒 → 改 → 重跑」是 6+ 步，
`_AGENT_MAX_STEPS` 一到就强制收尾作答，此时模型手上往往只有半截证据。
> 工具轮 6→8、自愈轮独立计，不再共用一个上限；检索可整体外包给子 agent，
> 「找的过程」不再占主循环的步数与上下文。

**B. 模型永远不知道字段有哪些取值。**
`sql_soundness` 证明的是 **schema 合法性**（表存在、列归属、JOIN 已声明、聚合语义合规），
**不证明谓词有意义**。模型写 `WHERE status = '已完成'` 而库里存的是 `Completed` ——
soundness 全绿、执行返回 0 行、答案「该状态无数据」。**静默错误，全链路无人拦截。**
> ✅ **已解决（P1.3）**：`profile_values` 给出真实取值分布，prompt 规定字面量必须照抄；
> 取不到时不许凭空写。

**C. 最终作答轮打了两次 LLM。**
`chat_bi.py:1515`：循环里 `resp` 已返回完整 content 却被丢弃，再调 `_stream_final_answer`
发**第二次全上下文请求**；这次 `stream=True` 的 token 又被 buffer 进 `answer`（没有透传），
最后由 `_emit_answer_tokens` 加 `sleep(0.012)` **假打字机**吐出。
等于每问一次多付一次全量 prefill + 一次生成，换来一个模拟的流式效果。
> ✅ **已解决（P4.5）**：收尾轮直接用 `msg.content`。
> `avg_llm_calls` 3.5 → **2.6**（−26%）。

### 1.2 三项核心能力的盘点（含当前进展）

| 能力 | 改造前 | 现在 | 与 CC 的形态差异 |
| --- | --- | --- | --- |
| **验证** | 已有且强于 CC/Cursor（F3 `sql_soundness` + F4 `FactLedger`/`answer_verifier`） | ✅ 补到 4 层：+ **值域层**（P1.3 `profile_values`）、+ **计划层**（P3 `caliber_trace`）；拒绝均带修复信号（P1.4）；顺带修掉 F3 一处既有误拒 | CC 靠外部 oracle（编译器/测试/lint）；本项目有本体做**封闭世界**，能做静态证明 |
| **语义搜索** | 最弱，纯 `ILIKE` 单层 | ✅ 四层：词面（ILIKE）+ **向量**（P1.5，同义词）+ **图结构**（P1.2）+ **口径**（P3）。后两层是本体独有的 | Cursor 只有词面+向量两层；本项目多图结构层，这是本体独有的 |
| **任务规划** | Data Agent 侧**为零** | ◐ 缺口识别→主动反问（P4.1）+ 检索外包（P4.2）均已交付；完整结构化查询计划仍未做（被 `compile_metric` 参数覆盖了大半） | 不该照抄 TodoWrite——Data Agent 要的是**查询计划**不是任务清单 |

> **关键观察**：写侧的规划设施**已经很完整**——`agent_pipeline`（draft→validate→confirm→execute）、
> `agents/registry`（drafter/executor 双端）、`job_planner`（编译 JobSpec）、
> `draft_task_service`（异步任务/断点/进度）。**Data Agent 是全项目唯一没接进这套骨架的模块。**
> 所以「任务规划」不是从零造，是把只读问答侧接进已有骨架。

---

## 2. 分期总览

四期递进，每期独立可上线、可回滚。

| 期 | 状态 | 主题 | 前置 | 独立价值 |
| --- | --- | --- | --- | --- |
| **P0** | ✅ | 横切基建（评测/遥测） | 无 | **必须最先做**，否则后面每期都是「感觉变好了」 |
| **P1** | ✅ | 语义工具化 + 止血 | P0 | JOIN 可事前生成、堵住值域静默错误、修权限旁路、同义词可召回 |
| **P2** | ✅ | 上下文架构 | 无（与 P1 并行） | 去铁律（prompt −71%）、样本≠全集由结构保证 |
| **P3** | ✅ | 口径编译器（metric/tag/rule） | P1.2 | **兑现语义层核心价值：口径一致性** |
| **P4** | ✅ | 分层编排 + 自愈 | P1–P3 | LLM 调用 −26%、拒答可自愈、缺口主动反问、检索上下文隔离 |

### 依赖图

```
P0 ✅ ──────────────────────────────────────► 贯穿全程（每期对照基线跑 golden set）
    │
    ├── P1.1 权限止血 ✅
    ├── P1.2 join_path ✅ ─┬──► P3 编译器 ✅ ──┐
    ├── P1.3 profile ✅    │                  ├──► P4 编排 ✅
    ├── P1.4 拒绝提示 ✅ ──┘                  │
    └── P1.5 语义检索 ✅ ──────────────────────┘

    P2 上下文 ✅（与 P1 无耦合）─────────────► P4 ✅
```

**五期全部交付。** P4.2 检索子 agent 原本判为「无法验证故不做」，
后来先补了大本体 fixture 把收益变成可测的数，才动手——见 §8.11。

---

## 3. P0 · 横切基建

没有这层，后面每一期都只能靠感觉判断好坏。

| 交付物 | 内容 |
| --- | --- |
| `backend/tests/fixtures/golden_questions.py` | 用例集，五类：概览 / 检索取详 / 取数 / 守卫 / **应拒答** |
| `backend/tests/test_chat_bi_golden.py` | 离线回归 + LLM stub：断言调了哪些工具、是否拒答、run_sql 三态、拒绝码与提示 |
| `backend/app/services/agent_telemetry.py` | 每轮记录：步数、tool 分布、soundness 拒绝码分布、verifier 拒答率、LLM 调用次数 |
| `GET /api/chat-bi/telemetry` | 遥测快照端点 |

**关键决策**

1. **golden set 用 LLM stub 跑**（固定 tool_call 序列），保证 CI 确定性；
   真实模型的端到端验证单独打 `@pytest.mark.live`，本地手动跑。
   它**不测模型智商**，测的是工具分发 / 结果收割 / 接地判定 / 拒答闸门 / 权限降级——
   每次跑的差异只可能来自我们改的代码。
2. **遥测是内存计数器**，不落库——它服务于「改造期间的对照」，不是生产可观测性。
   改造结束后可整体摘除而不留 schema 债。
3. **用例写成 Python 而非 YAML**：`pyyaml` 不在 `requirements.txt`（只是传递依赖），
   且脚本化的工具调用序列用 Python dataclass 表达更直接、可类型检查。

**验收**：`make test` 全绿，且能打印一份基线报表。**这份基线就是后面每一期的对照组。**

---

## 4. P1 · 语义工具化 + 止血

### P1.1 权限旁路（最先，几十行）

**问题**：`POST /api/chat-bi/ask` 无 override，方法兜底到 `editor`（`auth.py:157`），
而它内部的 `run_sql` 直打真实 DSN（`chat_bi.py:1004`）；同一个人手动点
`POST /api/chat-bi/messages/{id}/execute` 却要 `publisher`（`auth.py:141`）。

> **一个 editor 手动执行 SQL 会被 403 挡住，但让 Agent 代跑同一条 SQL 就通过了。**
> 工具化把权限模型绕过去了。

**改法**

- `ChatBiService.ask/ask_stream` 增加 `principal_role` 入参，由 API 层从
  `request.state.principal_role`（`AdminAuthMiddleware` 写入）取，透传到 `_dispatch_run_sql`
- 角色不足时 `run_sql` **降级为「仅建议 SQL」**（复用已有的「无可执行数据源」分支），
  而不是硬报错——体验不掉，能力边界清晰
- 门槛由 `agent_run_sql_min_role`（默认 `publisher`）控制，与 `/execute` 对齐：
  **同一个动作在两条路径上必须同价**
- **fail-closed**：拿不到角色一律视为不够格（沿用 `auth.py` 既有取向）

**为什么用降级而不是加 auth override**：加 override 会让 editor 连问都不能问，
但检索类工具对 editor 是合法的。**权限应该约束到工具粒度，而不是端点粒度**——
这也是后续 P3/P4 引入更多工具时的正确基线。

### P1.2 `find_join_path`

**新文件** `backend/app/services/semantic_navigator.py`

```
find_join_path(proj, from_obj, to_obj, max_hops=3)
  → [{ path: [obj_a, rel_1, obj_b, ...],
       on: ["a.customer_id = b.customer_id", ...],
       cardinality_chain: ["1:N", "N:1"],
       fanout_risk: "sum/avg 会沿 a↔b 展开重复计数",
       safe_aggs: ["count_distinct"] }]
```

- 图数据取自 `OntologyProjection.relations_by_pair`（已有）
- ON 字段推断**直接复用** `warehouse_generator._foreign_keys_by_object` 的既有规则：
  `source_evidence.foreign_key` → `source_field` → `fk_column` → 回退 `<tgt>_id` = `_primary_key_of(tgt)`
- 扇出判定复用 `sql_soundness._fanout_reason`

> **硬约束**：navigator 与 prover 必须吃**同一份** `OntologyProjection`，
> 否则会出现「navigator 说能连、prover 说不能连」的自相矛盾。

### P1.3 `profile_values`

复用 `data_app_executor.execute_sql`，按 `semantic_type` 分派：

| semantic_type | 查询 |
| --- | --- |
| dimension / identifier / categorical | `SELECT col, COUNT(*) GROUP BY col ORDER BY 2 DESC LIMIT 20` |
| measure | `SELECT MIN, MAX, AVG, COUNT(*), COUNT(col)` |
| time | `SELECT MIN, MAX, COUNT(DISTINCT date_trunc(...))` |

**结果按 `(ontology_id, property_id)` 缓存**（取值分布变化慢），否则每问一次多打一次库。

**P1 里价值最高的一项**——它把 §1.1.B 的静默错误变成可发现。

### P1.4 拒绝消息带修复信号

**核心思想：把守卫从守门员变成教练。**

`SqlRejection` 加 `hint` 字段：

| 拒绝码 | 现在 | 改成（追加） |
| --- | --- | --- |
| `unknown_column` | 「字段 X 不属于对象 Y，拒绝臆造字段」 | `did_you_mean`（编辑距离最近 3 个）+ `available`（该对象合法字段） |
| `unknown_table` | 「表 X 不对应任何已发布业务对象」 | `did_you_mean`（最近的已发布对象名） |
| `undeclared_join` | 「A 与 B 之间没有已声明的业务关系」 | `join_path`（P1.2 的多跳结果）；P1.4 阶段先给「A/B 各自的已声明关系对端」 |
| `fanout_risk` | 「会导致重复计数」 | `safe_rewrite`：改用 `COUNT(DISTINCT ...)` |
| `illegal_aggregation` | 「对非度量字段做 SUM 无意义」 | `measures_of_object`（该对象的 measure 字段清单） |
| `illegal_group_by` | 「按该字段分组通常是口径错误」 | `groupable_of_object`（该对象可分组字段清单） |
| `ambiguous` | 「列名歧义或缺表限定」 | `candidates`（含该列的对象清单，提示加表限定） |

改动量最小、见效最快：拒绝结果本就作为 `role:tool` 回灌给模型，加了候选清单模型下一步就能自修。

### P1.5 `semantic_search`

- 索引：ObjectType(name+display+desc) / Property(name+display) / BusinessLogic(name+display+summary)
- 存储：`pgvector`（Postgres 已在栈内）
- **失效钩子挂在发布路径**（`publish.py`）——发布即重建该本体索引
- 检索**混合**：ILIKE 精确命中优先，向量补召回，合并去重

**P1 验收**（P1.1–P1.4 已达成，P1.5 待办）
- ✅ 多跳关联类由 `find_join_path` 覆盖；`undeclared_join` 拒绝**从未在 golden 基线中出现过**
  （拒绝码分布五期恒为 unknown_column / illegal_aggregation / unknown_table 各 1）
- ✅ 「检索关键词优先用中文」整段已删——改由语义卡的 `naming_note` **从真实数据观察**得出（P2.1）
- ✅ 语义检索（P1.5）已交付：`search_objects` / `search_logics` 走混合检索，
  同义词（往来单位 → 客户）可召回；默认关闭，未配嵌入服务时行为与改造前一致

---

## 5. P2 · 上下文架构（可与 P1 并行）

### P2.1 域语义卡

**新文件** `backend/app/services/domain_semantic_card.py`。发布时生成、缓存，运行时拼进 system prompt：

```
【客户域·语义卡】
业务板块（6）：客户主数据 / 订单履约 / 结算 / ...     ← community_detection 聚类
核心事实表（5）：订单(1.2M行) / 支付流水 / ...        ← table_role + DataHub 行数
核心维度（8）：客户 / 商品 / 区域 / 时间 / ...
已发布指标（前 30）：GMV / 客单价 / 复购率 / ...
规模：734 对象 / 4113 关系 / 128 指标
命名规范：对象名中文，字段名英文 snake_case
```

**直接替代** prompt 里 1b/1c 两大段铁律，且省掉一次 `get_domain_overview` 调用
（现在占 6 步预算里的 1 步）。

### P2.2 结构化压缩替代字符截断

`chat_bi.py:1603` 的 `result_text[:8000]` 会把 JSON 截在半路。改成语义降级链：

```
完整 → 丢 description → 丢次要字段 → items 采样但保 total_matched/facets → 仅摘要
```

> **不变式：回灌给模型的永远是合法 JSON。**

### P2.3 分面聚合替代截断清单

`_search_envelope` 现在靠 prompt 求模型「别把样本当全集」（`chat_bi.py:86` 那段铁律）。
改成结构保证：

```json
{ "total_matched": 140,
  "facets": { "by_cluster": {"订单履约": 62, "结算": 41},
              "by_table_role": {"fact": 88, "dim": 52} },
  "sample": [ /* 8 条 */ ],
  "next_page_token": "..." }
```

模型看到 facets 就不会说「共有以下 8 个」。**能由数据结构保证的，不要写进 prompt。**

**P2 验收**：概览/列举类通过率；**prompt 2590 → 741 字符（10 行）**；
「样本 ≠ 全集」由键名承载而非提示词。
（原写的「平均步数下降」未兑现，理由见 §8.6 第 6 条。）

---

## 6. P3 · 口径编译器（核心价值）

**前提已具备**：`expression_json` 已是可直接编译的结构（`expression_formatter._mock_format` 产出）：

```json
{ "type": "metric",
  "refs": [{ "ref_id", "object_type_id", "object_name", "property_name", "semantic_type" }],
  "body": { "operation": "sum", "args": [{"ref": "..."}],
            "filter": {"op":"and","conditions":[{"left":{"ref"},"op","right":{"literal"}}]},
            "group_by": [{"ref": "..."}], "window": null } }
```

`refs` 带 `object_type_id` + `property_name`，配 `OntologyProjection` + `find_join_path`
就能确定性出 SQL。

**新文件** `backend/app/services/metric_compiler.py`

```
compile_metric(db, logic_id, *, dimensions=[], filters=[], grain=None, limit=100)
  → { sql, join_path, caliber_trace, certificate }
```

**编译流水线**

```
BusinessLogic.expression_json
   ↓ 1. 解析 refs → 对象/字段（对 OntologyProjection 校验，未发布即拒）
   ↓ 2. body.operation + args      → SELECT 聚合表达式
   ↓ 3. body.filter                → WHERE（复用 _coerce_condition 规范形）
   ↓ 4. body.group_by + 入参 dims  → GROUP BY
   ↓ 5. 跨对象 → find_join_path(P1.2) 生成 FROM/JOIN + 扇出防护
   ↓ 6. grain                      → 时间字段 DATE_TRUNC（按 dialect）
   ↓ 7. prove_sql_sound 自证        ← 编译产物必须过自己的证明器
   → sql + caliber_trace
```

**三个关键设计决策**

1. **编译器产物必须过 `prove_sql_sound`**。看似冗余，实为关键不变式——保证
   「编译器不会生成证明器会拒的 SQL」，二者永不打架。任何一次自证失败都是编译器 bug，CI 直接红。
2. **`caliber_trace` 是一等交付物**，不是日志。它取代现在事后反推的 `_steps_to_caliber`，
   让前端口径卡从「猜测」变「契约」。
3. **`logic_type` 分三条路**：`metric` → 聚合查询、`tag` → CASE WHEN 派生列、
   `rule` → 校验谓词。P3 只做 `metric`（占比最高），tag/rule 列为 P3.5。

**工具侧**：`get_logic` 返回加 `compilable` 与可用维度清单；新增 `compile_metric` 工具。
模型从「照着口径文本重写 SQL」变成「选口径 + 选维度」——
**幻觉面从整个 SQL 语法空间坍缩到一组枚举**。

**P3 验收**（已达成，除第三条外）
- ✅ 每个已发布 metric 类 BusinessLogic 都能编译或给出明确的不可编译原因
  （9 种拒绝码，每种带修复信号）
- ✅ 编译器产出的每一条 SQL 都过 `prove_sql_sound`（架构不变式，见 §8.5；
  `test_every_compiled_output_is_certified` 对 6 种参数组合各独立验证一次）
- ~~同一指标在 Data Agent / 数据应用 / 物化 ETL 三处产出一致 SQL~~ →
  **当前无法验证**：物化侧（`warehouse_generator._logic_tables`）**根本不翻译口径**，
  只建一张 `stat_date + metric_value` 的空壳表，真正的口径执行属于尚未落地的 M6。
  也就是说现在没有「第二套实现」可比对——编译器是**唯一**实现，这本身就是一致性的
  最强形态。已在 `_logic_tables` 就地留下指针：M6 必须调用 `compile_metric`，
  不得另写一套翻译。

---

## 7. P4 · 分层编排 + 自愈

### 7.1 Planner

`_stream_agent_events` 前置规划轮，产出结构化 plan：

```
{ metric, dimensions[], filters[], grain, compare, gaps[] }
```

- plan 作为新 SSE 事件 `plan` 推给前端，**可展示可编辑**（对应 CC 的 plan mode）
- `gaps[]` 非空 → 走**澄清反问**分支，不硬答

> **形态判断**：不要照抄 TodoWrite。CC 的 todo 是「工程任务清单」，因为写代码长程、
> 有副作用、需人监督进度。Data Agent 是只读问答，它需要的是**语义查询计划**。

### 7.2 检索子 agent

在 734 对象里定位相关子图是高 token、低价值输出的活。fan-out 到独立上下文，
只回 `{object_ids: [...], reason: "..."}`。**主上下文不被检索垃圾污染**——
这是 CC 的 Task 工具在这里的正确类比。

### 7.3 自愈回环

现在 `unverified` 非空直接 `_ungrounded_refusal`（`chat_bi.py:374`），
模型连「哪句没凭证」都收不到。改成：

```
verifier 不过 → 回灌 unverified 片段 + 账本可证事实 → 重写一次 → 仍不过才拒答
```

上限 1 次，避免死循环。

### 7.4 分阶段预算

`_AGENT_MAX_STEPS=6` 全局一刀切 → 规划 1 / 检索 2 / 编译执行 2 / 自愈 1。

### 7.5 顺手修双次 LLM 调用

最终轮直接 `stream=True` 边流边攒，verifier 改**增量校验**（每积累一段校验一次），
不过则立即中止并切拒答文案。**省一次全量 prefill + 真流式。**

**P4 验收**：多跳/对比类通过率；拒答率下降但**错答率不上升**
（两个必须同时看，否则就是把拒答换成了幻觉）；LLM 调用次数减半。

---

## 8. 灰度、回滚与进度

- 行为开关按**具体语义**取名，不搞笼统的 `v1/v2`（见 §8.2 第 1 条）
- 新验证层先上 `warn` 观察，再转 `on`（沿用 `agent_soundness` 三档风格）
- 每期结束在 §8.2–8.6 记录「设计与实现的差异」（沿用 `DATA_AGENT_REDESIGN.md §11` 的写法）

### 8.1 进度

**已交付**（按交付顺序）

| # | 项 | 主要产出 | 说明 |
| --- | --- | --- | --- |
| 1 | P0 横切基建 | `agent_telemetry.py`、`tests/fixtures/golden_questions.py`、`test_chat_bi_golden.py`、`GET /chat-bi/telemetry` | §8.2 |
| 2 | P1.1 权限旁路 | `agent_run_sql_min_role` + `principal_role` 全链路透传 | §8.2 |
| 3 | P1.4 拒绝修复信号 | `SqlRejection.hint`（7 类拒绝码全覆盖） | §8.2 |
| 4 | P1.2 `find_join_path` | `semantic_navigator.py`；`RelView` 带 JOIN 键；规则与证明器共用 | §8.3 |
| 5 | P1.3 `profile_values` | `column_profiler.py`；顺带修 F3 一处既有误拒 | §8.4 |
| 6 | P3 口径编译器 | `metric_compiler.py`（metric 类）；`caliber_trace` 接管口径卡 | §8.5 |
| 7 | P2 上下文架构 | `domain_semantic_card.py`、`tool_result_compaction.py`；prompt −71% | §8.6 |
| 8 | P4.5 双次 LLM 调用 | 收尾轮直接用 `msg.content`；`avg_llm_calls` 3.5 → 2.6 | §8.8 |
| 9 | P4.3+4.4 自愈 + 预算 | 校验不过先重写一次；工具轮 6→8 且与自愈预算分离 | §8.8 |
| 10 | P4.1 澄清反问 | `ask_clarification` 工具 + 可点击候选项（前端） | §8.8 |
| 11 | P1.5 语义检索 | `semantic_search.py` + 索引表/迁移 + 发布钩子；混合检索 | §8.9 |
| 12 | P3.5 tag / rule 编译 | 标签→分桶分布、规则→违规统计；顺带修证明器对派生分组键的误拒 | §8.10 |
| 13 | 大本体 fixture | `tests/fixtures/large_ontology.py` + 上下文规模基线 | §8.11 |
| 14 | P4.2 检索子 agent | `retrieval_agent.py` + `locate_entities`；隔离比 28× | §8.11 |

**待办**：无。五期规划项全部交付。

### 8.1.1 Agent 工具目录现状（12 个）

| 工具 | 期 | 性质 |
| --- | --- | --- |
| `search_objects` / `search_logics` | V1+**P1.5** | 检索：ILIKE 优先 + **向量补召回**（同义词） |
| `search_relations` | V1 | 检索（ILIKE；关系不入语义索引，理由见 §8.9） |
| `get_object` / `get_logic` / `get_domain_overview` | V1 | 取详 |
| **`find_join_path`** | P1.2 | **语义能力**：关系图上出关联路径 + ON + 扇出 |
| **`profile_values`** | P1.3 | **语义能力**：字段真实取值分布 |
| **`compile_metric`** | P3 + **P3.5** | **语义能力**：指标/标签/规则 → 确定性 SQL + 口径轨迹 |
| **`ask_clarification`** | P4.1 | 缺口只能由用户补齐时反问，而非硬答 |
| **`locate_entities`** | P4.2 | 把检索**整体外包**给子 agent，只回收结论（隔离比 28×） |
| `run_sql` | V1 | 执行（只读 + 权限闸门 + 语义证明） |

起点 7 个全是 CRUD 包装；现在 3 个语义能力工具让语义层参与**事前生成**，
而不只是事后否决——这正是 §0 说的那个转变。

### 8.2 P0 / P1.1 / P1.4 实现说明（与设计的差异）

1. **未引入 `agent_arch` 开关。** 原设计要一个 v1/v2 灰度位，但 P0/P1.1/P1.4 都是
   **严格增强**而非另一套架构：P1.4 的 `hint` 纯追加、P0 只加计数器。
   一个没有消费者的开关就是死配置。改为按具体语义命名的
   **`agent_run_sql_min_role`（默认 `publisher`）**——它同时承担 P1.1 的回滚职责
   （降为 `editor` 即回到改造前行为）。真正需要双路径时（P2 的上下文架构）再引入灰度位。

2. **权限用「工具粒度降级」而非「端点粒度拦截」。** 没在 `auth.py` 的 `_ROLE_OVERRIDES`
   给 `/chat-bi/ask` 加 publisher 门槛——那会让 editor 连问都不能问，而检索类工具对
   editor 本是合法的。改为在 `_dispatch_run_sql` 里判角色，不足则降级为「仅建议 SQL」
   （`is_error=False`，不污染接地判定）。这也是 P3/P4 引入更多工具时的正确基线。

3. **顺带修了一处两条路径不一致**：非流式 `ask()` 在拒答时用 `_ungrounded_refusal`
   整体覆盖 payload，把工具轨迹 `steps` 一起丢了；而 `ask_stream()` 明确用
   `_prev_steps` 保留（注释写着要让用户看到「做了什么才拒答」）。现已对齐——
   golden 用例 `guard_unknown_table_hint` 正是撞在这上面才暴露出来的。

4. **`OntologyProjection` 新增 `partners_of()`**：回答「那它能和谁 JOIN」。
   P1.2 的 `find_join_path` 会直接复用它做图遍历的邻接查询。

5. **golden set 起步 10 用例 / 17 个断言测试**，不是设计里写的每域 40 题。
   先把**harness 建对**（stub、别名解析、种子本体、遥测对照）比堆用例重要；
   用例集设计成追加式，后续每修一个线上问题就补一条。

### 8.3 P1.2 实现说明

1. **JOIN 键落进了投影层，不是导航器私有。** `RelView` 新增 `src_key` / `tgt_key` /
   `bridge_obj`，由 `build_projection` 从 `source_evidence` 推出并**对投影校验存在性**——
   推出来的列名若不是已发布属性就置 None。宁可说「有关系但给不出 ON」，也不能吐一个
   证明器随后会以 `unknown_column` 拒掉的 ON，那只会让 Agent 空转。

2. **规则真的只有一份**（设计里说「复用」，这里落成了共享实现）：
   - `primary_key_name` / `foreign_key_names` 提到 `ontology_projection`，
     `warehouse_generator` 改为调用它们——**导航器给的 ON 与物化建的外键指向同一列**。
     语义类型判定仍留在各自侧（物化按原始字符串精确匹配 `identifier`，投影走归一别名），
     所以既有物化产物不变。
   - `_other_is_many` 从 `sql_soundness` 移到 `ontology_projection.other_is_many`，
     证明器与导航器共用同一份多重性换算。`test_navigator_sql_hint_passes_the_prover`
     和 `test_navigator_fanout_agrees_with_prover` 就是钉住这条不变式的测试。

3. **扇出是「相对谁」的**。`measure_object` 参数决定判定视角：订单金额按客户汇总安全
   （N:1），客户数按订单展开就会重复计数（1:N）。默认取起点对象。

4. **接进了 P1.4 的 `undeclared_join` 提示**：臆造 A↔B 的 JOIN 被拒时，提示里直接带上
   真正的多跳路径（「订单和区域没直接关系，但可经客户」）。导航器故障不拖垮证明器——
   兜底退回对端清单。

5. **顺带补上 F3↔F4 的一处断裂**：`SqlCertificate.tables/columns` 原本被
   `_prove_sql_or_reject` 丢弃。于是模型写了一条**被证明合法**的 SQL、再在正文里解释它
   引用的字段，会因事实账本里没有该字段而被 F4 判成幻觉——自己证过的东西反过来拒自己
   （golden 用例 `query_join_via_navigator` 撞出来的）。证书结论现随 `run_sql` 结果回传
   并入账；它是证明器的结论而非模型的主张，入账不削弱 F4。

6. **golden 基线必须钉死数据源**。`_resolve_domain_data_source` 没有数据域绑定，会捞到
   全库任意一个 `DataSource`；不钉的话，别的测试建了数据源，golden 里的 `run_sql` 就会
   真去执行，基线随测试顺序漂移。**这背后是个真问题**——A 域的问题可能打到 B 域的库，
   已单开任务跟进，不在 P1.2 范围内。

### 8.4 P1.3 实现说明

1. **策略由语义类型分派**，不是一刀切采样：类别/标识/文本 → TopN 取值 + 频次 + 去重数；
   度量 → min/max/avg；时间 → 区间。`TECHNICAL` 与 `UNKNOWN` **一律不画像**——
   前者的语义本就是「默认不入业务查询」，后者拿不准，与全链路「拿不准即禁止」一致。

2. **生成的 SQL 由我们负责正确性**（不同于 Agent 手写的 SQL）：用 sqlglot 按目标方言渲染
   并强制加标识符引号。`order` 是保留字，不加引号直接语法错——测试里的物理表就叫 `order`，
   专门钉这一点。sqlglot 不认识的后端（如 `kyuubi`）走兼容方言（hive）。

3. **画像 SQL 过自己的证明器**（P3 编译器将依赖同一模式）。这一步当场抓到两个真问题：
   - 自己的 bug：`ORDER BY <输出别名>` 被判臆造字段 → 改为按聚合表达式排序；
   - **F3 的既有误拒**（见下）。

4. **权限与 run_sql 同闸门**。画像读的是真实数据，与 `run_sql` 是同一类暴露，
   故同样受 `agent_run_sql_min_role` 约束；越权与无数据源都是**降级**（`available=false`
   + 说明）而非报错，且提示里明说「不得据此猜测字面量」。

5. **输出别名统一加 `__p_` 前缀 + 按列序读取结果**。`_apply_mapping` 会把本体属性名整词
   替换成物理列名，别名若恰好与某个属性同名会被一起改写；前缀保证不可能命中，
   按列序读则彻底不依赖别名。

6. **缓存按 `scope_key`（ontology_id + 数据源 id）分区**，默认 900s。本体重发或换数据源，
   同名字段的取值分布就不是同一回事，必须落在不同键上。

7. **DataHub 采样值回退未做**（原设计表里列为可选）。它需要在同步的工具分发路径里发异步
   网络请求，且拿到的是建模时的陈旧样本。画像的本质就是要读数据，无数据源时的降级是
   **固有的**，不是偷工——故如实降级，不用陈旧样本冒充。

#### 顺带修掉 F3 的一处既有误拒（影响面不小）

`sqlglot` 的 `qualify` 会把 `ORDER BY COUNT(*)` 归一成 `ORDER BY <输出别名>`，于是别名以
**裸列**形态出现，证明器把它当臆造字段拒掉。也就是说——

```sql
SELECT status, COUNT(*) AS cnt FROM "order" GROUP BY status ORDER BY COUNT(*) DESC
```

**「按 X 降序取 TopN」这类最常见的查询，此前一直被 F3 误拒**。

修法：只豁免**显式 `AS x` 定义的**别名，且只在 `ORDER BY / HAVING / GROUP BY` 里豁免。
两处收紧都必要——第一版只按「输出名」豁免，`SELECT fake_col FROM order` 的输出名也叫
`fake_col`，臆造字段就能靠「自己给自己当别名」蒙混过关（既有测试当场拒收）。
别名的**定义式**仍逐列证明，`SELECT ghost AS x ... ORDER BY x` 照旧在 `ghost` 处被拒。

### 8.5 P3 实现说明

1. **自证不变式当场生效**。`_certify` 对每条编译产物跑 `prove_sql_sound`，
   失败即抛 `uncertified_output`（带拒绝码）。测试 `test_every_compiled_output_is_certified`
   对 6 种参数组合各证一遍。这不是走过场——P1.3 用同一模式时当场抓出了两个 bug。

2. **JOIN 全部来自 P1.2 导航器**。为此给 `JoinHop` 加了 `from_key` / `to_key`：
   `on` 是给人看的渲染串，编译器需要的是结构化的列名，不能去解析字符串。

3. **字面量零拼接**。全部走 `exp.Literal` 构造，注入串只会成为一个字符串常量。
   对应测试的判据也修正过——「SQL 文本里不含 DROP TABLE」是**错的**判据
   （转义后的字面量当然含这些字符）；正确判据是「整条 SQL 只解析出一条 SELECT，
   且注入串原样落在一个 Literal 节点里」。

4. **拿不准就报错，绝不猜**：
   - 口径只有文字摘要、无 `expression_json` → `no_expression`（不许照着摘要猜 SQL）；
   - 引用的字段已下线 → `unresolved_property`（宁可整条失败，也不能悄悄换个字段算出一个数）；
   - 有两个时间字段却只给了 `grain` → `ambiguous_time_property`，要求指定；
   - 维度对象与主对象无通路 → `unjoinable`；会扇出 → `fanout_risk` + 安全聚合建议。

5. **`caliber_trace` 接管口径卡**。`_steps_to_caliber` 新增 `compiled` 参数，
   编译器轨迹**排在最前且优先**——它是本体确定性生成的**契约**，
   而按 steps 反推的卡片只是事后猜测（「调了 get_object，那大概是个对象口径」）。

6. **`object_labels`（标识符 → 中文显示名）随编译结果返回**。没有它，模型要么拿
   `customer` 这种技术名作答，要么自己译一个「客户」——后者会被 F4 判成未接地实体
   （golden 用例 `metric_compiled_with_dimension` 就是这么暴露的）。

7. **SQL 收割优先级**：`run_sql` 实际提交的 > 口径编译产物 > 正文围栏块。
   编译产物排在正文之前——它已自证，比模型正文里写的可信。

8. **`tag` / `rule` 当期明确报错**，不做半吊子编译——已由 P3.5 补齐，见 §8.10。

### 8.6 P2 实现说明

**prompt 2590 → 741 字符（10 行）**，达成 §0 立的 ≤10 行目标。删掉的每一条都由构造顶上：

| 删掉的铁律 | 现在由谁保证 |
| --- | --- |
| 1b「概览题必须先调 get_domain_overview」+ 域骨架描述 | 域语义卡常驻 system（P2.1） |
| 1c 整段「不得把样本当全集」 | 截断时键名即 `sample` + `sample_note` + facets（P2.3） |
| 「检索关键词优先用中文（本体以中文命名）」 | 语义卡的 `naming_note`，**从真实数据观察**而非假设 |
| 0 与 4b 的「不得编造 / 不得展开到未检索的字段」 | `FactLedger` + `answer_verifier` 断言级核验（F4） |
| 「结果过长已截断」相关叮嘱 | 语义降级压缩保证回灌永远是合法 JSON（P2.2） |

保留下来的全是**工具选择策略**——「该先调谁」这类判断，守卫拦得住结果却教不会顺序。

1. **语义卡缓存键含 `(ontology_id, version, published_at)`**：重新发布必然改变其一，
   缓存自动失效。不用「记得调 reset_cache」的约定——那种约定迟早会漏。

2. **卡只统计已发布内容**。`get_ontology_grouped_graph` 会混入草稿，故语义卡自己按
   已发布子图调 `_compute_cluster_partition`。卡上写的每一条都必须是 Agent 真能检索到的。

3. **卡上的名字入事实账本**。它们是服务端从已发布本体算出来的，与 `get_domain_overview`
   同源同可信。不入账的话，模型引用卡上看到的核心对象名会被 F4 判成幻觉——
   我们把事实塞进它的上下文，又因为它用了而拒答，说不过去。

4. **压缩是语义降级阶梯，不是字符截断**：完整 → 丢长文本 → 丢次要字段 → 列表采样
   （就地标注 `_total` / `_is_sample`）→ 标量摘要。
   **不变式：回灌给模型的永远是合法 JSON**，测试对 5 档预算逐一 `json.loads` 验证。

5. **`_search_envelope` 用键名承载语义**：未截断叫 `items`（这就是全部），
   截断叫 `sample` + `sample_note` + `sample_facets`。这是个**破坏性契约变更**，
   `test_search_reports_true_total_not_page_size` 已相应更新——原先无论如何都叫 `items`，
   于是 140 条里的 8 条被当成完整清单，只能靠一整段铁律去堵。

6. **一处原计划的收益没有兑现**：原文写语义卡能「省掉一次 `get_domain_overview` 调用」。
   实际没有——接地判定仍要求至少调过一次工具，卡本身不满足。
   把卡改成可独立作答会削弱「不许凭上下文空答」这道guard，不值得。
   卡的真实收益是**导航**（选对工具与关键词、知道有哪些现成指标）与**prompt 瘦身**，
   不是省步数。§8.12 的基线里 `avg_steps` 未降，与此一致。

**基线快照**（P1.1/P1.4 落地后，10 用例）：

```json
{ "runs": 10, "refused_runs": 3, "refusal_rate": 0.3,
  "refuse_kinds": { "ungrounded": 2, "unverified": 1 },
  "avg_steps": 1.5, "avg_llm_calls": 3.5,
  "rejection_codes": { "unknown_column": 1, "illegal_aggregation": 1, "unknown_table": 1 },
  "run_sql_outcomes": { "suggest_only": 2, "rejected": 3 } }
```

> `avg_llm_calls: 3.5` 里含 §7.5 那笔冗余——每次收尾作答都多打一次全量请求。
> P4.5 修完这个数应显著下降，golden 用例 `overview_objects` 的
> `expect_llm_calls=3` 届时要改成 2。

### 8.8 P4 实现说明

1. **P4.5 是纯粹的浪费，删掉即可**。收尾轮 `msg.content` 本就是最终答案，
   原实现把它丢掉再发一次全上下文请求"流式"重生成，而那次的 token 同样被 buffer、
   最终仍由假打字机吐出——白付一整轮 prefill + 生成。
   保留一条兜底：少数模型返回「空 content + 无 tool_calls」，此时才补一轮
   （golden 用例 `overview_empty_final_content` 专门覆盖）。
   **`avg_llm_calls` 3.5 → 2.6。**

2. **P4.3 自愈指令要同时给「错在哪」和「能说什么」**。只说「你错了」模型多半换个说法
   再错一次；`FactLedger.provable_names()` 把本轮可引用的实体一并给它，边界才清楚。
   这与 P1.4 给拒绝加修复信号是同一条思路——**守卫要当教练**。
   基线：`repairs: 3, repairs_succeeded: 1`。救不回来仍拒答——放宽守卫比多拒一次更糟。

3. **P4.4 预算分离而非简单加大**。工具轮 6 → 8，自愈轮独立计 1，不占工具预算。
   原来 6 步一刀切，「检索 → 读细节 → 写 SQL → 被拒 → 改 → 重跑」正好撑满；
   此后又加了三个工具，真被拒一次就没余量了。

4. **P4.1 偏离了原设计，是有意的**。规格写的是**强制**前置规划轮，
   但那会给每个问题固定加一次 LLM 调用——刚把 3.5 压到 2.6，不该这样还回去；
   CC 的 plan mode 也是**opt-in** 而非强制。
   改用工具形态 `ask_clarification`：简单问题零开销，模型只在真有缺口时反问。
   规划轮的独有价值本就是「缺口识别 → 主动反问」，这条被完整保留；
   而结构化 plan 的其余部分（metric/dimensions/filters/grain）已被 `compile_metric`
   的参数覆盖，再做一遍是重复。
   > **澄清 ≠ 拒答**：拒答是「答不了」，澄清是「先确认再答」，
   > 两者对用户的下一步指引完全不同，故独立计数（`clarifications`），不进拒答率。
   > 前端把候选项渲染成可点击追问，用户一步接上而不必重打一遍。

5. **P4.2 检索子 agent 本期未做**。它的收益是「734 对象里定位子图的 token 垃圾不污染
   主上下文」——只在超大本体上显现，而 golden 用例是小本体，做了也验证不了，
   反而引入一条无法回归的复杂路径。等有真实大域的问答样本再做。

### 8.9 P1.5 实现说明

**与原设计的最大偏离：没用 pgvector。** 三条具体理由，不是嫌麻烦：

1. 它要装 Postgres 扩展——一个本项目没有的**部署前置条件**；
2. **测试跑 SQLite**，pgvector 路径在 CI 里覆盖不到。P0 整期的投入就是「每期都要可回归」，
   这时候引入一条测不到的路径是自相矛盾；
3. **规模用不上**：一个域的可检索实体是百到千级，纯 Python 暴力余弦毫秒级完成。

改为：向量存普通表（JSON 文本，两种库通用）+ 进程内按 `(ontology_id, version)` 缓存
+ 暴力点积。规模真上来再换 pgvector，那时索引接口不用动。

**其余取舍**

1. **只索引对象与业务逻辑，不索引关系与字段**。关系名是从两端派生的公式化名称
   （「订单归属客户」），语义检索收益低，而「这两个对象怎么连」这个真实需求已由
   `find_join_path`（P1.2）覆盖；字段量级大一个数量级，而 Agent 的路径本就是
   先定位对象、再 `get_object` 看字段。这把索引量压在百到千级——正是上面第 3 条的前提。

2. **向量截断到 256 维并 L2 归一化**。归一化后余弦退化成点积，省掉每条的模长计算；
   截断是 Matryoshka 式的，前若干维已承载主要语义，检索快数倍而召回基本无损。

3. **字面命中永远排在语义召回之前**（`merge_hits`）。用户打出的词是最强意图信号，
   不该被一个分数更高的近义实体挤掉；向量只负责补 ILIKE 够不到的表达。
   语义补进来的条目标 `matched_by: "semantic"`，模型据此措辞更准。

4. **默认关闭，降级是常态而非异常**。`agent_embedding_model` 留空即不启用——
   未配置嵌入服务、索引未建、调用失败，一律退回纯 ILIKE，功能不受影响。
   建索引失败也**绝不阻断发布**：发布是不可逆的治理动作，不能因为一个检索增强而失败。

5. **索引按本体版本存取**。发布使 `version` +1，旧索引自然取不到，不会召回陈旧实体——
   与语义卡（P2.1）用的是同一套失效思路，都不依赖「记得清缓存」的约定。

6. **测试注入确定性假嵌入**：同义词映射到同一概念向量，于是
   `test_synonym_recall_that_ilike_cannot_do` 能证明「往来单位 → 客户」这条链路打通，
   而不依赖真模型。嵌入模型本身的质量属 `@pytest.mark.live` 范畴。

### 8.10 P3.5 实现说明（tag / rule 编译）

三类逻辑**只在「聚合什么」这一步分叉**，维度/过滤/JOIN/自证全程共用——
它们的产出都是聚合查询，区别只是被聚合的东西：

| 类型 | 编译成 | 为什么是这个形状 |
| --- | --- | --- |
| `metric` | `SUM/COUNT/AVG(度量)` | 原有 |
| `tag` | `CASE WHEN … END AS 标签, COUNT(*) … GROUP BY CASE …` | **分布查询**而非逐行打标：「高价值客户有多少」「各分层各占多少」才是问数场景要的；逐行明细本就该用 `run_sql` |
| `rule` | `COUNT(*) AS violations … WHERE NOT (条件)` | 规则是「应当成立」的断言，直接查它没信息量；**违规行**才有 |

**取舍**

1. **`_involved_objects` 改为扫整个 `body`**。metric 的 `filter`、tag 的 `cases`、
   rule 的 `condition` 形状各不相同，逐个特判迟早漏一个——漏了就生成一条缺 JOIN 的 SQL，
   再被证明器以 `unknown_table` 拒掉，白跑一轮。

2. **标签值缺失 → 明确报错**（`incomplete_tag`）。形式化没抽出标签时 AST 里写的是
   `{"value": null}`，编出来只是一列 NULL。这里有个坑：**「解析出了字面量」不等于
   「有标签值」**——`{"value": null}` 会被解析成一个合法的 NULL 字面量，
   判据必须看值本身（`_has_label`），这一条是被测试逼出来的。

3. 补齐 `not_in` 比较算子（AST 契约里本就有，原实现漏了）。

#### 顺带修掉证明器第二处误拒

`_group_by_columns` 原先**递归**收集 GROUP BY 下的所有列节点，于是
`GROUP BY CASE WHEN amount >= 1000 THEN '高价值' ELSE '普通' END` 里的 `amount`
被当成分组键，以 `illegal_group_by` 拒掉——**整类标签口径都编不出来**。

判据应是「分组键**是不是**这一列」，而非「这一列有没有出现在分组键里」：
按度量原值分组确实是口径错误（每个不同金额一组），但按度量**分桶**完全合法。
改为只检查直接的裸列分组键。`test_tag_over_measure_is_not_illegal_group_by`
同时钉住两边：分桶放行、原值仍拒。

> 这是 P1.3 之后**第二次**由「编译产物必须过自己的证明器」这条不变式抓出的既有误拒。
> 两次都不是新代码的 bug，而是证明器过严——这条自证的价值比预想的高。

### 8.11 大本体 fixture 与 P4.2 检索子 agent

**先补 fixture 再动手，是这一项能做成的原因。** P4.2 原本判为「收益只在超大本体上显现、
当前无法验证，故不做」。补上 `tests/fixtures/large_ontology.py` 后，收益变成了可测的数：

| | 数值 |
| --- | --- |
| 检索序列**不用**子 agent 时灌进主上下文 | **7984 字符 / 4 次工具调用**（`test_locating_entities_costs_real_context`） |
| 同样的检索**用**子 agent | 隔离 **5428 字符**，交回主上下文 **191 字符** |
| 隔离比 | **28.4×** |
| 代价 | **+4 次 LLM 调用** |

**这是一笔明确的交换，不是纯赚**：用更多 LLM 调用换更小的主上下文。
所以遥测把 `subagent_llm_calls` 与主循环的 `llm_calls` **分开计**——
合在一起只会看到调用数涨了，看不到主上下文省了多少。
prompt 里也写明了适用边界：**已经知道要找什么就直接 `search_*`，别绕这一道**。

**fixture 的设计取舍**

1. **结构要真实**，不是造 N 个孤立对象：6 个业务板块（板块内链式密集、板块间稀疏）
   + 3 个高连通枢纽（公司/文档类型/币种这类到处被引用的公共维度）。
   这正是 `community_detection` 与 `find_join_path` 真实会遇到的形状。
2. **必须确定性**。第一版用内置 `hash(name)` 分配枢纽——Python 对字符串的 hash
   **逐进程随机**，基线就不可对照了。改用 `crc32`。
3. **要带外键列**。第一版只给通用字段，于是跨板块寻路能找到路径却推不出 ON——
   真实 ERP 对象是有 `<目标>_id` 的，没有就测不出多跳寻路，而那正是大本体才有的场景。
4. 参数化：默认 75 对象（跑得快），调 `objects_per_segment=60` 即 363 对象，
   接近真实 ERP 域量级。

**子 agent 的职责边界是硬的**：它只拿 `search_* / get_object` 四个检索工具，
拿不到 `run_sql` / `compile_metric` / `profile_values`。
职责一旦放宽，隔离上下文就会重新变成一个什么都往里塞的主上下文。
越权调用**明确回绝**而非静默忽略——静默忽略会让模型一直重试。

### 8.12 基线演进（每期对照）

用例数随每期新增而增长，故看**绝对值**而非比率：

| 交付后 | 用例 | 拒答数 | 拒绝码分布 | avg_steps | avg_llm_calls |
| --- | --- | --- | --- | --- | --- |
| P0/P1.1/P1.4 | 10 | **3** | unknown_column 1 / illegal_agg 1 / unknown_table 1 | 1.50 | 3.50 |
| P1.2 | 12 | **3** | 同上 | 1.50 | 3.50 |
| P1.3 | 13 | **3** | 同上 | 1.54 | 3.54 |
| P3 | 15 | **3** | 同上 | 1.53 | 3.53 |
| P2 | 16 | **3** | 同上 | 1.50 | 3.50 |
| P4 | 20 | 4 ⚠️ | 同上 | 1.45 | **2.60** |

> prompt 字符数只在两个时点实测过：**P3 交付后 2590 → P2 交付后 741**。
> 中间各期未逐一测量，故不列（每期加一条工具纪律，趋势是涨的）。
>
> ⚠️ P4 的拒答数从 3 变 4，**不是守卫变严**：新增的
> `repair_exhausted_still_refuses` 用例本身就是设计来验证「自愈救不回来仍须拒答」的。
> 原有 3 条拒答用例的行为一字未变。

三条结论：

1. **绝对拒答数五期恒为 3**，且始终是那三条设计上就该拒的用例
   （无工具调用 / 幻觉实体 / 查不存在的对象）。
   三个新工具 + 证书回灌 + 语义卡 + 两次破坏性契约变更，
   **没有一次引入新的拒绝面，也没有一次放松守卫**——这是每期最该盯的信号。
2. **拒绝码分布五期一字未变**：新增能力没有制造新的失败模式。
3. **prompt 前四期在涨（每加一个工具加一条纪律），P2 一次性打回 741**（−71%）。
   这条曲线的反转就是 §0 那句「能由构造保证的不写进 prompt」的兑现。

`avg_llm_calls` 前五期恒为 3.5，**P4.5 一次性降到 2.60（−26%）**——那笔冗余正是
每次收尾多打的一整轮请求。golden 用例 `overview_objects` 的 `expect_llm_calls`
已随之从 3 改为 2。

P4 后新增两个观测量：`repairs: 3 / repairs_succeeded: 1`（自愈把 1 个本会被拒的答案
救了回来）、`clarifications: 1`（澄清反问，独立于拒答率计数）。

---

## 9. 附录：交付顺序回顾

**第一批 P0 + P1.1 + P1.4** 的选择依据（已验证有效）：

- **P0** 给出基线报表，后面所有判断有依据——否则「JOIN 拒绝率降了多少」无从谈起。
  事后看这一步的价值被低估了：golden set 在 P1.2/P3/P2 各抓到一个真问题
  （非流式拒答丢 steps、F3↔F4 证书断裂、编译结果缺 `object_labels`）。
- **P1.1** 是安全问题，几十行，不该拖。
- **P1.4** 改动量最小、见效最快：拒绝结果本就回灌给模型，加候选清单后模型立刻能自修。

**后续顺序的经验**：P1.3 与 P3 都采用「生成的 SQL 必须过自己的证明器」这一模式，
两次都**当场抓到 bug**（含 F3 一处影响面不小的既有误拒）。
凡是我们自己生成 SQL 的地方，都应该加这道自证。
