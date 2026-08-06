# 多任务编排 · 剩余事项

> 目标：「物化之后清洗、清洗完成聚合」这类**前后相继的多个任务**可被编排。
> 决策已定（2026-08-06）：**分两步做**——第一步落在提案层（已交付），第二步落到执行层（本文主体）；
> **人工确认按每环单独确认**；**触发方式手动与周期调度都要支持**。
> 决策补充（2026-08-06）：**同步一律走 SeaTunnel / DataX**（不论是否同源）；**transform / metric
> 生成 Flink SQL，经 Airflow 触发 Flink 引擎执行**，与 materialize 同构；**离线 / 实时由制品 spec 的
> `execution_mode`（`batch` / `streaming`）决定**。Flink 由此从「搬运工具」序列退出，专职做计算。
> 本文是执行计划：拆到文件级任务、验收标准、以及必须守住的不变量。

---

## 0. 第一步已交付：提案层任务链

对话里说「物化到数仓，然后清洗，再按口径聚合」→ Agent 出一条链 → 点「创建任务链」只建链、
不起草任何制品 → 逐步点「起草第 N 步」→ 每步照旧在制品抽屉里走
「校验 → dry-run → 人工确认 → 执行」。上一步执行成功后下一步才解锁，届时目标数据源/库/引擎
自动接过去。

| 交付物 | 位置 |
|---|---|
| 链与步骤模型 | `app/models/agent.py`（`GovernanceTaskPipeline` / `GovernanceTaskPipelineStep`） |
| 迁移 | `alembic/versions/c2d3e4f5a6b7_governance_task_pipelines.py` |
| 编排服务 | `app/services/task_pipeline.py`（`create` / `advance` / `detail`） |
| API | `POST/GET /api/agents/pipelines`、`GET /{id}`、`POST /{id}/advance` |
| Agent 工具 | `chat_bi._dispatch_propose_pipeline` + `propose_pipeline` 工具 + `pipeline_proposal` 块 |
| 前端 | `ChatBiReferences.PipelineProposalBlock` |
| 测试 | `tests/test_task_pipeline.py`（15）、`test_agent_implementations.py` 末两条端到端 |

**已经确立、后续不得推翻的三条**：

1. **链不替谁确认**。服务、API、UI 三处都没有「一键跑完整条链」的入口——那必然绕过逐制品的
   人工确认，而「未确认不得执行」是这条流水线的硬不变量。`test_service_offers_no_way_to_execute_a_whole_chain`
   钉住这点。
2. **下游不预先起草**。它的 context 要等上游真跑完才配得齐；提前起草出来的是预测不是事实，
   而制品一旦落库就会被人当成已经定下的东西。
3. **链态不落库**，由各步制品的状态聚合推导（`TaskPipelineService._status`）。制品的权威在
   `governance_artifacts`，另存一份迟早分叉。

**第一步没做的**：链是线性的（不是 DAG）；只能手动逐步推进，**没有周期调度**。

---

## 1. 还差什么

两件事，前者是后者的前提：

### 1.1 阻断项：transform / metric 根本不执行

| kind | `Executor.execute` 实际做了什么 | 有没有「完成」信号 |
|---|---|---|
| `materialize` | 生成 DAG 落盘 + 触发 Airflow，回执带 `dag_run_id`，状态可回读 | ✅ 有（Airflow DagRun） |
| `sync` | 只渲染 SeaTunnel 作业配置，`handoff: "SeaTunnel + DolphinScheduler"` | ❌ 无 |
| `transform` | 只渲染 ETL SQL，`handoff: "DolphinScheduler"` | ❌ 无 |
| `metric` | 只渲染 DDL + 聚合 SQL，`handoff: "DolphinScheduler"` | ❌ 无 |

**目标通道（2026-08-06 决策）**：同步（sync）不论是否同源，一律走 **SeaTunnel / DataX**；
transform（清洗）、metric（聚合）这类计算任务生成 **Flink SQL**，经 Airflow 触发 **Flink 引擎**
执行——与 materialize「生成 → 落盘 → 触发 → 回读」同构。Flink 因此从**搬运工具**序列退出，
专职做计算。「清洗完成」的客观信号即 Flink 作业的 DagRun 终态。

