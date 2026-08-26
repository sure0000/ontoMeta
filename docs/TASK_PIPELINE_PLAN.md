# 多任务编排 · 已建成规格与遗留事项

> 状态：**P1–P3 已交付（Doris as-built；生产切流尚未完成）**
> 基线核验：2026-08-22，后端全量 `1572 passed / 3 skipped`，Alembic 单一 head/current `d3e4f5a6b7c8`。
> 本文以当前代码为准；旧版“线性链、SeaTunnel/DataX、DolphinScheduler、无周期调度”的描述已失效。

---

## 0. 一句话结论

对话中的“物化 → 同步 → 清洗 → 聚合”已经可以落成 `GovernanceTaskPipeline`：

- 步骤支持 `depends_on`，可表达线性、扇出和汇聚；
- 可逐步起草，也可一次起草全部步骤；
- 每一步仍是独立 `GovernanceArtifact`，各自经过校验、dry-run、人工确认和执行；
- 可将整条已走通的链编译成 Airflow DAG 并设置周期调度；
- sync 使用 Flink SQL 写默认 Doris ODS；transform 与 metric/tag/rule 使用 Doris 原生 SQL；旧 Flink metric 路径不可用于新任务；
- materialize 只建结构，sync 只搬数据，两者语义分离；
- 链状态由步骤制品聚合推导，不保存第二份权威状态。

当前实际写侧类型：

```text
materialize / sync / transform / metric
```

---

## 1. 核心领域对象

| 对象 | 位置 | 责任 |
|---|---|---|
| `GovernanceTaskPipeline` | `app/models/agent.py` | 链级名称、意图、本体、周期 DAG 信息 |
| `GovernanceTaskPipelineStep` | `app/models/agent.py` | 步序、kind、intent、context、depends_on、artifact_id |
| `GovernanceArtifact` | `app/models/agent.py` | 每一步的声明式规格、校验、确认、执行和回执权威 |
| `TaskPipelineService` | `app/services/task_pipeline.py` | 建链、逐步推进、全部起草、状态聚合、上下文继承 |
| Pipeline Compiler | `app/services/pipeline_compiler.py` | 拓扑校验、编译 Airflow 链 DAG、周期化 |
| Pipeline Lineage | `app/services/pipeline_lineage.py` | 预览/回写任务链血缘 |

### 1.1 链级事实

以下字段确实属于链，故落在 `GovernanceTaskPipeline`：

- `schedule_cron`
- `compiled_dag_id`
- `compiled_at`

链整体运行状态不落库，由各步骤关联制品的状态聚合得到：

```text
全部未起草                      → drafted
部分已起草或执行中              → running
任一步 failed                   → failed
全部 succeeded                  → succeeded
```

### 1.2 步骤依赖

`depends_on_json` 保存显式上游步序：

- 空/None：回退线性默认，依赖上一步；
- 一个上游对应多个下游：扇出；
- 多个上游对应一个下游：汇聚；
- 编译前使用拓扑排序检测环。

---

## 2. 当前执行架构

```text
GovernanceArtifact Spec
        │
        ├─ materialize ─→ 目标引擎 DDL ───────────────┐
        ├─ sync ───────→ Flink SQL 数据搬运 ─────────┤
        ├─ transform ──→ Doris SQL 清洗加工 ─────────┤
        └─ metric ─────→ Doris SQL 聚合/标签/规则 ──┤
                                                       ▼
                                                Airflow DAG
                                                       ▼
                                           Flink on YARN / 目标库
                                                       ▼
                                             DagRun 状态与回执
```

### 2.1 四种制品的边界

| kind | 做什么 | 不做什么 | 主要实现 |
|---|---|---|---|
| materialize | 根据本体和契约创建物理表结构 | 不搬数据 | `agents/executors/materialize.py` |
| sync | 将真实源表数据按本体映射搬到已物化目标表 | 不创建业务表 | `agents/executors/sync.py` |
| transform | 对 ready Doris ODS Projection 生成并执行 Doris SQL 清洗 | 不直连业务源、不调用 Flink | `agents/executors/transform.py` |
| metric | MetricCompiler(doris) 编译并执行 ADS 聚合/标签/规则 SQL | 不替用户发明口径、不调用 Flink | `agents/executors/metric.py` |

