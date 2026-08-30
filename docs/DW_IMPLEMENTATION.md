# 本体驱动的智能数仓 · 已建成执行规格

> 状态：**as-built（Phase 0–5 与 Phase 6 控制面已落代码；生产迁移在步骤 1 阻断，未切流）**
> 基线核验：2026-08-22，后端全量 `1572 passed / 3 skipped`，Alembic 单一 head/current `d3e4f5a6b7c8`；前端 lint 0 error / 23 存量 warning，build 成功。
> 生产执行报告：`DORIS_PHASE6_PRODUCTION_MIGRATION_REPORT.md`。
> 当前写侧制品只有 `materialize / sync / transform / metric`；已移除的 cluster/Bigtop Manager、SeaTunnel/DataX、DolphinScheduler 路径不属于当前架构。

---

## 1. 核心不变量

**本体是一级语义源数据，物理表是可重建的二级投影。**

| 层次 | 内容 | 权威性 |
|---|---|---|
| 源系统 | ERP/CRM 等物理表与数据 | 建模证据与业务事实来源 |
| 本体 | 对象、属性、关系、业务逻辑 | 引擎无关的业务语义权威 |
| 物化契约 | 层、目标引擎、装载、分区、SCD、调度 | 本体到物理落地的受控配置 |
| 物理数仓 | DIM/DWD/DWS/ADS 表与任务 | 本体和契约的派生结果，可重建 |

由此得到以下硬约束：

1. 生成器必须确定性、幂等；
2. 引擎知识只存在于 Dialect Adapter；
3. 只有本体中已确认且可物化的实体才生成物理结构；
4. 同步按本体映射搬运，不直接照搬源 schema；
5. 能力不足必须进入 warning/unsupported/error，不得静默降级；
6. LLM 只产声明式 Spec，确定性 Executor 负责生成和执行；
7. 未确认制品不得首次执行；
8. 凭据不进入 Spec、LLM 上下文或生成产物。

> DIM/DWD/DWS/ADS 目前是物化契约的 `target_layer`，不是完整维度建模范式。业务过程、事实粒度、度量加性、一致性维度等能力见 `CONVERSATIONAL_ONTOLOGY_MODELING_OPTIMIZATION_PLAN.md`。

---

## 2. 当前闭环

```text
DataHub 元数据 / 人工定义
  → LLM 草稿 + 规则证据
  → 工作区人工编辑、冲突处理、发布本体
  → 物化契约
  → 写侧 Agent 生成声明式制品
  → Validation Gate + dry-run
  → 人工确认
  → Airflow/Flink/目标数仓执行
  → 状态与回执
  → DataHub 血缘/语义回写
  → Chat BI / Data App
```

当前四种写侧制品：

| kind | 输入 | Spec 主要内容 | 确定性执行 |
|---|---|---|---|
| `materialize` | 本体 + 物化范围/目标 | 目标数据源、库表覆盖、层、装载配置 | 只创建物理表结构，不搬数据 |
| `sync` | 本体对象 + 真实源表 | 源/目标、装载方式、分区、连接别名 | Flink SQL 搬运到已物化表 |
| `transform` | 本体对象 + 清洗需求 | 目标对象、清洗规则、执行模式 | Flink SQL 清洗加工 |
| `metric` | 已确认业务逻辑 | 口径、主对象、维度、过滤、目标层 | MetricCompiler + Flink SQL 聚合/标签/规则 |

写侧类型的唯一运行时真源是 `app/agents/__init__.py::register_builtin_agents()`。

---

## 3. RBAC 与安全边界

角色：

```text
reader < editor < reviewer < publisher
```

- GET 默认 reader；
- 普通写操作默认 editor；
- 冲突解决、确认类操作至少 reviewer；
- `/api/agents`、发布、执行、设置、主体管理等至少 publisher；
- `ONTOMETA_ADMIN_TOKEN` 是 bootstrap superuser，等价 publisher；
- 令牌库内只保存哈希和前缀；
- 凭据只存在于设置/数据源受管配置和运行时注入，不进入治理制品 Spec。

集中策略位于 `app/auth.py::minimum_role_for`，端点不得再创建互相矛盾的本地角色体系。

---

## 4. 物化契约

`MaterializationContract` 挂在本体对象、关系或业务逻辑上，主要字段：

- `target_layer`: dim/dwd/dws/ads
- `target_engines`
- `load_strategy`: full/incremental/cdc
- `partition_key`
- `scd_type`: none/scd1/scd2
- `refresh_cron`
- `materialized`

