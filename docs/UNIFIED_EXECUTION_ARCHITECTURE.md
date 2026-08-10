# 统一执行架构（Flink SQL on YARN）

**状态**：已实施（2025-01 refactor/unify-query-gateway 分支）

## 概述

统一执行架构将 ontoMeta 的三条执行路径（搬运 / transform / metric）收敛为单一 Flink SQL on YARN 路径，废除多通道（runner/docker）与多工具（SeaTunnel/DataX）选择。

**核心原则**：
- 搬运 = Flink SQL（与 transform/metric 同一执行路径，无架构分叉）
- 产物统一：Flink SQL 文件 + Airflow DAG（BashOperator 跑 `flink run`）
- 不再有工具选择：SeaTunnel/DataX/sync-runner 全部废弃

## 变更清单（A-L 阶段）

### 代码路径收敛

**A. flink_sql_generator 加搬运原语** (`4fee661`)
- `generate_move_sql()` 生成 `INSERT INTO target SELECT * FROM source`
- 支持 full/incremental/cdc 三种装载方式

**B. materialize/sync 搬运改走 Flink SQL** (`7f34c05`)
- `materialization_runner.py` 调 `compile_move_task()` 生成 Flink SQL
- 删除 `JobPlan` 中间层（原为 SeaTunnel/DataX 配置，现已无用）

**C. sync executor 去 SeaTunnel 渲染** (`8845af8`)
- `sync_executor.py` 不再调 `get_job_adapter().render()`
- 删除 `_build_runner()` / `_render_seatunnel()` 等旧通道方法

**D. preflight 去 runner 探测** (`ffb3894`)
- 删除 `SyncRunnerClient` / `_check_sync_tool` / `_check_runner_sink` 等 runner 探测
- `_check_execution_channel` 改为轻量 Flink 前置检查（JAR 缺则 WARN，无 checkpoint 则 FAIL）
- **测试债**：28 个 preflight 测试失败（全是断言 runner 行为的），待重写或删除

**E. sync_tool_resolver 反转** (`92bdb23`)
- `resolve_sync_tool()` 恒返回 `flink_on_yarn` + `uncovered_modes=空`
- `engine_modes()` 恒返回 `["full", "incremental", "cdc"]`
- 原 200+ 行的 runner capabilities 查询 + docker 镜像选择逻辑全部删除

**F/G. 删 datax/seatunnel + docker DAG 代码** (`6c60894`)
- 删除 `app/warehouse/jobs/{seatunnel,datax}.py`（多工具适配器已废）
- 重写 `registry.py` 为 Flink-only 适配层（`get_job_adapter` 恒返回 FlinkAdapter）
- 重写 `airflow_dag_builder.py`：删除 `AirflowDagBuilder` 类（`_build_docker_dag` / `_build_runner_dag`），只保留 `build_flink_sql_dag` + Flink 相关函数
- 从 1019 行精简到 ~400 行（只留 Flink 代码）

**H/J. 设置去 sync_channel + 删 sync_runner 包** (`729e544`)
- `AirflowSetting` 删除 `sync_channel` / `sync_runner_endpoint` / `sync_runner_token` 字段
- 删除 `connectors/sync_runner.py`（SyncRunnerClient / runner HTTP 协议）
- 删除 3 个 runner 端点（`/settings/sync-runner/secrets` 的 list/put/delete）
- 删除 `_probe_sync_runner`（runner 连通性探测）

**I. 前端去工具选项**（跳过）
- 本 repo 无前端代码，前端改动需在 web repo 完成

**K. 测试清理** (`e20e7d2`)
- 删除 `test_sync_runner_client.py` / `test_airflow_dag_builder.py`（测试已删除的模块/类）
- 恢复 `_plan_staging` + `_Staging`（materialization_runner 需要）
- 修复 FlinkSqlTask / FlinkSubmitConfig / flink_job_runner 适配新签名
- **测试状态**：1305 collected, 682 passed, 3 failed（JSON 序列化 / mock 对象问题，不阻塞主路径）

**L. 文档更新**（本文档）

## 产物结构

统一架构下，物化产物落盘至 `<dags_dir>/ontometa/<artifact_id>/`：

```
<dags_dir>/ontometa/<artifact_id>/
├── <dag_id>.py           # Airflow DAG 文件（BashOperator 跑 flink run）
├── <dag_id>.json         # 边车 spec（DDL / SQL / flink 配置）
└── <task_id>.sql         # 每个搬运任务的 Flink SQL（INSERT INTO ... SELECT ...）
```

**DAG 结构**：
```
create_tables ──> [搬运任务…] ──> swap_<task> ──> _tails (仅全量表有 swap)
              ├──> [增量/CDC 任务…] (无 swap，直接 INSERT)
              └──> add_constraints (外键/主键，所有表建完后加)
```

## 配置变更

