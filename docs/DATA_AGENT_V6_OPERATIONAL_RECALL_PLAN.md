# Data Agent V6 计划：运行记录可问答（Operational Recall）

> 状态：🟡 **P0/P1 已落地，P2（两类运行记录）已落地**——其余运行记录族仍按需扩展。
> 日期：2026-08-28
> 承接 [DATA_AGENT_V5_OPTIMIZATION.md](./DATA_AGENT_V5_OPTIMIZATION.md)（工具收窄 38→9）与 [DATA_AGENT_V5_PLAN.md](./DATA_AGENT_V5_PLAN.md)。
> 主战场：`backend/app/services/chat_bi.py`、`chat_bi_tool_schemas.py`、`chat_bi_skills.py`、
> `chat_bi_blocks.py`、`ontology_ladder.py`，新增 `backend/app/services/ops_records.py`。

---

## 0. 一句话结论

系统把「发生过什么」记得很全——**20+ 张运行记录表、40+ 个只读 REST 端点、十几个已经写好的读模型**——
但 Data Agent 一个都读不到。用户在界面上看得见的东西，在对话里问不出来。

**缺的不是记录，是读侧接线。** V6 只做一件事：把既有读模型接到 Agent 工具面上，
并修掉路由 / 意图 / 接地三道机制——它们现在会把这类问题引去建任务、或者干脆放模型自由发挥。

> **总纲**：不新建表、不新建判定口径、**全部只读**。复用 `object_landing` / `agent_pipeline` /
> `draft_task_service` / `provenance_service` / `governance_standard` 等既有服务，
> 一行判定逻辑都不重写——第二份口径就是下一个 bug。

---

## 1. 触发案例：一个问题打穿五道机制

用户问：**「这个本体物化到哪个数据源？」**

| 环节 | 实际发生 | 锚点 |
| --- | --- | --- |
| 意图分类 | 不含取数/结构标记 → 判 `general` | `chat_bi.py:5560-5577` |
| 技能路由 | 「物化」命中 task 标记（第 1 优先级）→ 进**建任务**车道 | `chat_bi.py:5597-5599` |
| 工具收窄 | task 白名单只有建任务工具，`get_lineage`/`get_domain_overview` 都被排除 | `chat_bi_tool_schemas.py:1790-1796` |
| 上下文 | 深加载包塞进 `materialization.engines=["doris"]`，提示词称其为「物化引擎…可直接据此作答」 | `ontology_ladder.py:428-453`、`chat_bi.py:5825-5827` |
| 接地校验 | `general` 且未点名实体 → `general_waives=True`，**豁免接地** | `chat_bi.py:6512-6514` |

结果不是拒答，是**流畅地答错**：模型照着"物化引擎=doris"编一句"物化在默认 Doris 数仓"，
与该本体到底有没有物化、落在哪个 Doris 实例、哪张表，全都无关。

真正的答案一直在库里：`OntologyWarehouseDeployment.doris_datasource_id`（`models/warehouse.py:79`）、
`ObjectLanding.ods_table/serving_table`（`services/object_landing.py:52-65`）。前端的
`components/ObjectLanding.tsx` 天天在渲染它。

---

## 2. 五类失效（根因分解）

补几个工具只能治第 1 类。V6 必须五类同治，否则新工具会重蹈 `object_landing` 的覆辙——
写好了、没人调。