默认推导：

| 本体实体 | 默认落层 | 默认是否物化 |
|---|---|---:|
| `business_object` | DIM | 是 |
| `bridge` 对象 | DWD | 是 |
| `fact_table/bridge_table` 关系 | DWD | 是 |
| `foreign_key` 关系 | DWD 语义，仅列约束 | 否 |
| BusinessLogic | ADS | 是 |
| 技术/普通数据表 | 规约默认层 | 否 |

人工 patch 的字段会进入 `overridden_fields` 并被钉住，机器重推导只更新未钉住字段。

主要实现：

- `app/models/warehouse.py`
- `app/services/materialization_contract.py`
- `app/api/warehouse.py`
- `frontend/src/components/MaterializationContractPanel.tsx`

---

## 5. 引擎无关逻辑模型与 Adapter

`app/warehouse/` 是引擎知识的唯一入口：

- `logical_schema.py`: `LogicalSchema` / `LogicalTable` / `LogicalColumn` / `LogicalConstraint`
- `capabilities.py`: 能力检查与 `CapabilityError`
- `adapters/`: 各引擎类型、DDL、ALTER、装载与方言翻译
- `registry.py`: Adapter、DSN scheme 和驱动提示注册表

当前注册引擎：

```text
clickhouse / doris / hive / iceberg / postgres / starrocks
```

`kyuubi` 是 Hive 方言别名，不作为独立物化引擎公开。

生成器必须通过 `get_adapter(engine)` 获取能力与渲染行为，不得直接在服务层按 engine 写分支。

---

## 6. 本体到物理正向生成

`app/services/warehouse_generator.py::WarehouseGenerator` 负责：

- 本体 + 物化契约 → `LogicalSchema`
- DDL
- ETL 映射 SQL
- 分层依赖 DAG
- 本体到物理 mapping
- 跨引擎派生描述
- 完整 bundle

关键映射：

| 本体信息 | 物理产物 |
|---|---|
| `ObjectType` + `Property` | 表与列 |
| `display_name/description` | 表/列注释 |
| identifier 与命名/唯一证据 | 主键候选或 warning |
| `RelationType(foreign_key)` | 外键声明（能力允许且字段可证明） |
| `fact_table/bridge_table` + mapping object | DWD 明细/桥接表 |
| `BusinessLogic` | ADS 结果表形状 |
| `source_ref/source_field_ref` | 源表、字段映射 |

必须显式返回的不可生成情况包括：

- 缺物化契约；
- 缺物理源；
- N:N 缺桥接；
- 事实/桥表缺实现对象；
- 粒度/实现表冲突；
- 外键目标未物化或字段不可证明；
- 引擎能力不足；
- 依赖环。

---

## 7. 治理制品状态机

`GovernanceArtifact` 状态：

```text
drafted → validated → confirmed → executing → succeeded | failed
```

核心服务：`app/services/agent_pipeline.py`。

### 7.1 校验

`app/agents/validation.py` 检查：

- kind 是否已注册；
- Spec 必填项；
- 本体对象/字段/口径引用是否真实；
- 引擎是否存在及能力是否可用；
- 主对象、目标数据源等执行前提；
- 凭据字段；
- 生效治理规约。

校验报告包含 issue 与 dry-run，确认前必须呈现。

### 7.2 确认与执行

- `confirm` 只接受通过校验的制品；
- `execute` 只接受 confirmed；
- 执行时注入 `artifact_id` 作为幂等 run id；
- succeeded 重放返回已有回执，不产生第二次副作用；
- 外部执行状态通过 DagRun/live-state 对账，真实失败可回写 failed。

`HIGH_RISK_KINDS` 当前为空；原 cluster 类型已移除。该机制保留，未来新增不可逆类型时再显式加入。

---

## 8. 四类 Drafter / Executor

## 8.1 Materialize

文件：

- `agents/drafters/materialize.py`
- `agents/executors/materialize.py`

边界：

- 只创建本体投影的物理结构；
- 使用目标数据源类型决定引擎；
- 提交前运行 materialize preflight；
- DDL 建表幂等；
- 不产生搬运作业、不改变已有业务数据。

## 8.2 Sync

文件：

- `agents/drafters/sync.py`
- `agents/executors/sync.py`

边界：