即：**「清洗完成」目前没有任何客观信号**。制品 `succeeded` 只表示「SQL 生成成功」，不表示
那条 SQL 跑过。第一步的链因此是「人看着上一步的产物，自己判断该不该推进」——在提案层这是
诚实的（我们从没声称跑过），但周期调度必须有真实的完成信号，否则「上游成功才跑下游」无从谈起。

### 1.2 执行层编排：一条 DAG 串起多个任务

现有 `AirflowDagBuilder.build()` 与 `materialization_runner._run_orchestrated` 已经具备大半：

- 入参已有 `schedule`（cron）、`max_active_tasks`、`dag_id_suffix`；
- DAG 内已有**任务依赖**（`_task_group_order` 按 dim → dwd → dws → ads 串层，`_with_swap` 在搬运后挂切换）；
- **已经有一条「生成产物 → 落盘 → 触发 Airflow → 回读 DagRun」的完整通道**：materialize 就走它，
  回执带 `dag_run_id` / `run_url` / `state`，解析等待有 `_wait_for_parse` 兜。

**这是 1.2 可行性的关键**：transform / metric 的 **Flink SQL 作业**与 materialize 的搬运 / DDL 作业
**共享同一条 Airflow 投递通道**——同样是「产物落盘 + 触发 + 回读」，不需要新的调度器、不需要碰
sync-runner。差别只在任务体：materialize 落 DDL / 搬运任务，transform / metric 落 Flink SQL 作业任务
（`flink run -f <job.sql>`）。

---

## 2. 分阶段拆解

### P1 · 让 transform / metric 生成 Flink SQL，经 Airflow 触发 Flink 执行

**做什么**：这两类**计算**任务不再只渲染 SQL 交人，而是生成 **Flink SQL** → 落盘 → 由 Airflow
触发 `flink run` → 回读作业终态，与 materialize 完全同构。离线 / 实时由制品 spec 的 `execution_mode`
（`batch` / `streaming`）决定：`batch` 走有界流批，`streaming` 走持续流（CDC 口径的实时清洗 / 聚合）。

| 任务 | 文件 | 说明 |
|---|---|---|
| P1-0 ✅ | `app/services/sync_tool_resolver.py` | 已交付：`_PRIORITY` 去掉 flink；新增 `_NON_SYNC_TOOLS={flink}`，auto 候选源头剔除 flink，pin flink 两通道都拒。FlinkAdapter 仍留注册表（供计算侧复用、诊断展示）。新增 3 条测试，34+70 既有用例绿 |
| P1-1 ✅ | `app/services/flink_sql_generator.py`（新） | 已交付：`generate_flink_sql()` 把「源表 + 目标表 + SELECT 体」组装成完整脚本（SET 模式 + CREATE TABLE source/sink + INSERT）。JDBC connector 逐表声明；类型常规映射；batch/streaming 切换（streaming 支持 watermark）；凭据只走占位符。SELECT 体由 executor 生成（FROM 引用 Flink 裸表名）。9 条单测绿 |
| P1-2 ✅ | `app/services/flink_job_runner.py`（新） | 已交付：`run_flink_sql()` 把一批 Flink SQL 任务打包成一次 Airflow 提交（生成 DAG → 落盘 → 触发 → 回读）。复用 materialize 的 `AirflowClient` / `_wait_for_parse` / `trigger_dag`。未配 SqlRunner JAR 退回「仅产出」；未配 Airflow 报错；触发失败如实记 error。4 条单测绿 |
| P1-3 ✅ | `app/services/airflow_dag_builder.py` | 已交付：`build_flink_sql_dag()` + `FlinkSqlTask` / `FlinkSubmitConfig`。Flink on YARN 走 BashOperator：`flink run -t yarn-per-job -c <SqlRunner> <jar> --file <job.sql>`。.sql 落 job_files（`write` 支持文本件）；batch attached / streaming detached（-d）；可选 create_sink_tables（metric 的 ads 表）；依赖串联。部署参数进 `config.py`（FLINK_SQL_RUNNER_JAR 等）　11 条单测绿 |
| P1-4 ✅ | `app/agents/executors/transform.py` | 已交付：`execute` 改为调 `warehouse_generator.build_flink_etl_input`（守住映射逻辑不重写）+ `flink_job_runner.run_flink_sql`；spec 读 `execution_mode`；未配 datasource/SqlRunner JAR 退回「仅产出」并明说原因。4 条集成测试绿 |
| P1-5 ✅ | `app/agents/executors/metric.py` | 已交付：同 P1-4，metric 的 ads 表先在数仓建（warehouse_ddl），再执行 Flink 聚合。回执带 dag_run_id/run_url。集成测试复用 P1-4 |
| P1-6 ✅ | `app/api/agents.py` + schema | 已交付：`get_artifact` 加 `live_state` 字段，best-effort 回读 DagRun 实时态（复用 warehouse 的 `_receipt_batches`/`_aggregate_state`）。transform/metric 回执的单 DAG 结构经 fallback 自然支持。136 tests 绿 |
| P1-7 ✅ | drafter（transform/metric）| 已交付：transform / metric 的 drafter 产出 spec 时带 `execution_mode`（从 context 读，缺省 `batch`；metric 允许 `streaming`）。spec 无独立 Pydantic schema（直接 dict），故落在 drafter 输出层；executor 读同名字段。全套 1079 tests 绿 |