| # | 失效模式 | 说明 | 证据锚点 |
| --- | --- | --- | --- |
| **F1** | **无读工具** | 记录在库、读模型写好了，但工具面上没有入口 | `object_landing.py` 的消费方全是 REST/前端，`chat_bi*` **零引用** |
| **F2** | **最后一米被剥** | 数据已经在手上，白名单投影把它丢了 | `_compact_object_detail`（`chat_bi.py:1243-1268`）丢 `landing`；`_project`（`:5170-5179`）丢 `spec` |
| **F3** | **技能收窄够不着** | V5 把工具 38→5~10，但没开「运行记录」车道 | `SKILL_TOOL_ALLOWLIST`（`chat_bi_tool_schemas.py:1764-1801`）六族无一含运行记录读工具；`DEFAULT_TOOL_ALLOWLIST`（`:1804`）同样没有 |
| **F4** | **路由把读拧成写** | 「物化/同步/任务/状态」一律进 task 建任务车道 | `_auto_select_skill`（`chat_bi.py:5596-5599`），overlay 通篇教建任务（`chat_bi_skills.py:165-204`） |
| **F5** | **假信息源** | 契约冒充落点，且提示词授权「可直接据此作答」 | `ontology_ladder.py:428-453` + `chat_bi.py:5825-5827` |

还有一条**会卡死任何新工具**的硬约束：

> **F0（前置约束）**：新读工具必须在 `_ledger_register`（`chat_bi.py:1558-1640`）登记它返回的事实名，
> 否则答案里复述的表名 / 数据源名 / 版本号 / DAG id 会被 F4 断言校验判成幻觉，**整条回答被拒**。
> 这正是 `propose_*` 工具踩过的坑。

---

## 3. 现状盘点：11 个问题族 × Agent 可达性

| 族 | 典型问题 | 既有读模型 | 既有 REST | Agent 现状 |
| --- | --- | --- | --- | --- |
| **A 落点/物化** | 这个对象落到哪张表了？表建了吗？能查了吗？ | `object_landing.py:159/237/242` | 无独立端点，仅内嵌 `/api/object-types*` 的 `landing` 字段 | ❌ 完全不可达 |
| **B 任务执行** | 那个任务跑完了吗？失败在哪？下次什么时候跑？ | `agent_pipeline.py`、`task_pipeline.py`、`flink_health.py` | `/api/agents/artifacts[/{id}]`、`/api/agents/pipelines*`、`/api/warehouse/materialize/{aid}/status` | ⚠️ `get_task_status` 只给 7 字段、**只覆盖本会话产出**；REST 的 `_to_out`（`api/agents.py:105-121`）返回完整 `spec`+`validation_report`+`execution_receipt`+`confirmed_by/at` |
| **C 数据源/连通** | ERP 库连得上吗？依赖组件装好了吗？ | `dependency_service.py`、`source_datasource.py` | `/api/data-sources*`、`/api/settings/dependencies*`（含 `deploy_status`/`deploy_error`/`deploy_log`） | ❌ 仅 `get_task_options` 顺带列 Doris 候选 |
| **D 本体版本/发布** | 这个域发到第几版？和上一版差什么？ | `version_diff.py`、`publish.py` | `/api/ontologies/{id}/versions`、`/diff`、`/snapshot`、`/publish-preflight` | ❌ |
| **E 草稿/复核** | 上次生成改了什么？还有哪些冲突？哪些表没建模？ | `draft_task_service.py:1202/1217/1237`、`provenance_service.py:222`、`unmodeled_tables.py` | `/api/domains/{id}/tasks[/logs\|/merge-report]`、`/progress`、`/unmodeled-tables`、`/api/ontologies/{id}/conflicts` | ❌ |
| **F 决策/审计** | 这个任务当初谁拍的板？改过几轮？哪一环没走？ | `chat_bi_ledger.py`（`list_decisions`/`build_closure`） | `/api/chat-bi/decisions`、`/conversations/{id}/decisions\|closure` | ❌ |
| **G 血缘/影响** | 这张表谁产出的？改了影响谁？ | `pipeline_lineage.py`、`lineage_emitter.py` | `/api/agents/pipelines/{id}/lineage`、`/api/data-apps/{id}/lineage` | ⚠️ `get_lineage` 只给**已发布本体关系**邻域，不含物理表血缘 |
| **H 规约合规** | 当前用哪版规约？历史产物还合规吗？ | `governance_standard.py` | `/api/governance/standard[/history]`、`/relint` | ⚠️ 有 `lint_against_standard`，无历史/生效版本查询 |
| **I 建模工单** | 这个需求做到哪步了？规格失效了吗？ | `modeling_case.py` | `/api/modeling-cases*`、`/specs/{kind}/stale` | ✅ 已有 `get_modeling_case` |
| **J 数据应用** | 这个看板发了几版？ | `data_app.py` | `/api/data-apps[/versions\|/lineage\|/share]` | ❌ |
| **K 生产割接** | 割接到第几步？谁批的？观察窗还剩多久？ | `warehouse_migration.py` | `/api/warehouse/migrations*` | ❌ |