- 只适用于有真实物理源表的本体对象；
- 幂等确保同步目标表并搬数据；无独立加工时，同一张表直接作为本体 serving 表；
- 统一走 `materialization_runner.run_sync` 的 Flink SQL 通道；
- 连接信息以 alias/ref 传递；
- 关键源保全判定已进入 Spec，但额外 STG 副本尚未在 Flink 路径实现，回执会显式 `preservation_pending`。

## 8.3 Transform

文件：

- `agents/drafters/transform.py`
- `agents/executors/transform.py`

边界：

- 目标对象与字段结构来自本体/物化契约；
- 只将闭集清洗规则确定性叠加到 WarehouseGenerator 的映射 SQL；
- 当前可确定性执行的核心规则包括关键字段空值过滤与按键去重；
- 跨库由 Flink source/sink connector 承担；
- batch/streaming 与作业级 Flink 参数由 Spec 控制，空值跟随设置页默认。

## 8.4 Metric

文件：

- `agents/drafters/metric.py`
- `agents/executors/metric.py`
- `services/metric_compiler.py`

边界：

- Drafter 选择并结构化已存在的 BusinessLogic，不替用户定义口径；
- metric/tag/rule 共享编译路径，但结果列形状不同；
- 优先使用 `expression_json` AST 通过 MetricCompiler 生成 SQL；
- 标签/规则没有形式化表达式时拒绝生成任务；
- 主对象缺失时拒绝，不生成 `FROM <未绑定>`；
- 结果表 DDL 与执行 SQL读取同一份 `result_column_specs()` 权威。

---

## 9. 执行编排

当前执行路径：

```text
声明式 Spec
  → WarehouseGenerator / MetricCompiler
  → Flink SQL 或目标引擎 DDL
  → Airflow DAG 文件与作业文件
  → 触发 DagRun
  → 回读 state/run_url/log_url
```

主要实现：

- `services/flink_sql_generator.py`
- `services/move_job_compiler.py`
- `services/airflow_dag_builder.py`
- `services/materialization_runner.py`
- `services/pipeline_compiler.py`

配置遵守 `DEVELOPMENT_PRINCIPLES.md`：Airflow/Flink 运行期配置从设置页/数据库读取；作业级并行度、队列、提交目标、checkpoint 和额外 `-D` 可在 Spec 覆盖。

未配置外部执行依赖时，Executor 可以返回“仅产出”，但回执必须明确没有真实执行。上层业务完成状态不得只依据 Artifact 的本地产物生成成功。

---

## 10. 多任务 Pipeline

`GovernanceTaskPipeline` 支持：

- 对话提案；
- 逐步起草；
- 一次起草全部步骤；
- `depends_on` 线性、扇出、汇聚；
- 拓扑环检测；
- 上下文白名单继承；
- 链状态由制品聚合；
- 周期 cron；
- 编译为 Airflow 链 DAG；
- 血缘预览/回写；
- Airflow 原生失败续跑。

详见 `TASK_PIPELINE_PLAN.md`。

---

## 11. DataHub 回写与血缘

现有能力：

- 发布本体的名称/描述/术语/域回写；
- preview/apply 分离；
- 不用空值覆盖已有元数据；
- 物化与任务链血缘计划、回写；
- 表级血缘为当前可依赖主路径；字段映射可计算，但是否发出受目标 DataHub 版本支持度约束。

首次对目标 DataHub 版本执行 mutation/血缘回写前，仍需在非生产实例验证实际 GraphQL/aspect 能力。

---

## 12. Chat BI 与 Data App

- Chat BI 使用已发布本体作为封闭世界；
- 本体检索、关系导航、口径编译、SQL 语义证明与答案凭证账本共同防止幻觉；
- `run_sql` 与手动执行复用只读与权限门；
- Data App 支持数据源、Dataset、Panel/Dashboard、预览、发布、版本、分享、公开与嵌入；
- Agent 可提出指标/标签、任务、任务链、面板、看板、数据源和本体草稿提案；
- Agent 只提案，真正写入和执行由用户确认动作触发。

---

## 13. 治理规约与形式化校验

### 13.1 治理规约

`app/governance/` 提供版本化 Policy Pack，驱动：

- Agent 事前约束；
- Validation Gate；
- 生成期命名/元数据体检；
- 存量 re-lint；
- 校验报告规约版本戳。

### 13.2 本体形式化不变式

`services/ontology_formal.py` 在发布前检查：

- derivation 无环；
- 口径 AST 可解析、引用可解；
- 聚合字段语义一致；
- 业务关系基数可识别。

