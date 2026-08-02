# 物化编排改造 · 实施计划

> 配套设计见 `MATERIALIZE_ORCHESTRATION.md`（方案），本地验证栈见 `docker/orchestration/README.md`。
> 本文是**执行计划**：拆到文件级任务、验收标准、测试、以及每阶段需要哪些本地服务。
> 「已核实」= 我在本机实测过；「⚠ 待验证」= 依赖尚未起来的服务，起栈后按清单逐条确认。

---

## 0. 本机基线（已核实，2026-08）

`docker ps` + `GET :8080/config` 实测结果——**已有的比预想多，缺的只有三样**：

| 已在跑 | 版本/端口 | 对本计划的意义 |
|---|---|---|
| DataHub quickstart | v1.6.0；GMS `:8080`、前端 `:9002`、Kafka `:9092`、OpenSearch `:9200` | M11 血缘验证的目标，**不用自己搭** |
| ERPNext + MariaDB | v16.28.0；MariaDB `:3308` | 真实源库；本体「数据域-ERP-全量」的 `source_ref` 就指向它（如 `_3214abce8e7be3d7.tabAddress`） |
| Bigtop Manager | bm-1/2/3 + pg；`:18080` | 目标数仓备选方案（部署 Hive）时用 |

| 缺 | 由 `docker/orchestration` 提供 |
|---|---|
| Airflow | `:8081`，含 `acryl-datahub-airflow-plugin` |
| SeaTunnel | 一次性任务容器 + 常驻调试容器 |
| 目标数仓 | Doris all-in-one `:9030`（三案见 README） |

**两条硬约束（实测得出，直接决定排期）**：

1. **镜像拉取极慢**：`alpine:3.20`（4MB）实测 **6 分 03 秒**。airflow/seatunnel/doris 直连拉不下来，
   **必须先配镜像源**。`make orch-preflight` 会测速拦截。这是整个计划的前置阻断项。
2. **PyPI 可达**（首字节约 9s），但慢到不适合每次容器启动 `pip install` → Airflow 镜像用 Dockerfile 预装插件。

**版本对照（已核实）**：DataHub v1.6.0 → `acryl-datahub-airflow-plugin==1.6.0` 要求
`apache-airflow>=2.5.0,<4.0.0`（2/3 都行）；最新补丁 `1.6.0.17` 收紧为**仅 Airflow 3**。
故取 Airflow 2.10.5 + 插件 1.6.0。

---

## 1. 阶段总览

| 阶段 | 内容 | 需要的本地服务 | 能否离线做 | 状态 |
|---|---|---|---|---|
| **M9** | JobSpec + SeaTunnel Adapter + JobPlanner | 无 | ✅ 完全可以 | ✅ 已交付 |
| **M10** | DAG 生成 + Airflow 触发/回读 + 前端改造 | Airflow（+ 任一目标库） | 代码可，联调不可 | ✅ 代码已交付，⏳ 联调待镜像 |
| **M11** | 血缘注册（插件为主 + 兜底 emitter） | Airflow + DataHub（已有） | ❌ | 待办 |
| **M12** | CDC/水位、质量校验、DataX/Flink Adapter | 全栈 | ❌ | 待办 |

**M9 交付物**：`app/warehouse/jobs/{base,seatunnel,registry}.py`、`app/services/job_planner.py`，
测试 `test_job_planner.py` + `test_sync_tool_adapters.py`（22 条）。
拿真实 ERP 本体（734 对象）跑通，并借此发现并修掉 M3 的一个既有 bug：
`Property.source_field_ref` 存的是 DataHub schemaField 标识（`<datasetUrn>#字段名`）而非裸列名，
`_field_refs` 一直当列名用，导致生成的 ETL SQL 里是 ``SELECT `urn:li:dataset:(...)#doctype` ``——必然报错。
修在共用源头 `_physical_field()`，M3 与 M9 一并修好。

**M10 交付物**：`app/connectors/airflow.py`、`app/services/airflow_dag_builder.py`、
`materialization_runner` 的 `execute_mode`、`airflow_settings` 表与设置页「物化调度」页签、
弹窗改「提交并运行」+ DagRun 轮询。测试 `test_airflow_connector.py`（7）、
`test_airflow_dag_builder.py`（10）、`test_airflow_settings_api.py`（5）、
runner 编排用例（4）。

**M9 不依赖任何新服务**——在被镜像拉取卡住之前就能把方案最核心的部分（生成的作业配置对不对）
验证完。这也是它排第一的原因，不只是依赖顺序。

---

## 2. M9 · JobSpec + 搬运工具 Adapter（无外部依赖）

**目标**：把「本体 + 物化契约 + 逻辑计划」编译成与工具无关的 `JobSpec`，再渲染成 SeaTunnel 配置。
不执行、不连接任何外部系统。