**11 族里 7 族完全不可达、3 族残缺、1 族完整。**

---

## 4. 设计原则

1. **只读。** 新增的全是读工具。任何写仍走六环确认（见 `six-ring-task-confirmation` 约定），一条不破。
2. **不造第三份口径。** 两套落点登记（`IngestionContract` 走同步、`WarehouseObjectProjection` 走物化/清洗）
   并存是历史事实，`object_landing.py` 文件头已明写。新读模型一律复用 `bulk_object_landings`，
   不新建表、不重写 state 判定。
3. **权威分层要显式。** 执行门槛权威 = `GovernanceArtifact.status`；流程权威 = `ModelingCase.stage`；
   观察/审计层 = `ChatBiDecisionRecord`（append-only）+ `EntityChangeLog` + `VersionRecord`。
   每条返回的事实必须带 `source` 标明取自哪一层——否则会答出两个互相矛盾的"真相"。
   （典型：制品的 `confirmed_by/at` 在 `agent_pipeline.edit()` 改 spec 时会被清空，
   「当初谁拍的板」**只能**查决策账本，见 `chat_bi_ledger.py:8-11`。）
4. **工具数量守住 V5 的收益。** V5 把 38→9 是实测有效的。V6 只加 **2 个**读工具 + 1 个技能，
   不做「一族一工具」（那是 13 个，直接把 V5 的账吃回去）。
5. **事实必须入账本。** 见 F0。每族显式声明 `ledger_fields`。
6. **宁可拒答，不可编造。** 运维问题接地要求与取数/结构问题同级，不走 `general` 豁免。

---

## 5. 方案

### 5.1 核心：`app/services/ops_records.py` —— 运行记录读模型注册表

一个薄注册层，把 11 族的既有服务统一成同一个信封。**它自己不查库，只调既有服务。**

```python
@dataclass(frozen=True)
class RecordAnswer:
    family: str                    # "landing" / "task_run" / ...
    subject: str | None            # 问的是谁（对象显示名 / 任务名 / 域名）
    facts: list[dict]              # [{"label": "ODS 表", "value": "ods.ods_erp_customer"}]
    items: list[dict]              # 列表型（历史记录 / 清单）
    as_of: datetime | None         # 这份事实的时点——运维答案没有时点等于没有答案
    source: str                    # 权威层，如 "GovernanceArtifact.status"
    truncated: bool

@dataclass(frozen=True)
class RecordFamily:
    key: str
    display: str
    answers: str                            # 一句话，进工具 description
    reader: Callable[[Session, dict], RecordAnswer]
    ledger_fields: tuple[str, ...]          # 哪些字段是事实名，供 F4 入账

REGISTRY: dict[str, RecordFamily] = {...}   # 13 个 reader，全部委托既有服务
```

族 → 委托目标（一一对应第 3 节）：