### 13.3 SQL 语义证明

`ontology_projection.py`、`semantic_navigator.py`、`sql_soundness.py` 与 MetricCompiler 共同验证：

- 表/列引用存在；
- JOIN 有本体关系和可解析键；
- 聚合不存在不可接受扇出；
- 语义类型支持对应运算。

---

## 14. 已知边界

1. 当前 DIM/DWD/ADS 是分层投影，不是完整维度模型；
2. 主键证据不足时不发强制约束，真实 profiling 后应增强唯一性证明；
3. SyncSpec 的关键源 STG 保全尚未真实生成副本；
4. 外部 DataHub/Airflow/Flink/数仓能力需按部署环境验证；
5. “仅产出”与“真实执行成功”必须在更上层交付状态中继续区分；
6. 复杂 N:N、事实粒度、SCD 自动设计、度量加性等由对话式建模优化计划补齐；
7. 真实 ERPNext/Odoo Benchmark 尚需完成端到端投递和对照报告。

---

## 15. 测试与基线

2026-08-22 当前工作树：

```text
backend: 1542 passed, 3 skipped
alembic: bacbc3c392ad (single head, current=head)
frontend lint: P0 起始时 1 error / 23 warnings；error 已在 P0 修复，warnings 作为存量记录
```

重点测试：

- RBAC：`test_rbac.py`
- 制品状态机：`test_agent_pipeline.py`
- 四类 Agent：`test_agent_implementations.py`
- 方言与生成器：`test_dialect_adapter.py`、`test_warehouse_generator.py`
- Flink 执行：`test_transform_metric_executor_flink.py`、`test_sync_executor_chain.py`
- 任务链：`test_task_pipeline.py`、`test_pipeline_compiler.py`、`test_pipeline_dag_topology.py`
- Data App：`test_data_app.py`、`test_data_app_phase2.py`
- 形式化校验：`test_ontology_formal.py`、`test_sql_soundness.py`

统一回归：

```bash
cd backend && .venv/bin/pytest -q
cd frontend && npm run lint && npm run build
```

---

## 16. Doris 重构实施状态（2026-08-23）

已完成的基础收敛：

- `DEFAULT_ENGINE` 与新物化契约默认值已切换为 `doris`；新 materialize/transform/metric/sync 规格默认不再生成 Hive；
- `DataSource` 增加显式 `purpose`、`is_default_warehouse`、`enabled`，不再把 `catalog_name` 作为新路由事实源；
- 新增 `DorisWarehouseConfig`、`OntologyWarehouseDeployment`、`WarehouseObjectProjection` 的模型与 Alembic 迁移；
- 新增 `query_routing.py` 的 Doris-only readiness/receipt 门禁；无源对象物化只推进 `schema_ready`，源对象同步成功后直接推进统一表 Projection 为 `queryable=true`；
- 增加 Doris warehouse policy 与 Gate 约束；显式默认 Doris 后新 materialize/transform/metric 制品不得使用其他数仓引擎；
- 前端物化引擎选择已收敛为 Doris，旧引擎保留只用于历史读取/迁移期间审计。

Phase 2 已完成的接入基础：

- 新增版本化 `IngestionContract` 与 API，显式绑定 business-source DataSource、默认 Doris 和 ODS 物理表；
- 新 Doris sync 只允许写 `ods*` 数据库，Flink Doris Connector 的 `FENODES` 从 Airflow Connection extra 注入；
- full 使用 ODS 正式表 + staging + Doris atomic replace；incremental 使用有界 JDBC 水位 batch；CDC 使用 detached 流作业、checkpoint/savepoint 与真实 Flink Job ID；
- IngestionContract/Projection 状态只由 Airflow task 最终态和 Doris 表验证推进，提交成功不等于 data ready；无独立加工的源对象以同步 ODS 表直接 serving，需要加工时再切换到独立服务表；
- detached CDC 的 BashOperator 最终输出结构化真实 Flink Job ID；缺 Job ID 则任务失败。`flink_rest_endpoint` 由 Web/DB 配置，健康 API 查询 Flink REST 并推进 running/failed/stale；
- reader DSN 不再通过 Doris 配置 API 回显，新增 Doris 时 Web 表单同时配置 9030 SQL 与多 FE 8030 fenodes；
- Data Agent system prompt、query skill 与任务提案工具已固化 Doris-only 边界；sync 提案缺业务源/默认 Doris/ODS 或 incremental/CDC 必填参数时会在提案阶段被确定性拒绝。

