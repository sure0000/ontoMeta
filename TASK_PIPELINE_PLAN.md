# 多任务编排 · 剩余事项

> 目标：「物化之后清洗、清洗完成聚合」这类**前后相继的多个任务**可被编排。
> 决策已定（2026-08-06）：**分两步做**——第一步落在提案层（已交付），第二步落到执行层（本文主体）；
> **人工确认按每环单独确认**；**触发方式手动与周期调度都要支持**。
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

即：**「清洗完成」目前没有任何客观信号**。制品 `succeeded` 只表示「SQL 生成成功」，不表示
那条 SQL 跑过。第一步的链因此是「人看着上一步的产物，自己判断该不该推进」——在提案层这是
诚实的（我们从没声称跑过），但周期调度必须有真实的完成信号，否则「上游成功才跑下游」无从谈起。

### 1.2 执行层编排：一条 DAG 串起多个任务

现有 `AirflowDagBuilder.build()` 已经具备大半：

- 入参已有 `schedule`（cron）、`max_active_tasks`、`dag_id_suffix`；
- DAG 内已有**任务依赖**（`_task_group_order` 按 dim → dwd → dws → ads 串层，`_with_swap` 在搬运后挂切换）；
- **已经有一种「在数仓上跑一段 SQL」的任务形态**：`SQLExecuteQueryOperator(conn_id=warehouse_conn_id, sql=...)`，
  建表 DDL 与 staging 切换都走它。

**这是 1.2 可行性的关键**：transform 与 metric 的产物就是「一段跑在目标数仓上的 SQL」，
形态与已有的 DDL/swap 任务完全一致，不需要新的执行通道、不需要新镜像、不需要碰 sync-runner。

---

## 2. 分阶段拆解

### P1 · 把 transform / metric 接进 Airflow 执行通道

**做什么**：让这两类制品的 `execute` 不再只是「渲染完交给别人」，而是像 materialize 一样落一条
DAG 并触发，回执带 `dag_run_id`，状态可回读。

| 任务 | 文件 | 说明 |
|---|---|---|
| P1-1 | `app/services/airflow_dag_builder.py` | `build()` 增加 `sql_tasks: tuple[SqlTask, ...]`（`task_id` / `sql` / `depends_on`），渲染成 `SQLExecuteQueryOperator`。**复用现有 DAG 骨架**，不新写一份 |
| P1-2 | `app/services/sql_task_runner.py`（新） | 「一批 SQL → DAG 落盘 → 触发 → 回读」的公共路径，从 `materialization_runner._run_orchestrated` 抽出可复用部分 |
| P1-3 | `app/agents/executors/transform.py` | `execute` 改为经 P1-2 提交；保留 `handoff` 模式作为**未配 Airflow 时的降级**，且回执里明说「未执行，仅产出」 |
| P1-4 | `app/agents/executors/metric.py` | 同上（DDL + 聚合 SQL 两个任务，DDL 在前） |
| P1-5 | `app/api/agents.py` | 制品状态回读接上 DagRun 实时态（比照 `chat_bi._live_task_state`） |

**验收**：
- 一条 transform 制品执行后回执带 `dag_run_id` 与 `run_url`，Airflow 里能看到该 DAG 跑过；
- 未配 Airflow 时**不报错**，退回「仅产出」并在回执里显式说明——不静默假装执行了；
- `sync` 暂不接（它走 SeaTunnel 作业配置，另有通道，见 P3）。

**风险**：`transform` 的 ETL SQL 由 `warehouse_generator.generate_etl_sql` 产出，源表来自 ODS；
若源库与目标仓不是同一个连接，`SQLExecuteQueryOperator` 单连接跑不通。**动手前先确认**这条
SQL 在真实部署里是不是同库跨 schema；不是的话 transform 得走搬运通道而非 SQL 通道，P1-3 改期。

### P2 · 把整条链编译成一条 DAG（含周期调度）

**做什么**：`GovernanceTaskPipeline` → 一条带依赖的 Airflow DAG，挂 cron 周期跑，
下游等上游本周期成功。

| 任务 | 文件 | 说明 |
|---|---|---|
| P2-1 | `app/models/agent.py` + 迁移 | `GovernanceTaskPipeline` 加 `schedule_cron`、`compiled_dag_id`、`compiled_at` |
| P2-2 | `app/services/task_pipeline_compiler.py`（新） | 链 → DAG：逐步取各自制品的 spec，转成任务组，按 `step_index` 串依赖 |
| P2-3 | `app/api/agents.py` | `POST /agents/pipelines/{id}/compile`（编译并提交）、`DELETE .../schedule`（下线） |
| P2-4 | 前端 `PipelineProposalBlock` | 全链走通一遍后出现「挂成周期任务」按钮 + `CronPicker`；显示 `compiled_dag_id` 与最近 DagRun |
| P2-5 | `app/services/task_pipeline.py` | `detail()` 带上周期态（已挂调度 / cron / 最近一次运行） |

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
| P3-1 | `sync` 进链：它产的是 SeaTunnel 作业配置，DAG 里应复用 runner 通道的 `_sync` 任务而非 SQL 任务 |
| P3-2 | 链支持扇出/汇聚（DAG 形态）。**只在 P2 之后做**——提案层保持线性是有意的，分叉的真正去处是编译后的 DAG |
| P3-3 | 链级血缘：把 P2 的跨任务依赖上报 DataHub（复用 `lineage_emitter`） |
| P3-4 | 失败续跑：链在第 N 步失败后，从断点重跑而不是从头 |

---

## 3. 不变量清单（改动前先读）

1. **未确认不得执行**（逐制品）。P2 的周期调度是唯一的例外，且以「确认前移 + spec 快照 + 变更失效」
   换取，不是简单放宽。
2. **ontoMeta 只生成产物，不做第二个调度器**。P1 把 transform/metric 接进 Airflow，接的是**已有的**
   那条通道；不要为它们新建执行框架。
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