| family | 委托 | source（权威层） |
| --- | --- | --- |
| `landing` | `object_landing.bulk_object_landings` / `bulk_logic_landings` | `IngestionContract` + `WarehouseObjectProjection` |
| `task_run` | `agent_pipeline.list_artifacts/get` + `_live_task_state` | `GovernanceArtifact.status` |
| `pipeline` | `task_pipeline` + `pipeline_lineage` | 各步制品聚合 |
| `datasource` | `data_app` + `dependency_service` | `DataSource.status/tested_at` |
| `ontology_version` | `version_diff` + `publish` | `VersionRecord` |
| `draft_run` | `draft_task_service.list_tasks/get_task_logs/get_progress` | `DraftGenerationTask` |
| `merge_report` | `provenance_service.get_merge_report` | `DraftGenerationTask.merge_report_json` |
| `unmodeled` | `unmodeled_tables` | 派生（排除已建模/已删/平台自造） |
| `conflict` | `provenance_service` | ProvenanceMixin 字段级 |
| `decision` | `chat_bi_ledger.list_decisions/build_closure` | `ChatBiDecisionRecord`（append-only） |
| `standard` | `governance_standard.history/relint` | `GovernanceStandardRecord` |
| `data_app` | `data_app` | `DataAppVersion` |
| `migration` | `warehouse_migration` | `WarehouseMigrationEvidence`（不可变） |

### 5.2 工具面：2 个新读工具

**为什么是 2 个而不是 1 个或 13 个**：`landing` 是最高频且要内联进 `get_object`，值得独立；
其余 12 族低频、按需触发，合成一个带 `family` 枚举的工具，description 里逐族列清"能答什么"。
13 个独立工具会把 V5 的工具收窄收益吃光。

```
get_landing(target_kind, target_id | keyword)
    → 「这个对象/口径落到哪张物理表了、什么状态」
    → 委托 REGISTRY["landing"]

get_ops_record(family, subject_id?, keyword?, limit?)
    → 其余 12 族。family 为枚举，description 逐族一句话
```

### 5.3 技能面：新增 `ops` 技能

`chat_bi_skills.py` 加一族，`SKILL_TOOL_ALLOWLIST` 加一条：

```python
"ops": frozenset({
    "search_objects", "get_object",          # 定位主体
    "get_landing", "get_ops_record",         # 新
    "get_task_status", "get_lineage",        # 复用
    "select_skill",
}),
```

overlay 要点：**这是只读现场勘查模式**——先定位主体（对象/任务/域），再取记录，
答案必须带 `as_of` 与 `source`；**不出任何提案**（要建任务请 `select_skill('task')`）。
`block_types=("markdown", "record", "task_status", "lineage")`。

### 5.4 路由面：新意图 `operational`

**这一步是 V6 成败关键**——不改路由，新工具永远不会被调到。

1. **`_auto_select_skill`（`chat_bi.py:5596`）在 task 之前插入读/写判别**：
   同时命中任务名词（物化/同步/加工/任务）与**读意图词**（到哪了/在哪/哪个/查/看/为什么失败/谁批的/第几版/历史）
   → `ops`；只命中任务名词 + 写意图词（建/创建/帮我做/起草）→ `task`。

2. **新增 `_OPERATIONAL_MARKERS`**，判定顺序 `analytical → operational → structural → general`。
   > ⚠️ **顺序不能动 analytical 优先**：现有注释写明 analytical 赢平局是 fail-open（宁可多给取数、绝不误伤真实取数）。
   > 因此 `_OPERATIONAL_MARKERS` **必须用复合词**（「落到哪」「跑完了吗」「谁批的」「第几版」「部署状态」），
   > 不能收「记录」「状态」「进度」这类裸词——「订单表有多少行记录」会被误判。

3. **`operational` 要求接地**：改 `chat_bi.py:6067` 与 `:6506` 的条件，
   把 `intent in ("structural", "analytical")` 扩为含 `"operational"`。
   代价是没调工具时会拒答——**这是想要的**，比第 1 节那种流畅答错好。

4. **`sql_allowed` 对 `operational` 保持开启**（不同于 `structural`）：
   核对落点常需要 `count(*)` 验证表里到底有没有数。

### 5.5 账本面：`_ledger_register` 登记（F0 前置）