Phase 3 已完成的 Doris Transform：

- 新增 `doris_sql_dag_builder.py` / `doris_job_runner.py`，Airflow DAG 只使用 Doris SQL Operator 与 `SQLCheckOperator`，不含 BashOperator/flink run；
- TransformExecutor 不再 import Flink runner/generator，只读当前 ontology version 的 ready ODS Projection；
- 全量 transform 采用 drop/create staging → Doris INSERT SELECT → 主键/非空质量闸门 → `REPLACE WITH TABLE`；
- Transform dry-run 与执行共享同一份 Doris SELECT/清洗规则编译结果；只有 Airflow 最终 success 才推进 Projection `transform_status=ready/queryable=true`；
- Transform Spec/前端已移除 streaming、source alias、parallelism、queue、checkpoint 等 Flink 字段。

Phase 4 已完成的 Doris Metric：

- MetricExecutor 不再 import Flink，metric/tag/rule 统一通过 `MetricCompiler(dialect="doris")` 编译；
- MetricCompiler 的语义对象通过当前 ontology version 的 ready/queryable Object Projection 映射为 Doris serving 表；
- ADS DDL 与 SQL 结果列共享 `result_column_specs()`，写入 ADS staging，质量检查后 atomic replace；
- 新增 `WarehouseLogicProjection`，只有 Airflow 最终 success 才进入 ready/queryable；
- Metric Spec/前端已移除 streaming、parallelism、queue、checkpoint 等 Flink 参数。

能力感知提示词已重新开放 materialize/sync/transform/metric，但 metric 必须引用已发布且形式化的 BusinessLogic。主 system prompt 已精简为中性能力说明；上游网关仅在返回明确 `Invalid prompt ... flagged` 时使用最小 system prompt 单次重试，避免动态本体卡导致误判。

Phase 5 已完成的 Doris-only 查询收敛：

- Data Agent 工具已删除 `list_catalogs` 与 `run_sql.target`；业务源/Cube/catalog/更新时间不参与选源；
- SQL 语义证明得到的每个对象必须逐一命中当前已发布 ontology version 的唯一 queryable Projection，不能再用“至少一张表 ready”放行整个 SQL；
- Query Gateway 从 Projection 生成物理表/字段映射，`DataSource.mapping_json` 仅作历史元数据，不再是查询权威；
- Data Agent 回执包含 Doris datasource、Deployment、物理表、Projection、同步水位和 stale 标识；
- Data App、Widget/Public preview、画像与 Ontology Ladder 不再执行保存的业务源/Cube DataSource；默认 Doris/Deployment/Projection 不就绪时 `execution_blocked`，不 fallback 到业务源或 Mock；
- 活跃 Phase 6 批次在步骤 10 审批前只允许 shadow 验证，结果不得进入最终用户答案。

Phase 6 已完成的控制面与运行时清理：

- 新增 `WarehouseMigrationBatch` / `WarehouseMigrationEvidence` 与 Alembic `d3e4f5a6b7c8`；
- 1–15 步只能顺序推进；失败批次 blocked；步骤 10 审批、步骤 12 pause 旧 DAG 只能走专用动作；
- 切流前强制回滚演练、指定审批人/回滚负责人和观察窗口；
- shadow compare 只返回哈希/计数，不保留原始业务结果；
- 新 materialize/sync Spec 与执行目标一律为唯一默认 Doris，非 Doris 仅可用于历史产物回看；
- 历史成功 Artifact/receipt 不原地改写，Airflow 最终态以独立迁移证据记录。

尚未完成且禁止宣称完成：独立 CDC 周期健康检查 DAG、Airflow Connection 自动写入、真实生产 Doris/Flink Connector/Airflow 集成验证，以及生产步骤 1–15。当前应用数据库没有默认 Doris，生产切流为 **NO-GO**。

## 17. 后续主线

下一阶段不再扩张零散写侧类型，按以下顺序收敛：

1. `ModelingCase`：需求、上下文、模型、计划与验收的权威流程；
2. `DimensionalModel`：业务过程、事实粒度、维度、键、SCD、加性、桥接；
3. `LogicBundle`：指标/标签/规则批量编译与确认；
4. `DeliveryPlan`：编译到现有 Artifact/Pipeline；
5. 真实 Benchmark 与生产收口。

执行方案见：`CONVERSATIONAL_ONTOLOGY_MODELING_OPTIMIZATION_PLAN.md`。