### 2.1.1 单任务的调度频率（2026-08-26）

四类任务里**只有 materialize 不带调度**（建表是幂等的一次性动作，挂 cron 只会每轮重复
`CREATE`）。其余三类都在 Spec 里带自己的周期，与链级 `schedule_cron` 互不替代：

| kind | Spec 键 | 生效路径 |
|---|---|---|
| sync | `refresh_cron` | `run_sync(refresh_cron=…)` → 写回契约 → `_cron_by_entity` → 一个 cron 一个 DAG |
| transform | `schedule` | `run_doris_sql(schedule=…)` → DAG `schedule=` |
| metric | `schedule` | 同上 |

`sync.refresh_cron` 是补齐项：此前 Spec 里没有这个键，执行器读到的恒为 `None`，产出的
搬运 DAG 一律 `schedule=None`（只能人点），想定时只能绕到物化弹窗里逐实体改契约。

**cron 一律过形态校验**（`services/cron_spec.normalize_cron`，闸门里的 `schedule_invalid`）。
理由与 Flink 参数相同：这些值逐字写进 DAG，写错会让 Airflow **import 不了**那条 DAG，
而 import 失败在 ontoMeta 这边看不见——回执 ok、任务显示"已提交"，表却永远不更新。

### 2.1.2 装载方式与它要的参数

装载方式是三选一，**每一支要的参数都必须在同一张表单里填得完**，否则那个选项是死路：

| mode | 必填 | 备注 |
|---|---|---|
| full | — | staging + 原子切换 |
| incremental | `primary_keys` `incremental_column` `initial_watermark` | 水位之后由每轮成功值推进 |
| cdc | `primary_keys` `sequence_column` `delete_policy` `flink_checkpoint_dir` | checkpoint 设置页配了全局默认就跟随 |

主键/增量字段/sequence 列的候选**按所选对象收窄**（`GET /api/ontologies/{id}/properties?object_type=`）。
命中 `<对象>_id` / `id` 约定的列会被预选为主键（`is_identity`），没命中就不给默认值——
猜错主键会让重跑插出重复行。

### 2.2 统一执行通道

- 调度器：Airflow；
- 搬运执行：仅 sync 使用 Flink；
- 计算执行：transform/metric 使用 Doris SQL；
- 目标建表：Doris Adapter 生成 DDL；
- 凭据：Spec 只保存 `*_ref`/`*_alias`，运行时由受管连接配置解析；
- 状态：回执中的 `dag_id`/`dag_run_id`/`state` 及 live-state 回读；
- 未配置外部依赖时必须显式返回“仅产出/未执行”的原因，不得假绿。

---

## 3. 提案、起草与执行流程

```text
Data Agent propose_pipeline
  → 用户查看并修改各步参数
  → 创建 Pipeline（只建链）
  → advance：上游成功后起草下一步
     或 draft-all：一次起草全部步骤
  → 每个 Artifact：validate → dry-run → confirm → execute
  → 关联步骤状态聚合为链状态
  → 全部成功后可设置 cron 并 compile 为周期 DAG
```

### 3.1 上下文继承

`TaskPipelineService.INHERITED_CONTEXT_KEYS` 只继承共同落点：

- `ontology_id`
- `engine`
- `database_prefix`
- `target_datasource_id`
- `target_database`

使用白名单而不是透传全部 Spec，避免把上一步的局部参数污染下游。当前步骤显式 context 优先于继承值。

### 3.2 为什么链不自动确认

链只管理顺序与上下文，不管理授权。以下不变量不因“批量起草”或“周期 DAG”而改变：

1. 未确认制品不得首次执行；
2. 确认前必须展示校验报告与 dry-run；
3. 用户修改 Spec 后，旧确认和旧编译结果必须失效；
4. 周期调度确认的是“这个已验证版本可以反复执行”，不是永久放弃治理门槛。

---

## 4. API 与前端

