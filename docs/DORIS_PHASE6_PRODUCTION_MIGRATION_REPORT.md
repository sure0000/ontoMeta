# Doris Phase 6 生产迁移执行报告

> 执行日期：2026-08-22
> 执行结论：**BLOCKED / 未切流**
> 阻断步骤：**1. 建立生产默认 Doris 与最小权限身份**
> 安全结论：未执行生产 DDL、未同步数据、未返回 shadow 结果、未暂停旧 DAG、未启用任何业务源 fallback。

## 1. 执行范围与规则

本次按 `docs/DORIS_WAREHOUSE_REFACTOR_PLAN.md` Phase 6 的 1–15 顺序执行。遵守以下停止规则：

1. 任一阻断项失败立即停止后续生产动作；
2. Data Agent、Data App、画像和预览不得 fallback 到 ERP/CRM/MySQL/PostgreSQL/Cube；
3. shadow query 只生成差异摘要和哈希，不向最终用户返回或持久化业务结果行；
4. `GovernanceArtifact` 与 `execution_receipt_json` 历史记录不原地重写；
5. 未有真实 Airflow/Flink/Doris 最终态证据时不得记录成功；
6. 未有指定审批人、观察窗口和回滚负责人时不得切流。

## 2. 迁移批次与时间线

### 2.1 迁移批次

| 项 | 值 |
|---|---|
| 生产迁移批次 ID | **未创建** |
| 原因 | 当前运行数据库没有默认 Doris；用户未提供可验证的审批人/回滚负责人 publisher principal id 及旧/新 DAG ID 清单。为避免伪造审计记录，在步骤 1 通过前不创建生产批次 |
| 代码控制面 | 已新增 `warehouse_migration_batches` / `warehouse_migration_evidence`，真实环境补齐前置后可创建批次 |
| 当前应用数据库 | `backend/ontometa.db`，Alembic `d3e4f5a6b7c8 (head)` |

### 2.2 时间线

| 时间（UTC） | 动作 | 结果 |
|---|---|---|
| 2026-08-22 07:41 | 阅读 Phase 6 方案、开发原则与 as-built，检查工作区 | 发现已有 Phase 0–5 未提交改动，保留并增量实施 |
| 2026-08-22 07:45 | 检查应用数据库与 Alembic | 数据库在旧 head，随后升级至 `d3e4f5a6b7c8`；发现无默认 Doris |
| 2026-08-22 | 检查非敏感 DataSource 状态 | 仅 `macmini-mysql`、`pg` 两个 `business_source`；默认 Doris=0 |
| 2026-08-22 | 按停止规则判定步骤 1 | **BLOCKED**：Doris config=0、Deployment=0、Projection=0、IngestionContract=0 |
| 2026-08-22 | 实施 Phase 6 控制面与 Phase 5 缺口收敛 | 完成严格步骤状态机、审批、观察、回滚、shadow 隐藏、DAG pause、对象级 Projection 门禁 |
| 2026-08-22 | 全量测试 | 后端 `1572 passed / 3 skipped`；前端 lint 0 error / 23 存量 warning；前端 build 成功 |
| 2026-08-22 | 生产切流判定 | **NO-GO**；未执行步骤 2–15 的生产副作用 |

## 3. 严格步骤执行状态