`chat_bi.py:1558` 加两个分支，按 `RecordFamily.ledger_fields` 把返回的
表名 / 数据源名 / DAG id / 版本号 / 主体名 `add_context_name` 入账。
**不做这步，两个新工具上线即被 F4 判幻觉。**

### 5.6 投影面：三处止血（可独立先发）

| # | 位置 | 改动 | 收益 |
| --- | --- | --- | --- |
| S1 | `_compact_object_detail`（`chat_bi.py:1260-1268`） | 透传 `d.get("landing")` | `ObjectTypeDetail` **已经带着** landing（`ontology_query.py:1208-1232` 还做过单对象自愈补查），纯粹最后一米被白名单丢掉。零新增查询 |
| S2 | `_project`（`chat_bi.py:5170-5179`） | 补 spec 白名单字段（`target_datasource_id`/`target_database`/`target_table`/`refresh_cron`）+ 支持 `scope="ontology"\|"all"` | 消掉「只覆盖本会话」的限制，对齐 REST 的 `_to_out` |
| S3 | `_load_materialization`（`ontology_ladder.py:428-453`）+ 提示词（`chat_bi.py:5825-5827`） | 要么并入真实 landing，要么把 key 改名 `materialization_contract`、措辞从「物化引擎」改「物化**配置**」 | **拆掉 F5 假信息源**。当前它在主动生成错误答案，优先级最高 |

### 5.7 渲染面：`record` 块

`chat_bi_blocks.py`（`:171` 的 `task_status` 块之后）加：

```python
for rec in payload.get("ops_records") or []:
    _add({"type": "record", "record": rec})
```

前端复用已有的 `components/ObjectLanding.tsx` 徽标与 antd `Descriptions`；
列表型（历史/清单）走表格。**as_of 与 source 必须渲染出来**，用户要能看出这份事实的时点与出处。

---

## 6. 阶段与验收

> 图例：⬜ 未开始 · 🟡 进行中 · ✅ 已交付

| 阶段 | 内容 | 依赖 | 风险 | 状态 |
| --- | --- | --- | --- | --- |
| **P0 止血** | S3（拆假信息源）→ S1 → S2。**零新工具、零新概念** | 无 | 低 | ✅ |
| **P1 骨架** | `ops_records.py` 注册表 + `landing`/`task_run` 两族 reader + `get_landing` 工具 + `ops` 技能 + **账本登记（F0）** | P0 | 中 | ✅ |
| **P2 铺开** | 其余 11 族 reader + `get_ops_record` + `operational` 意图 + 路由读/写判别 + `record` 块 + 前端 | P1 | 中 | 🟡 |
| **P3 验收** | 运维问题集实测 + golden 扩例 | P2 | 低 | ⬜ |

P2 记 🟡 而非 ✅：机制（`get_ops_record` 分发 + `operational` 意图 + `record` 块）已通，
但注册表里只有 `landing`/`task_run` 两族，§3 表格中的 C/D/E/F/H/J 等族尚未接 reader。

### 本地落地进度（2026-08-28）

- ✅ P0：物化配置与物理落点命名分离；对象详情透传已有 landing；任务状态读取补齐受控 spec 字段与查询范围。
- ✅ P1：新增 `get_landing`、`get_ops_record(family=task_run)` 两个只读工具；接入 `ops` 技能、FactLedger 登记和本体作用域校验。
- ✅ P2（首批）：新增 `operational` 意图与读/写路由判别；运行记录统一返回 `as_of`、`observed_at`、`source`；前端新增 `record` 块展示事实和来源。
- ✅ P2（第二批）：`get_ops_record` 接入 `pipeline`、`decision`、`ontology_version`、`standard`；分别复用任务链服务、追加式决策账本、本体版本服务和规约服务。scope 按族收紧（会话/本体/全局），FactLedger 同时登记具名事实与原始数值。
- ⏳ 后续：扩展其余运行记录族、完整的生产问题集回放，以及跨轮 Agent run/artifact 持久化。