**验收**：
- 一条 transform 制品执行后回执带 `dag_run_id` 与 `run_url`，Airflow 里能看到该 DAG 触发了 Flink 作业；
- `execution_mode=streaming` 的制品产出持续流作业，`batch` 产出有界批作业；
- 未配 Flink / Airflow 时**不报错**，退回「仅产出」并在回执里显式说明——不静默假装执行了；
- 同步侧确认：`resolve_sync_tool` 在 auto 时只会选出 seatunnel 或 datax，绝不再选 flink。

**已消解的旧风险**：上一版担心 transform 的 ETL SQL「源库与目标仓非同一连接则单连接跑不通」。
改走 Flink 后，source / sink 由各自的 connector 声明、天然跨源，**不再要求同库跨 schema**，该风险作废。

### P2 · 把整条链编译成一条 DAG（含周期调度）

**做什么**：`GovernanceTaskPipeline` → 一条带依赖的 Airflow DAG，挂 cron 周期跑，
下游等上游本周期成功。

| 任务 | 文件 | 说明 |
|---|---|---|
| P2-1 ✅ | `app/models/agent.py` + 迁移 | 已交付：`GovernanceTaskPipeline` 加 `schedule_cron`、`compiled_dag_id`、`compiled_at` 字段，跟踪已挂成周期任务的 DAG 与时间。迁移 `c1385f0ad1e8` 已生成 |
| P2-2 ✅ | `app/services/pipeline_compiler.py`（新） | 已交付：`compile_pipeline()` 校验门槛（所有步骤已确认、已执行、spec 未变更）、提取各步 DAG id、生成串联 DAG。**关键修正**：用 `TriggerDagRunOperator(wait_for_completion=True)` 替代 `ExternalTaskSensor`（后者按相同 execution_date 匹配上游、而各步 DAG 是手动触发的、时间戳不对齐会永远等不到）。9 条单测绿 |
| P2-3 ✅ | `app/api/agents.py` + schema | 已交付：`PUT /agents/pipelines/{id}/schedule`（设 cron）、`POST .../compile`（编译）、`DELETE .../schedule`（下线）。前提不满足时 409，错误说清卡在哪步。1088 tests 绿 |
| P2-4 ✅ | 前端 `ChatBiReferences.tsx` + types + api | 已交付：`PipelineProposalBlock` 里链走通（status=succeeded）后显示周期任务控件：未编译时显示 CronPicker + 编译按钮；已编译时显示 compiled_dag_id + cron 描述 + 下线按钮。前端 build 通过 |
| P2-5 ✅ | `app/services/task_pipeline.py` | 已交付：`detail()` 返回加 `schedule_cron`/`compiled_dag_id`/`compiled_at`（P2-1 时已加） |

**必须守住的门槛**（这是本阶段最容易被做坏的地方）：