### 任务

| # | 任务 | 文件 |
|---|---|---|
| 9.1 | `JobSpec` 数据类：源(连接别名/库/表)、目标(别名/库/表)、列映射、模式(full/incremental/cdc)、分区键、水位占位符、并行度 | 新 `app/warehouse/jobs/base.py` |
| 9.2 | SeaTunnel 渲染器 `render(JobSpec) -> dict` | 新 `app/warehouse/jobs/seatunnel.py` |
| 9.3 | 注册表 `get_job_adapter(tool)`，比照 `app/warehouse/registry.py` 的写法 | 新 `app/warehouse/jobs/registry.py` |
| 9.4 | `JobPlanner`：逻辑计划 + 契约 → `JobSpec[]`，依赖序复用 `generate_dag()`；无 `source_ref`、跨源不可达等情形进 `unsupported` | 新 `app/services/job_planner.py` |
| 9.5 | 列映射取值口径与 M3 完全一致（`Property.source_field_ref`，缺失回退同名），**不另写一套** | 复用 `warehouse_generator._field_refs` |

### 验收标准

- fixture 本体产出完整 `JobSpec[]`；**两次生成逐字节一致**（沿用 M3 幂等要求）。
- 渲染出的配置里**没有任何凭据**，只有数据源别名（沿用既有不变量，Validation Gate 的凭据扫描同样生效）。
- 不可搬运项显式进 `unsupported`，不静默丢。
- 列映射与 M3 生成的 ETL SQL 对同一实体给出**相同的列对应关系**（防两套逻辑分叉）。

### 测试

`backend/tests/test_job_planner.py`、`backend/tests/test_sync_tool_adapters.py`
——纯单测，无外部依赖，`pytest -q` 直接跑。

---

## 3. M10 · DAG 生成 + Airflow 触发/回读

**目标**：`JobSpec[] + DDL` → Airflow DAG 文件；ontoMeta 触发 DagRun 并回读状态。

### 任务

| # | 任务 | 文件 |
|---|---|---|
| 10.1 | DAG 生成：`create_tables`（跑 M3 的 DDL）→ `sync_<表>`（DockerOperator 起 SeaTunnel）→ 按 `generate_dag()` 的依赖序连边；任务上声明 inlets/outlets（为 M11 铺路） | 新 `app/services/airflow_dag_builder.py` |
| 10.2 | Airflow REST 客户端：触发 DagRun、查状态、取日志 URL；错误封装与可注入 client 比照 `app/connectors/bigtop_manager.py` | 新 `app/connectors/airflow.py` |
| 10.3 | `materialization_runner.run()` 增 `execute_mode`：`orchestrated`（默认）/ `direct`（现有直连落库，**保留**给无 Airflow 的开发机） | 改 `app/services/materialization_runner.py` |
| 10.4 | 制品回执增 `dag_id`/`dag_run_id`/`state`/`log_url`；新增状态查询端点供前端轮询 | 改 `app/agents/executors/materialize.py`、`app/api/warehouse.py` |
| 10.5 | Airflow 连接配置进 settings（endpoint/鉴权/DAG 投递目录），比照既有 `DatahubSetting`/`CubeSetting` 的 DB-backed 做法 | 改 `app/services/settings_service.py` |
| 10.6 | 前端：按钮语义改「提交并运行」；回执区改 DagRun 状态轮询 + Airflow 跳转；`direct` 模式显式标注「开发模式」 | 改 `frontend/src/components/MaterializeModal.tsx` |
| 10.7 | DagRun id 用制品 id → 重复提交天然幂等 | 10.2/10.3 内 |

### 验收标准

- 生成的 DAG 文件能被 Airflow 解析（`DagBag` 无 import error）。
- 同一制品重复提交只产生一个 DagRun。
- 契约 `refresh_cron` 成为 DAG 的 `schedule`（空 cron → `schedule=None`）——**上一轮做的定时策略选择器到此才真正生效**。
- 增量装载的水位由 Airflow `data_interval_start` 注入，替换掉现在无人注入的 `:watermark`。
- `execute_mode=direct` 时行为与今天完全一致（现有 7 条 runner 测试不改语义，改标记）。

### 测试

- `backend/tests/test_airflow_dag_builder.py`：用 `DagBag` 解析生成的 DAG（需 airflow 包，标 `integration`）。
- `backend/tests/test_airflow_connector.py`：httpx `MockTransport`，无需真实 Airflow（比照 BM 连接器测试）。
- 集成测试统一打 `@pytest.mark.integration`，默认 `-m "not integration"` 跳过，CI 与本地快跑不受影响。

### 本地验证

```bash
make orch-up-airflow          # 需先过 preflight（镜像源！）
# 起栈后先核实 REST 版本，不要照抄文档：
curl -s localhost:8081/openapi.json | grep -o '/api/v[0-9]*/dags/{dag_id}/dagRuns'
```