### 验收标准

- **覆盖率**：新建 `docs/DATA_AGENT_OPS_QUESTIONS.md`，11 族每族 8 题（约 90 题）。
  指标 = **可答率**（命中正确 family + 答案接地 + 带 `as_of`/`source`）。
  P2 完成目标 ≥ 80%；剩余 20% 必须是**明确拒答**，不允许编造。
- **不回归**：全套件 1578 个 test functions 全绿。
- **不回涨**：`avg_llm_calls` 不高于 V5 基线（V5 的验收护栏，见 `config.py:143-152` 那段实测记录）。
- **守卫不动**：只读 SQL 校验、RBAC、六环治理闸门一律不碰。

---

## 7. 明确不做

1. **不补三处「不落库」**：`agent_telemetry`（进程内计数器）、`agent_trace`（JSONL，默认关）、
   `agent_result_store`（run-local）——这三处不持久化是设计意图，不是缺口。
2. **不造第三张落点表**：两套登记并存是历史事实，一律复用 `bulk_object_landings`。
3. **不给写能力**：`ops` 技能不出任何提案。要建任务请显式 `select_skill('task')`。
4. **不引入「让 agent 直接查元数据库」的通用 SQL 反射**：那会绕开权威分层，
   读出 `GovernanceArtifact.confirmed_by`（会被 edit 清空）这类**过期事实**并当成真相答出去。
   必须经 `ops_records` 注册表，由 reader 声明 source。
5. **不改工具循环 / 不换框架**：V5 已论证过（`DATA_AGENT_V5_OPTIMIZATION.md`「为什么不换框架」）。

---

## 8. 风险

| 风险 | 后果 | 缓解 |
| --- | --- | --- |
| `_OPERATIONAL_MARKERS` 与 `_ANALYTICAL_MARKERS` 撞词 | 真取数问题被误判成运维问题 | 只用复合词；保持 analytical 赢平局；golden 加对照例 |
| `operational` 要求接地后拒答率上升 | 用户觉得"变笨了" | 拒答文案要指路（"我需要先查 X 记录，但没查到"）；P3 量化拒答率 |
| `get_ops_record` 的 `family` 枚举太大，模型选错族 | 答非所问 | description 逐族一句话 + 错选时返回 `available_families` 引导重试（对齐 `get_task_options` 的 `kind` 非法处理，`chat_bi.py:3432-3438`） |
| 13 个 reader 各自的返回口径漂移 | 前端渲染不一致 | 统一 `RecordAnswer` 信封；reader 只做投影不做判定 |
| S2 放开 `scope="all"` 后回灌过多制品 | 上下文膨胀 | 沿用 `limit` 上限 20（`chat_bi.py:5196-5200`）+ `truncated` 标记 |

---

## 9. 附：本方案的调查锚点

- 工具注册表 `_TOOL_BY_NAME`：`chat_bi_tool_schemas.py:1691-1718`
- 技能白名单 `SKILL_TOOL_ALLOWLIST`：`chat_bi_tool_schemas.py:1764-1801`
- 收窄函数 `_tools_for_skill`：`chat_bi_tool_schemas.py:1814`
- 意图分类 `_classify_intent`：`chat_bi.py:5560-5577`；标记词 `chat_bi_tool_schemas.py:1732/1741`
- 技能自动路由 `_auto_select_skill`：`chat_bi.py:5579-5626`
- 接地判定与 general 豁免：`chat_bi.py:6502-6516`
- 事实账本 `_ledger_register`：`chat_bi.py:1558-1640`
- 渲染块投影 `answer_to_blocks`：`chat_bi_blocks.py:56-200`
- 落点读模型 `ObjectLanding`：`services/object_landing.py:47-72`
- 制品 REST 全量投影 `_to_out`：`api/agents.py:105-121`
- 部署数据源真源 `OntologyWarehouseDeployment.doris_datasource_id`：`models/warehouse.py:79`