| # | 步骤 | 状态 | 证据/原因 |
|---:|---|---|---|
| 1 | 生产默认 Doris 与最小权限身份 | **BLOCKED** | `data_sources` 中默认 Doris=0；`doris_warehouse_configs`=0；无法验证 reader/DDL/ETL/Flink 四身份 |
| 2 | 当前 ontology version Deployment/Projection | NOT STARTED | 步骤 1 阻断；部署记录=0 |
| 3 | 物化 ODS/DIM/DWD/DWS/ADS | NOT STARTED | 无 Doris 目标与 DDL Connection |
| 4 | 全量同步 ODS | NOT STARTED | IngestionContract=0；无 Doris fenodes/Flink Connection |
| 5 | Doris transform | NOT STARTED | 无 ready ODS Projection |
| 6 | Doris metric/tag/rule | NOT STARTED | 无 ready 上游 Projection |
| 7 | 全维度对账 | NOT STARTED | 无 Doris 数据集可比较 |
| 8 | CDC 延迟/水位/更新删除/恢复 | NOT STARTED | 无 CDC Contract/Job ID |
| 9 | Data Agent shadow query | NOT STARTED | 无可查询 Doris Projection；未向用户返回任何 shadow 数据 |
| 10 | 审批切 Doris-only | **NO-GO** | 步骤 1–9 未全部通过；无可验证审批动作 |
| 11 | 稳定观察窗口 | NOT STARTED | 未切流 |
| 12 | 停止旧周期 DAG | NOT STARTED | 未切流；旧 DAG 未暂停/删除 |
| 13 | 删除运行时 fallback | 代码静态收敛完成；生产动作未开始 | 新运行任务已 Doris-only；历史读取兼容保留，详见第 8 节 |
| 14 | 历史 Artifact/receipt 只读审计 | 代码不变量已保留；生产验收未开始 | 无原地重写逻辑；生产批次未执行 |
| 15 | 更新 as-built | 本次代码状态已更新 | 生产 as-built 最终态仍标记 BLOCKED，不能标记完成 |

## 4. 生产前置状态快照

应用权威数据库的非敏感快照：

```text
data_sources                         2
  - macmini-mysql mysql    business_source default=false enabled=true status=ok dsn_set=true
  - pg            postgres business_source default=false enabled=true status=ok dsn_set=true
doris_warehouse_configs             0
ontology_warehouse_deployments      0
warehouse_object_projections        0
warehouse_logic_projections         0
ingestion_contracts                 0
warehouse_migration_batches         0
```

本快照证明步骤 1 不具备执行条件。环境中存在 `FLINK_HOME` 只证明本机有 Flink 路径，不证明真实 Flink REST、Doris Connector、checkpoint/savepoint 或生产 Airflow 集成可用。

## 5. 对账报告

### 5.1 报告状态

**NOT EXECUTED / BLOCKED**。没有 Doris 正式数据，因此不得制造“0 差异”结论。

### 5.2 必检维度

以下维度已固化为 Phase 6 步骤 7 的必填报告字段，任一为空或存在 `blocking_differences` 均阻断切流：

| 维度 | 本次结果 |
|---|---|
| 表行数 | 未执行 |
| 主键覆盖率/重复数 | 未执行 |
| 必填字段空值率 | 未执行 |
| 最大/最小业务时间 | 未执行 |
| 金额 SUM | 未执行 |
| 数量 COUNT | 未执行 |
| 维度分布 | 未执行 |
| metric/tag/rule 结果 | 未执行 |

`POST /api/warehouse/migrations/{batch_id}/steps` 的步骤 7 必须提交上述八组结果；有任何阻断差异时批次进入 `blocked`，步骤 10 审批 API 不可调用。

## 6. Shadow query 差异报告

### 6.1 本次结果

**NOT EXECUTED / USER VISIBLE = FALSE**。

原因：没有默认 Doris、Deployment 或 queryable Projection。系统未将 shadow 查询改查业务源，也未向最终用户返回结果。

### 6.2 已实施的隐私与门禁

- `POST /api/warehouse/migrations/shadow-compare` 只返回：用例名、结果哈希、行数、matched/different；
- 原始 `legacy_result` / `doris_result` 不落迁移证据、不在响应中返回；
- 步骤 9 要求 `cases > 0`、`different = 0`、`matched = cases`；
- 活跃迁移批次在步骤 10 审批前，Data Agent/Data App 返回 `execution_blocked`，shadow 数据不得成为用户答案；
- Doris 不可用或 Projection 不全时 fail-closed，不 fallback 到业务源或 Mock。

本次摘要：