---

## 4. M11 · 血缘自动注册

**目标**：一次物化跑完，DataHub 里能查到 `源表 → 目标表` 的血缘和对应的 DataJob。

### 任务

| # | 任务 | 文件 |
|---|---|---|
| 11.1 | DAG 任务的 inlets/outlets 填真实 Dataset URN：源侧直接用 `ObjectType.source_ref`，目标侧按目标平台 + `库.表` 构造 | 改 `airflow_dag_builder.py` |
| 11.2 | 兜底 emitter：DagRun 成功后由 ontoMeta 直接发血缘（复用 M7 的 GraphQL 通道），用于插件缺位/版本不匹配 | 新 `app/services/lineage_emitter.py` |
| 11.3 | 列级血缘（`fineGrainedLineages`）：映射表 M3 已有，直接产出 | 11.2 内，⚠ 视 DataHub 版本支持度 |

### 验收标准

- 跑一次物化后，在 DataHub 前端（`:9002`）能看到目标表的 upstream 指向 ERPNext 源表。
- 插件与兜底 emitter 产**同一份 URN**，重复上报幂等（不产生重复边）。

### 测试

`backend/tests/test_lineage_emitter.py`——mock GraphQL 断言请求体形状，比照 `test_datahub_writeback.py`。

---

## 5. M12 · CDC / 水位 / 质量校验 / 其他 Adapter

- `load_strategy=cdc` 的契约路由到 CDC 实现（SeaTunnel-CDC 或 Flink CDC）。
- 每张表的 sync 任务后可选挂质量校验任务（行数比对/主键唯一性）。
- DataX / Flink SQL Adapter 补齐，验证「工具可插拔」这条设计不是空话。

---

## 6. 端到端冒烟（全栈起来后的验收路径）

1. ontoMeta 里对 ERP 本体的某个对象点「提交并运行」。
2. `docker/orchestration/dags/` 出现新 DAG 文件，内容可读、可 diff。
3. Airflow（`:8081`）里 DAG 出现并被触发，`create_tables` 先跑。
4. 目标库里表已建，且**表注释是本体的业务名**（验证 DDL 没被搬运工具绕过——这是关键检查点）。
5. `sync_<表>` 任务跑完，目标表有数据，行数与源表一致。
6. ontoMeta 弹窗回执区轮询到 `success` 状态，能跳 Airflow 日志。
7. DataHub（`:9002`）目标表页面出现 upstream 血缘与 DataJob。
8. 契约配了 cron 的实体，DAG 的 schedule 与之一致；等一个周期看是否自动跑。

---

## 7. 风险与回退

| 风险 | 应对 |
|---|---|
| **镜像拉不动**（当前最大阻断） | 配镜像源；`make orch-preflight` 前置拦截；M9 全程不受影响可先做 |
| Airflow 2 vs 3 的 REST 差异 | 影响面限于 `connectors/airflow.py`；起栈后用 `/openapi.json` 实测确认，不照抄文档 |
| 插件与 Airflow/DataHub 版本三方耦合 | 已核实 1.6.0 兼容 2.5+/3.x；M11 有兜底 emitter，插件挂了血缘不断 |
| DDL 被搬运工具绕过丢注释 | 建表固定为 DAG 首个任务、跑 M3 的 DDL；SeaTunnel 关 auto-create schema；冒烟第 4 步专门查这个 |
| 目标数仓镜像太大 | 三案可选（Doris / BM 部署 Hive / MySQL 只验机制），见 README |
| 现有测试被推翻 | 现有 7 条 runner 测试保留为 `execute_mode=direct` 用例，新增 orchestrated 用例，不删覆盖 |
| 本机资源 | 14 CPU / 47GB，DataHub+ERPNext+BM 已占一部分；Doris all-in-one 建议单独起、用完即停 |

---

## 8. 起栈后待核实清单

方案文档里标「⚠ 需实施前验证」的条目，逐条落成可执行动作：

| # | 待核实 | 怎么验 |
|---|---|---|
| 1 | Airflow REST 版本与触发路径 | `curl localhost:8081/openapi.json` |
| 2 | 插件 × Airflow × DataHub 兼容 | 跑 hello DAG，看 DataHub 是否出现 DataFlow/DataJob |
| 3 | 字段级血缘支持度 | 发一条 `fineGrainedLineages` 看 GMS 是否接受 |
| 4 | SeaTunnel 配置格式与 Sink 能力 | 容器内 `seatunnel.sh --config` 跑一个最小作业 |
| 5 | DAG 文件投递通道 | 本地用挂载卷已验证；生产需确认 git-sync 或共享卷 |
| 6 | BM 纳管的 SeaTunnel 版本 | BM UI（`:18080`）查 Extra stack 版本 |