### 4.1 API

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/agents/pipelines` | 创建链 |
| GET | `/api/agents/pipelines` | 列表 |
| GET | `/api/agents/pipelines/{id}` | 详情与聚合状态 |
| POST | `/api/agents/pipelines/{id}/advance` | 起草下一可执行步骤 |
| POST | `/api/agents/pipelines/{id}/draft-all` | 起草全部未起草步骤 |
| PUT | `/api/agents/pipelines/{id}/schedule` | 设置 cron |
| POST | `/api/agents/pipelines/{id}/compile` | 编译周期 DAG |
| DELETE | `/api/agents/pipelines/{id}/schedule` | 下线周期任务记录 |
| GET/POST | `/api/agents/pipelines/{id}/lineage` | 血缘预览/回写 |

整个 `/api/agents` 命名空间需要 publisher。

### 4.2 前端

`frontend/src/pages/chat-bi/ChatBiReferences.tsx::PipelineProposalBlock` 已提供：

- 提案步骤和参数编辑；
- 创建任务链；
- 逐步起草；
- 一键起草全部步骤；
- 打开每一步的治理制品抽屉；
- 校验、确认、执行和回执；
- 周期 CronPicker、编译和下线；
- 会话与制品关联，支持后续回读状态。

---

## 5. 已交付里程碑

| 阶段 | 状态 | 交付内容 |
|---|:---:|---|
| P1 | ✅ | Flink SQL 生成、Airflow 投递、transform/metric/sync 执行与 live-state |
| P2 | ✅ | 链级 schedule、周期 DAG 编译、前端周期任务控件 |
| P3 | ✅ | `depends_on` DAG、拓扑环检测、扇出/汇聚、Pipeline 血缘、失败续跑 |

测试覆盖主要位于：

- `tests/test_task_pipeline.py`
- `tests/test_pipeline_compiler.py`
- `tests/test_pipeline_dag_topology.py`
- `tests/test_pipeline_lineage.py`
- `tests/test_agent_pipeline.py`
- `tests/test_agent_implementations.py`
- `tests/test_transform_metric_executor_flink.py`
- `tests/test_sync_executor_chain.py`

---

## 6. 当前遗留

### 6.1 建模语义缺口

任务链编排的是已知执行步骤，不负责确认业务需求、事实粒度、维度、SCD 或指标包。该缺口由：

- `docs/CONVERSATIONAL_ONTOLOGY_MODELING_OPTIMIZATION_PLAN.md`

中的 ModelingCase、DimensionalModel、LogicBundle 和 DeliveryPlan 方案补齐。

### 6.2 关键源 STG 保全

`SyncDrafter` 会产出 `preservation` 判定，但当前 Flink 搬运路径尚未额外生成 STG 原始副本。执行回执会显式标记 `preservation_pending`，不会静默声称已保全。

### 6.3 部分配置的“仅产出”语义

当缺少目标数据源、Airflow 或 Flink 运行配置时，Executor 可能返回声明式产物而不真实执行。后续建模工单的完成门槛必须区分：

- `artifact generated`
- `job submitted`
- `job succeeded`
- `business result accepted`

### 6.4 周期 DAG 文件下线

当前下线会清理 ontoMeta 的编译记录；Airflow `dags_dir` 中的文件仍需部署侧删除。生产收口时应增加受控的 DAG 文件撤销能力或明确运维动作。

---

## 7. 不变量清单

1. 链不替任何制品确认；
2. 链状态由制品聚合，不保存第二份；
3. 依赖环在编译前拒绝；
4. 上游失败时下游不执行；
5. 凭据不进入 Spec、DAG 产物和对话；
6. materialize 只建结构，sync 只搬数据；
7. transform/metric 的 SQL 结构复用本体、WarehouseGenerator 与 MetricCompiler，不另写第二套权威；
8. 未真实执行必须在回执中明确，禁止假成功；
9. 相同制品执行保持幂等；
10. 任务链未来接入 ModelingCase 时，仍以 GovernanceArtifact 为执行状态权威。

---

## 8. 验收命令

```bash
cd backend && .venv/bin/pytest -q \
  tests/test_task_pipeline.py \
  tests/test_pipeline_compiler.py \
  tests/test_pipeline_dag_topology.py \
  tests/test_pipeline_lineage.py \
  tests/test_agent_pipeline.py \
  tests/test_transform_metric_executor_flink.py \
  tests/test_sync_executor_chain.py

cd frontend && npm run lint && npm run build
```