```json
{
  "cases": 0,
  "matched": 0,
  "different": 0,
  "blocking_differences": ["生产 Doris 未配置，shadow query 未启动"],
  "raw_results_retained": false,
  "user_visible": false
}
```

> `cases=0` 不是通过；步骤 9 的服务层门禁会拒绝该报告。

## 7. Airflow / Flink / Doris 最终态

| 组件 | 本次最终态 | 说明 |
|---|---|---|
| Doris | **NOT CONFIGURED** | 默认 Doris=0，DorisWarehouseConfig=0，无法探测 FE/BE/权限 |
| Flink | **NOT VALIDATED FOR PRODUCTION** | 本机有 `FLINK_HOME`；没有 IngestionContract、Job ID、水位、checkpoint/savepoint 生产证据 |
| Airflow | **NOT PROBED FOR CUTOVER** | 没有 Doris 三条 Connection 与迁移 DAG 清单；未暂停任何旧 DAG |
| Data Agent | **FAIL-CLOSED** | 工具 schema 无 `list_catalogs`/`run_sql.target`；无 Doris 时不执行业务查询 |
| Data App/Preview | **FAIL-CLOSED** | 保存的业务源/Cube `data_source_id` 不再参与执行；无 Doris 时不回退 Mock/源库 |
| Profiling/Ladder | **FAIL-CLOSED** | 必须通过默认 Doris + 当前版本对象 Projection 门禁 |

## 8. 回滚演练结果

### 8.1 生产演练

**NOT EXECUTED / BLOCKED**。未切流，不存在可合法执行的生产回滚动作；不得把单元测试冒充生产演练。

### 8.2 已实现的受控回滚契约

切流前必须提交回滚演练报告，字段包括：

```text
performed_at
owner（必须等于批次 rollback_owner）
stop_new_dags
restore_old_read_only
watermark_resume
rto_seconds
result=pass
fallback_to_business_source=false
```

切流后的受控回滚仅允许在步骤 10 完成且步骤 13 运行时清理前执行：

1. 由指定 `rollback_owner` 操作；
2. 暂停新 Doris DAG；
3. 将当前 Deployment 的 Object/Logic Projection 设为不可查询；
4. 保留 Doris 数据、Artifact、receipt 和失败证据；
5. **不会把 Data Agent 动态 fallback 到业务源**；
6. 步骤 13 后只支持版本回滚，不承诺在线源库切换。

## 9. 未清理兼容项清单

| 兼容项 | 状态 | 原因/后续动作 |
|---|---|---|
| Hive/StarRocks/Postgres 等 Dialect Adapter | 保留，历史只读 | 用于历史 Artifact/receipt 生成结果回看；不得用于新执行任务 |
| `WarehouseGenerator.generate_derivation/bundle` 历史生成 API | 保留，非执行 | 旧制品审计仍可能引用；后续可迁到独立 history namespace |
| `DataSource.mapping_json` | 保留，非查询权威 | 历史元数据兼容；Query Gateway 已改用 ontology-version Projection |
| 旧 DAG 文件物理删除 | 未执行 | 未切流，禁止提前删除；步骤 12 先 pause，步骤 13 再受控清理 |
| 历史 `GovernanceArtifact.execution_receipt_json` | 永久保留只读 | 审计不变量；不得原地改 queued 为 success |
| `docs/MATERIALIZE_SYNC_STABILITY.md` 历史 runner/SeaTunnel 设计 | 保留历史正文，已加 superseded 标识 | 不再作为实现依据 |
| `docs/UNIFIED_EXECUTION_ARCHITECTURE.md` 旧“全部 Flink”正文 | 保留历史正文，已加 superseded 标识 | 当前矩阵见本文第 11 节 |

静态清理审计结果：

```json
{
  "blocking": [],
  "remaining": [
    "Hive/StarRocks dialect adapters retained for historical Artifact/receipt rendering only",
    "historical GovernanceArtifact and execution_receipt_json retained immutable/read-only"
  ]
}
```