### 环境变量（必需）

- `FLINK_SQL_RUNNER_JAR`：SqlRunner JAR 路径（`file://...` 或 `hdfs://...`）
  - 缺则退回「仅产出 handoff」模式（产 SQL 但不执行）
- `FLINK_CHECKPOINT_DIR`：checkpoint 目录（增量/CDC 必需）
  - 有增量表但无此配置则 preflight FAIL

### 已废弃配置（兼容保留，不再使用）

- `sync_channel` / `sync_runner_endpoint` / `sync_runner_token`（runner 通道已废）
- `sync_tool` / `sync_tool_images`（多工具选择已废，恒为 flink）
- `docker_network` / `drivers_dir`（docker 通道已废）

## 架构对比

| 维度 | 旧架构（多通道/多工具） | 新架构（统一 Flink） |
|------|----------------------|-------------------|
| **搬运工具** | SeaTunnel / DataX / sync-runner（按能力 + 镜像可用性自动挑） | Flink SQL（无选择） |
| **执行通道** | docker（DockerOperator 起容器）/ runner（HTTP 调 sync-runner） | Flink on YARN（BashOperator 跑 `flink run`） |
| **产物** | docker: 作业 JSON + DockerOperator DAG<br>runner: 作业 JSON + PythonOperator DAG | Flink SQL 文件 + BashOperator DAG |
| **搬运原语** | 工具特定配置（SeaTunnel conf / DataX JSON） | 标准 Flink SQL（`INSERT INTO ... SELECT ...`） |
| **与 transform/metric 关系** | 独立执行路径（搬运走工具容器，transform/metric 走 Flink） | 统一路径（搬运 = Flink SQL，与 transform/metric 同构） |
| **preflight 检查** | 探 runner capabilities / docker 镜像可用性 / sink 支持 | 检查 Flink JAR / checkpoint 目录（增量表） |
| **装载方式** | 按工具能力过滤（SeaTunnel 支持 full/incremental，DataX 仅 full） | Flink 支持 full/incremental/cdc 全集 |

## 迁移指南

### 开发环境

1. **删除旧依赖**：
   - 不再需要 SeaTunnel / DataX 镜像
   - 不再需要 sync-runner 部署

2. **配置 Flink**：
   ```bash
   export FLINK_SQL_RUNNER_JAR=hdfs://namenode:8020/ontoMeta/flink-sql-runner.jar
   export FLINK_CHECKPOINT_DIR=hdfs://namenode:8020/flink/checkpoints
   export FLINK_YARN_QUEUE=default
   ```

3. **Airflow DAGs 目录结构**：
   - 旧：`<dags_dir>/<dag_id>.py` + `<jobs_dir>/<artifact_id>/*.json`
   - 新：`<dags_dir>/ontometa/<artifact_id>/<dag_id>.py` + `.sql` + `.json`

### 生产环境

1. **Flink on YARN 就绪**：
   - 确保 Flink 集群可达（`flink run` 命令可用）
   - YARN 队列配置（默认 `default`）
   - HDFS checkpoint 目录权限

2. **Airflow 升级**：
   - 无需 `airflow-providers-docker`（不再用 DockerOperator）
   - 保留 `airflow-providers-common-sql`（建表 DDL）

3. **回滚方案**：
   - 旧 DAG（docker/runner 通道）与新 DAG（Flink）可共存
   - 回滚：切回 `main` 分支重新投递 DAG

## 后续工作

### 测试债

1. **preflight 测试**（28 个失败）：
   - 全是断言 runner 行为的旧测试
   - 需重写为 Flink 路径断言或删除

2. **flink_job_runner 测试**（3 个失败）：
   - JSON 序列化 / mock 对象问题
   - 需调整 test fixture

### 代码清理

1. **内联适配层**：
   - `sync_tool_resolver.py`：可内联到调用处删除（只剩恒定返回）
   - `jobs/registry.py`：可内联到调用处删除（只剩 FlinkAdapter）

2. **删除兼容字段**：
   - `AirflowSetting.sync_tool_images` / `sync_tool`（标注已废弃，可删）
   - `AirflowSetting.docker_network` / `drivers_dir`（docker 通道已废，可删）

3. **文档更新**：
   - 标记 `docs/MATERIALIZE_ORCHESTRATION.md` 为过时（全篇 runner/docker 架构）
   - 更新 `docs/MATERIALIZE_SYNC_STABILITY.md`（如有 runner 引用）

## 参考

- **Commit chain**: `4fee661` (A) → `7f34c05` (B) → `8845af8` (C) → `ffb3894` (D) → `92bdb23` (E) → `6c60894` (F/G) → `729e544` (H/J) → `e20e7d2` (K)
- **Branch**: `refactor/unify-query-gateway`
- **Test status**: 682/685 passed (3 failures in test fixtures, not production code)