> **只有每一步都已人工确认过，整条链才可编译成周期任务。**
>
> 周期调度天然是「无人值守反复执行」，与「每次执行都要人确认」直接冲突。折中不是放宽确认，
> 而是**把确认前移**：人确认的是「这条链的这个版本可以反复跑」，编译时把各步 spec 快照进 DAG；
> 任一步的 spec 之后被改动，`compiled_dag_id` 即失效、需重新确认并重编译。
>
> 因此 P2-3 的 compile 端点必须校验：所有步骤的制品都处于 `succeeded`，且 spec 未在确认后变更。

**验收**：
- 一条「物化 → 清洗 → 聚合」的链编译出**一条** DAG，Airflow 图上三组任务顺序相连；
- 挂 `0 2 * * *` 后每天跑一次，上游任务失败时下游不执行（Airflow 默认 `all_success` 即可）；
- 未全部确认的链请求编译 → 409，并说清卡在哪一步。

### P3 · 收尾

| 任务 | 说明 |
|---|---|
| P3-1 ✅ | `app/agents/executors/sync.py` | 已交付：sync = 对单对象跑搬运。有 target_datasource 时复用 `materialization_runner.run(selected_targets=[object_type])`（搬运通道产 dag_id，可被 compiler 串进链）；无则 handoff 降级。4 条测试绿 |
| P3-2 ✅ | 模型 + 编译器 + 迁移 | 已交付：步骤模型加 `depends_on_json`（显式依赖的上游步序列表，空=沿用线性默认）。`_validate_dag_topology()` 拓扑排序检测环（Kahn 算法）。`_render_chain_dag()` 按 depends_on 串联触发器（而非纯线性）。支持扇出（一上游分叉多下游）、汇聚（多上游汇一下游）。迁移 `d467f452d8b8` 已生成。7 条测试绿（线性/扇出/汇聚/环检测/端到端） |
| P3-3 ✅ | `app/services/pipeline_lineage.py`（新）+ API | 已交付：`PipelineLineageEmitter` 从链各步回执提取目标表，按 step_index 串成 上一步目标→下一步目标 的血缘边，preview/apply 分离上报 DataHub（复用 `datahub.build_dataset_urn` / `add_lineage_edge`，platform 取各步 spec 的 engine）。未执行的步骤如实 skip。4 条测试绿 |
| P3-4 ✅ | 失败续跑 | **已天然支持**：链 DAG 用 `TriggerDagRunOperator` 串联，上游失败时下游不触发（Airflow 默认 `all_success`）。断点续跑：在 Airflow UI 对失败的链 DAG run 点 "Clear" 选中失败任务，rerun 即从断点续跑——这是 Airflow 原生能力，无需额外代码。需在 UI/文档说明操作 |

---

## 3. 不变量清单（改动前先读）

1. **未确认不得执行**（逐制品）。P2 的周期调度是唯一的例外，且以「确认前移 + spec 快照 + 变更失效」
   换取，不是简单放宽。
2. **ontoMeta 只生成产物，不做第二个调度器**。P1 让 transform / metric 生成 Flink SQL 并**经已有的
   Airflow 通道**触发 Flink 执行——Flink 是执行引擎、Airflow 是调度器，二者都已存在；不要为它们
   新建执行框架，也不要把 Flink 当第二个调度器用。
3. **不静默降级**。未配 Airflow 时退回「仅产出」必须在回执里显式说明——回执上写着 `succeeded`
   而实际没跑过，是这套系统里代价最高的一类谎。
4. **链态由制品聚合推导**，不落第二份状态。
5. **凭据不进 Spec**，只传 `*_ref` / `*_alias`。

---

## 4. 已知遗留（与本计划相关，但不属于它）

- `transform` 的目标对象在没给 `target_table` 时由 `select_by_intent` **按意图猜**。链上第 2 步若
  没显式给对象，猜错了要到 dry-run 才看得出来。建链表单应把它做成必填下拉（同物化表单的做法）。
- `metric` 要求口径已在「业务逻辑」里定义好，否则起草即报错。Agent 侧应在建链时先查一遍，
  而不是让用户点到第 3 步才发现。
- 物化的目标表名在建数表单里仍是单个自由文本，而物化弹窗是**逐实体**的表名 + 推荐值 + 「已存在/将新建」
  标注。两者尚未对齐。