## 10. 全量测试结果

| 测试 | 结果 |
|---|---|
| Backend | `1572 passed, 3 skipped, 1 warning` |
| Frontend lint | 0 error，23 个存量 warning |
| Frontend build | 成功；Vite 仅报告大 chunk warning |
| Alembic | 单一 head/current：`d3e4f5a6b7c8` |
| Python compileall | 成功 |
| `git diff --check` | 成功 |

Skipped 用例与 warning 不是生产 Doris/Flink 集成通过的替代证据。真实集成测试仍因步骤 1 阻断而未执行。

## 11. 最终执行矩阵证明

### 11.1 目标矩阵

| 制品/能力 | 引擎 | 编排/执行 | 代码证明 |
|---|---|---|---|
| materialize | Doris DDL | Airflow | `materialization_runner.resolve_engine()` 强制默认 Doris |
| sync | Flink → Doris ODS | Airflow | IngestionContract + ODS 校验；Flink runner 仅由 sync 使用 |
| transform | Doris SQL | Airflow | TransformExecutor 无 Flink import；Doris SQL DAG |
| metric/tag/rule | Doris SQL | Airflow | MetricExecutor 无 Flink import；MetricCompiler(doris) |
| Data Agent query | Doris SELECT | Query Gateway | 无 target/list_catalogs；逐对象 Projection + cutover gate |
| Data App/preview | Doris SELECT | Query Gateway | 保存的 data_source_id 不参与执行；无 Doris fail-closed |
| profiling | Doris SELECT | Query Gateway | 当前 ontology/version Projection 映射 |

### 11.2 防回归测试

- `tests/test_warehouse_migration.py`：顺序、阻断、shadow 隐藏、Projection 覆盖、审批切流；
- `tests/test_list_catalogs.py`：Agent 无 catalog/source target；
- `tests/test_query_gateway.py`：只选唯一默认 Doris；
- `tests/test_agent_implementations.py`：非 Doris target/spec 被拒，transform/metric 无 Flink；
- `tests/test_data_app.py` / `tests/test_data_app_phase2.py`：Data App 不 fallback 到保存的业务源或 Mock；
- `tests/test_column_profiler.py`：画像要求 ready Projection；
- `tests/test_sync_executor_chain.py`：sync 固定业务源 → Flink → Doris ODS。

### 11.3 生产证明状态

代码矩阵证明：**PASS**。
生产执行矩阵证明：**BLOCKED**，因为不存在默认 Doris、生产 Deployment、真实 DagRun/Flink Job/查询水位证据。

## 12. 下一次执行的唯一入口

补齐以下输入后重新从步骤 1 开始，不得从步骤 2 续跑：

1. Web/DB 中唯一启用的生产 Doris DataSource；
2. 9030 SQL、8030 多 FE fenodes 与 reader secret；
3. `ontometa_reader`、`ontometa_ddl`、`ontometa_etl`、`ontometa_flink_sink` 最小权限验证；
4. Airflow DDL/ETL/Flink 三条 Connection 实测；
5. 精确 Doris/Flink/Connector/Airflow 版本；
6. 明确 `approver`、`rollback_owner`（均为已启用 publisher principal id；bootstrap token 对应 `bootstrap-admin`）及观察窗口分钟数；
7. 旧周期 DAG ID 与新 Doris DAG ID 清单；
8. 已通过的真实 Doris/Flink/Airflow 集成报告。

创建批次：

```http
POST /api/warehouse/migrations
```

步骤 2 使用 `POST /api/warehouse/migrations/ontologies/{ontology_id}/prepare-deployment` 创建 pending Deployment/Projection；materialize 提交不会提前推进 schema ready，只有 Airflow 最终 success 对账后推进。之后只能按 1–15 顺序提交证据。步骤 10、12 必须分别调用专用的审批切流和停止旧 DAG API，不能手工把状态写成通过。
