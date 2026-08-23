# Doris 统一数仓底座重构方案

> 状态：**Phase 0–5 与 Phase 6 控制面已完成代码/测试；生产 Phase 6 在步骤 1 阻断，未切流**
> 生产执行证据：`docs/DORIS_PHASE6_PRODUCTION_MIGRATION_REPORT.md`
> 文档日期：2026-08-23
> 目标：将底层数据架构统一为“业务数据源 → Flink → Doris”，由 Doris 承担数仓存储、ETL、指标计算与查询，Airflow 统一负责编排调度，Data Agent 只查询 Doris。
> 实施约束：本文是后续新会话的执行依据；在方案审核通过前，不修改业务代码、数据库结构和运行环境。

---

## 0. 文档定位

本文描述目标架构、边界、不变量、数据模型、执行链、迁移步骤、验收标准与回滚策略。

当前已建成架构以以下文档为准：

- `docs/DW_IMPLEMENTATION.md`
- `docs/UNIFIED_EXECUTION_ARCHITECTURE.md`
- `docs/MATERIALIZE_SYNC_STABILITY.md`
- `docs/TASK_PIPELINE_PLAN.md`
- `docs/DEVELOPMENT_PRINCIPLES.md`

本文获批并实施完成后，应同步更新上述文档。其中：

- `DW_IMPLEMENTATION.md` 应更新为新的 as-built 规格；
- `UNIFIED_EXECUTION_ARCHITECTURE.md` 当前的“sync/transform/metric 全部走 Flink”结论将被替代；
- `MATERIALIZE_SYNC_STABILITY.md` 中 Hive、SeaTunnel、sync-runner 等历史设计不得继续作为新架构实现依据；
- 历史文档保留审计价值，但需明确标注“已被 Doris 架构替代”。

本文不改变以下产品根原则：

1. 本体仍是业务语义的一级权威源；
2. Doris 是本体物理投影与业务数据的统一物理数仓底座；
3. LLM 只产声明式 Spec，不直接执行命令；
4. 确定性 Compiler/Executor 生成 SQL、DAG 和执行计划；
5. 未经 Validation Gate 和人工确认的治理制品不得首次执行；
6. 凭据不进入 Spec、LLM 上下文、SQL 文件、DAG 边车和执行回执；
7. 运行期连接与部署配置遵守 `DEVELOPMENT_PRINCIPLES.md`：Web 设置页配置、数据库为唯一事实源。

---

## 1. 重构目标

### 1.1 目标拓扑

```text
ERP / CRM / MySQL / PostgreSQL 等业务数据源
                    │
                    │ JDBC / CDC
                    ▼
                  Flink
           只负责采集、传输、CDC
                    │
                    ▼
             Apache Doris
       ┌────────────┼────────────┐
       │            │            │
      ODS       DIM / DWD     DWS / ADS
   原始标准投影   维度与明细     汇总与指标
       └────────────┴────────────┘
                    ▲
                    │ Doris 原生 SQL
                    │
       Transform / Metric / 数据质量
                    ▲
                    │
                 Airflow
       统一调度、依赖、重试、状态对账
                    │
                    ▼
      Data Agent / Data App / 数据预览
              全部只查询 Doris
```

### 1.2 组件职责

| 组件 | 重构后职责 | 明确不负责 |
|---|---|---|
| 业务数据源 | 原始业务事实来源 | 不供 Data Agent 直接查询；不执行数仓 ETL |
| Flink | source → Doris ODS 的 full/incremental/CDC 同步 | 不执行 Doris 内部 transform/metric |
| Doris | 唯一数仓底座；存储、清洗、关联、汇总、指标、在线查询 | 不承担调度编排 |
| Airflow | DAG 调度、任务依赖、重试、运行状态、周期编排 | 不承担业务计算语义 |
| ontoMeta | 本体、契约、声明式制品、确定性生成、校验、投递、对账 | 不在 Web 进程直接执行写侧 SQL |
| Data Agent | 基于已发布本体生成并证明 SQL，只读查询 Doris | 不选择或直连 ERP/CRM 等源库 |
| DataHub | 元数据输入、血缘和语义回写 | 不进入 Data Agent 的业务查询链路 |

### 1.3 不在本次重构范围内

1. 不把 Airflow 替换为其他调度器；
2. 不把 Flink 替换为 DataX、SeaTunnel 或自建搬运服务；
3. 不把 Doris 作为本体语义权威；本体仍是语义权威；
4. 不在首期自动使用 Doris Aggregate Key 模型；未显式证明粒度前不做激进优化；
5. 不在首期实现跨 Doris 集群联邦查询；
6. 不允许 Data Agent 通过参数临时切回业务源；
7. 不直接修改历史成功制品及其执行回执。

---

## 2. 目标架构硬不变量

以下规则必须落实为代码校验和测试，不得只写在 Prompt 或说明文档中：

1. **唯一新建数仓引擎是 Doris。**
2. **Flink 仅允许出现在 `sync` 执行链路。**
3. **`transform` 和 `metric` 只能生成、执行 Doris SQL。**
4. **`materialize` 只能面向已配置的默认 Doris。**
5. **Data Agent、Data App 查询、字段画像和数据预览最终统一查询 Doris。**
6. **Data Agent 不暴露源库 target，不允许源库 fallback。**
7. **Doris 不可用时 fail-closed：不改查其他 DataSource。**
8. **未同步或未加工完成的对象不得标记为 queryable。**
9. **物化成功只表示 schema ready，不表示 data ready。**
10. **DAG 投递或触发成功不等于业务任务成功；以 Airflow/Flink 最终态为准。**
11. **查询数据源不能按 `updated_at`、创建时间、行顺序或 `catalog_name=NULL` 推断。**
12. **本体版本、对象投影、物理库表和字段映射必须可审计。**
13. **业务源连接和 Doris 连接不得被放进 LLM 上下文或治理 Spec。**
14. **全量同步与全量 ETL 失败时不得破坏当前正式表。**
15. **能力不足显式进入 validation error/warning，不静默降级。**

建议增加架构级常量或策略模块，使以下断言成为运行时硬条件：

```python
WAREHOUSE_ENGINE = "doris"
ALLOWED_EXECUTION_ENGINES = {
    "materialize": {"doris"},
    "sync": {"flink"},
    "transform": {"doris"},
    "metric": {"doris"},
    "query": {"doris"},
}
```

不要仅把 `DEFAULT_ENGINE` 从 `hive` 改成 `doris` 后继续保留静默 fallback。目标是“强制 Doris”，不是“更偏爱 Doris”。

---

## 3. 当前实现与目标架构的差距

### 3.1 当前执行矩阵

当前代码实际形态：

| 制品 | 当前执行方式 | 目标执行方式 |
|---|---|---|
| materialize | Airflow `SQLExecuteQueryOperator` 执行目标引擎 DDL | Airflow 执行 Doris DDL |
| sync | Flink SQL on YARN | 保留，但目标固定为 Doris ODS |
| transform | Flink 同时连接源端和目标端 | 改为 Doris 内部 SQL |
| metric | MetricCompiler + Flink SQL | 改为 MetricCompiler + Doris SQL |
| Data Agent | warehouse-first，全局 DataSource 选择 | 固定当前本体部署对应的默认 Doris |

### 3.2 需移除的现存假设

1. `backend/app/warehouse/registry.py` 中 `DEFAULT_ENGINE = "hive"`；
2. `backend/app/services/materialization_contract.py` 中默认引擎为 Hive；
3. `backend/app/services/warehouse_generator.py` 中“Hive 是权威物理副本、其余引擎从 Hive 派生”；
4. `TransformExecutor` 将 transform 编译为 Flink SQL；
5. `MetricExecutor` 将 metric/tag/rule 编译为 Flink SQL；
6. `flink_job_runner.py` 将通用计算任务的 DAG engine 写死为 Hive；
7. Data Agent `run_sql.target` 支持 `warehouse/erp/crm`；
8. `resolve_domain_data_source()` 按全局 DataSource、`catalog_name` 和更新时间选源；
9. `list_catalogs` 向 Agent 暴露源库查询目录；
10. `DataSource.mapping_json` 同时承载多个本体的映射，缺少本体版本隔离；
11. transform/metric Spec 暴露 Flink parallelism、queue、checkpoint 等不再适用的字段；
12. 前端仍允许选择 Hive/StarRocks/Postgres 等物化引擎。

### 3.3 当前选源风险

当前 `resolve_domain_data_source()`：

- 不按 `domain_id` 或 `ontology_id` 过滤；
- 将 `catalog_name` 为空或 `internal` 的数据源视为 warehouse；
- 多候选按更新时间取最新；
- 无明确 warehouse 时存在兼容性 fallback。

目标架构必须彻底删除这套推断，改为：

```text
当前已发布本体
→ 当前本体版本的 Doris Deployment
→ 默认 Doris DataSource
→ 对象级 Projection
→ Doris 只读执行
```

---

## 4. Doris 数仓分层

### 4.1 分层定义

| 层 | 语义 | 数据产生方式 | 是否可直接供 Agent 查询 |
|---|---|---|---:|
| ODS | 业务源在 Doris 中的标准化原始投影 | Flink | 默认否；仅显式设置 serving 时允许 |
| DIM | 主数据、维度、业务对象当前态 | Doris SQL | 是 |
| DWD | 明细事实、桥表、业务事件 | Doris SQL | 是 |
| DWS | 面向主题的轻度汇总 | Doris SQL | 是 |
| ADS | 指标、标签、规则、数据应用结果 | Doris SQL | 是 |

### 4.2 推荐命名

```text
ods_<domain_code>
dim_<domain_code>
dwd_<domain_code>
dws_<domain_code>
ads_<domain_code>
```

示例：

```text
ods_erp.sales_order
dim_erp.customer
dwd_erp.order_detail
dws_erp.daily_order_summary
ads_erp.gmv
```

### 4.3 Serving 表规则

每个可查询本体对象必须有唯一的 `serving_table`：

| 对象类型 | 默认 Serving 层 |
|---|---|
| 主数据、维度对象 | DIM |
| 业务明细、事实、桥表 | DWD |
| 主题汇总对象 | DWS |
| metric/tag/rule | ADS |
| 尚未加工、只有 ODS 的对象 | 默认不可查询；人工允许后才可作为 serving |

Data Agent 不自行猜 ODS/DWD/ADS，查询映射由部署元数据明确给出。

---

## 5. 四类治理制品的目标执行语义

## 5.1 Materialize：Doris 建结构

```text
本体 + 物化契约
→ LogicalSchema
→ DorisAdapter
→ Doris DDL
→ DorisSqlDagBuilder
→ Airflow SQLExecuteQueryOperator
→ Doris
```

边界：

- 只建表、分区、分桶和必要的表属性；
- 不搬数据；
- 不执行 transform 或 metric；
- 目标必须是默认 Doris；
- 成功后状态为 `schema_ready`，不能直接标记 `queryable=true`；
- ODS 接入表与语义层表均通过确定性 DDL 创建。

## 5.2 Sync：Flink 只同步到 Doris ODS

```text
业务源物理表
→ Flink JDBC/CDC Source
→ 字段重命名、必要类型转换
→ Doris Connector
→ ods_<domain>.<table>
```

禁止：

```text
业务源 → Flink → DIM/DWD/DWS/ADS
```

Flink 同步阶段只做进入 Doris 所需的最小转换：

- 源字段到本体字段的确定性映射；
- 源类型到 ODS 类型的必要 CAST；
- CDC 元数据处理；
- 不承载业务清洗、业务关联、指标聚合。

### 5.2.1 Full

```text
Doris 创建 <ods_table>__stg_<run>
→ Flink 全量写 staging
→ 行数/主键/非空检查
→ Doris REPLACE WITH TABLE
```

要求：失败时正式表不变。

### 5.2.2 Incremental

```text
按增量字段读取源数据
→ Flink
→ Doris Unique Key UPSERT
→ 持久化成功水位
```

必填：

- 主键；
- 增量字段；
- 初始水位；
- 迟到数据策略；
- 重跑幂等策略。

### 5.2.3 CDC

```text
MySQL binlog / PostgreSQL WAL
→ Flink CDC
→ Doris Unique Key Merge-on-Write
```

必须明确：

- 业务主键；
- sequence column；
- UPDATE 顺序；
- DELETE 传播策略；
- checkpoint/savepoint；
- Flink job id；
- 作业升级、停止和恢复流程。

Airflow 对长期 CDC 作业的职责是“部署、更新、巡检”，不应让单个 Airflow task 永久阻塞：

1. 提交 Flink detached job；
2. 保存 `flink_job_id`；
3. 周期健康检查；
4. 异常告警或基于 savepoint 恢复；
5. 防止 cron 重复提交同一 CDC 作业。

## 5.3 Transform：Doris 内部 ETL

```text
Doris ODS / 上游语义表
→ Doris SELECT
→ 清洗、去重、关联、派生
→ Doris DIM / DWD / DWS
```

示例：

```sql
INSERT OVERWRITE TABLE `dim_erp`.`customer`
SELECT
  `customer_id`,
  TRIM(`customer_name`) AS `customer_name`,
  UPPER(`customer_code`) AS `customer_code`,
  `modified_at`
FROM `ods_erp`.`customer`
WHERE `customer_id` IS NOT NULL;
```

全量 transform 推荐：

```text
create staging
→ insert/select into staging
→ quality check
→ atomic replace
```

Transform Spec 移除或废弃：

- `source_ref_alias`；
- `execution_mode=streaming`；
- Flink parallelism；
- Flink yarn queue；
- Flink deploy target；
- Flink checkpoint；
- Flink extra args。

保留：

- ontology/version；
- 上游 Doris 表；
- 目标 Doris 表；
- cleansing rules；
- load mode；
- partition；
- quality rules；
- schedule。

## 5.4 Metric：Doris 原生聚合

```text
BusinessLogic / expression_json
→ MetricCompiler(dialect="doris")
→ Doris SELECT
→ ADS 结果表
→ Airflow 调度执行
```

metric、tag、rule 继续共享同一个 MetricCompiler 和结果形状真源，不另写口径逻辑。

示例：

```sql
INSERT OVERWRITE TABLE `ads_erp`.`gmv`
SELECT
  CURRENT_DATE AS `stat_date`,
  `region`,
  SUM(`amount`) AS `metric_value`
FROM `dwd_erp`.`sales_order`
GROUP BY `region`;
```

禁止 MetricExecutor 再生成 Flink Source/Sink DDL。

---

## 6. 数据模型重构

## 6.1 DataSource 角色显式化

当前 `catalog_name` 不再承担“仓库/源库”判定。建议给 `DataSource` 增加：

```text
purpose: business_source | warehouse
is_default_warehouse: bool
```

约束：

1. 每个运行环境只允许一个启用的默认 warehouse；
2. 默认 warehouse 必须 `kind=doris`；
3. `business_source` 可以是 mysql/postgres 等；
4. Agent 查询不读取 `catalog_name`；
5. `catalog_name` 如需保留，只作外部 catalog 元数据，不作路由依据。

建议数据库约束与服务层校验同时存在，避免出现两个默认 Doris。

### 6.1.1 Doris 连接配置契约

Doris 不能只复用当前 `DataSource.dsn_secret_ref` 的一个 MySQL URL。目标架构至少有三种连接用途：

1. Data Agent/Data App 通过 FE MySQL 协议执行只读查询；
2. Airflow 通过 Doris SQL 连接执行 DDL、transform 和 metric；
3. Flink Doris Connector 通过 FE HTTP 节点写 ODS。

这三类连接的协议、端口和权限不同，必须在 Web 配置中显式增加。**配置入口仍归“数据源/数仓”管理，不在 `DependencyComponent` 中再建第二套 warehouse 连接**：当前统一依赖服务已明确 warehouse 由 `DataSource` 管理，重构应保持一个逻辑事实源。

建议新增单例绑定配置 `DorisWarehouseConfig`，与默认 warehouse DataSource 1:1：

```text
id                              # default
warehouse_datasource_id          # FK DataSource，必须 purpose=warehouse/kind=doris
enabled

# FE MySQL 协议：Data Agent 查询和 SQL Operator
query_host
query_port                       # 默认 9030
default_catalog                  # 默认 internal
default_database                 # 可为空；实际表使用 database.table
connect_timeout_seconds
query_timeout_seconds
ssl_enabled

# FE HTTP：Flink Doris Connector
fenodes_json                     # ["fe-1:8030", "fe-2:8030"]

# 运行身份别名；这里只存 alias/conn_id，不在 Spec 中存凭据
airflow_ddl_conn_id
airflow_etl_conn_id
airflow_flink_conn_id

# Data Agent 只读连接的受管 secret/DSN 引用
reader_dsn_secret_ref

created_at
updated_at
```

推荐稳定的 Airflow Connection ID 使用 DataSource id，而不是名称，避免改名导致已有 DAG 断链：

```text
ontometa_doris_<datasource_short_id>_ddl
ontometa_doris_<datasource_short_id>_etl
ontometa_doris_<datasource_short_id>_flink
```

各连接用途：

| 配置/连接 | 协议 | 端口 | 身份 | 使用方 |
|---|---|---:|---|---|
| reader DSN | MySQL protocol | 9030 | `ontometa_reader` | Data Agent/Data App/画像 |
| `*_ddl` Airflow Connection | MySQL protocol | 9030 | `ontometa_ddl` | materialize |
| `*_etl` Airflow Connection | MySQL protocol | 9030 | `ontometa_etl` | transform/metric |
| `*_flink` Airflow Connection | Doris Connector + FE HTTP | 8030 | `ontometa_flink_sink` | sync |

Flink Connection 的 `extra` 至少需要：

```json
{
  "fenodes": "fe-1:8030,fe-2:8030",
  "jdbc_url": "jdbc:mysql://fe-1:9030",
  "catalog": "internal"
}
```

当前 `flink_sql_generator` 的 Doris sink 会引用 `${ALIAS_FENODES}`，但现有 `endpoint_credential_env()` 尚未从 Airflow Connection extra 注入 `FENODES`。实施 Phase 2 时必须补齐，不能只配置 9030 查询端口。

### 6.1.2 连接配置的唯一事实源与凭据流转

Web 配置的权威关系建议为：

```text
DataSource + DorisWarehouseConfig（逻辑配置真源）
              │
              ├─ reader secret/ref → ontoMeta Doris Query Gateway
              └─ conn_id + 受管凭据 → 幂等同步到 Airflow Connections
                                      ├─ DDL
                                      ├─ ETL
                                      └─ Flink sink
```

要求：

1. 设置页支持保存、测试和启用默认 Doris；
2. 保存后由后端幂等创建/更新三条 Airflow Connection，或明确校验已存在；
3. API 回显只返回 `*_set/*_hint`，不得返回密码明文；
4. 治理 Spec、DAG 边车和 SQL 文件只保存 conn_id/alias；
5. 修改密码时留空表示保持原值；
6. 删除或停用默认 Doris 前，检查是否仍有活跃 Deployment/DAG；
7. Doris 配置更新后要使 Query Gateway 的连接池失效并重建；
8. 连接测试不能把 DSN、用户名或密码写入日志和执行回执。

若首期暂不实现 Airflow Connection 自动写入，也必须在设置页展示三条**确定性的 conn_id**及逐条检测结果，不能等 DAG 运行后才发现 Connection 不存在。

### 6.1.3 Doris DSN 与方言识别

Doris 使用 MySQL 线协议，现有前端会生成：

```text
mysql+pymysql://user:password@fe-host:9030/database
```

该 URL 可被 SQLAlchemy/PyMySQL 执行，但现有 `_backend_of(dsn)` 会把它识别成 `mysql`，不能据此判断数仓引擎。重构后必须把“连接传输协议”和“SQL 方言”分离：

```python
execute_sql(
    dsn=reader_dsn,
    dialect="doris",
    sql=sql,
    ...,
)
```

Query Gateway 以 `DataSource.kind == "doris"` 和 Deployment 判定方言，不从 DSN scheme 猜 Doris；底层仍可用 `mysql+pymysql` 连接。不要未经驱动验证就改成 `doris://`，否则 SQLAlchemy 可能因缺少 Doris dialect plugin 无法创建 Engine。

### 6.1.4 Doris 连接 Preflight

启用默认 Doris 或执行任务前至少检查：

| 检查 | 方式 | 失败处理 |
|---|---|---|
| FE SQL 可达 | reader/DDL/ETL 分别 `SELECT 1` | 阻断对应能力 |
| Doris 身份 | 查询版本/节点信息，确认不是普通 MySQL | 阻断启用 |
| reader 只读 | `SHOW GRANTS` + 写权限检查 | 阻断 Data Agent 上线 |
| DDL 权限 | canary 库表建表/删除表，禁止删除库 | 阻断 materialize |
| ETL 权限 | canary 表 INSERT/SELECT/清理 | 阻断 transform/metric |
| FE HTTP 可达 | 探测所有 `fenodes` | 阻断 Flink sync |
| Airflow Connections | 检查三条 conn_id 存在且字段完整 | 阻断任务提交 |
| Flink Connector | 最小 canary sink 或 connector capability probe | 阻断 sync |
| 默认唯一性 | 只存在一个 enabled default Doris | 阻断启用 |

Canary 对象必须位于专用测试库，不得触碰业务正式表；清理失败需在报告中显式列出。

## 6.2 IngestionContract

新增接入契约，精确描述源表如何进入 Doris ODS：

```text
id
ontology_id
ontology_version
object_type_id

source_datasource_id
source_physical_table
source_mapping_json

doris_datasource_id
target_ods_database
target_ods_table

mode                       # full / incremental / cdc
primary_keys_json
sequence_column
incremental_column
delete_policy
refresh_cron

flink_params_json
status
last_success_at
sync_watermark
flink_job_id
checkpoint_path
savepoint_path
created_at
updated_at
```

职责边界：

> 描述“哪个业务源的哪张物理表，以什么 Flink 模式，进入 Doris 哪张 ODS 表”。

## 6.3 MaterializationContract 调整

保留：

- `target_layer`；
- `load_strategy`；
- `partition_key`；
- `scd_type`；
- `refresh_cron`；
- `materialized`；
- 三方合并和人工钉住字段。

调整：

1. `target_engines` 历史字段可保留，但新契约固定 `['doris']`；
2. 不再表达 Hive 权威与多引擎派生；
3. 增加或形成独立 Doris Storage Contract：

```text
doris_key_model             # unique / duplicate / aggregate（首期前两种）
distribution_keys_json
bucket_count
replication_num
sequence_column
```

推荐默认：

| 场景 | Doris 模型 |
|---|---|
| 有稳定业务主键的当前态表 | Unique Key |
| CDC 当前态表 | Unique Key + sequence |
| 无主键的追加明细 | Duplicate Key |
| ADS 指标结果 | 按结果粒度选择 Unique/Duplicate |
| Aggregate Key | 首期不自动推导，必须显式配置 |

## 6.4 OntologyWarehouseDeployment

新增本体版本到 Doris 的部署记录：

```text
id
ontology_id
ontology_version
doris_datasource_id
status                      # pending/schema_ready/ready/stale/failed/disabled
materialization_artifact_id
created_at
updated_at
```

## 6.5 WarehouseObjectProjection

新增对象级投影：

```text
id
deployment_id
object_type_id

ods_database
ods_table
serving_layer
serving_database
serving_table

column_mapping_json
schema_status               # pending/ready/failed
sync_status                 # empty/syncing/ready/stale/failed
transform_status            # not_required/pending/running/ready/failed
last_sync_at
sync_watermark
queryable
created_at
updated_at
```

Data Agent 使用该表解析本体逻辑标识符到 Doris 物理标识符，不再使用全局 `DataSource.mapping_json` 作为本体映射权威。

## 6.6 状态推进规则

### Materialize 最终成功

```text
schema_status = ready
sync_status = empty
queryable = false
```

### Sync 最终成功

若 serving table 是 ODS：

```text
sync_status = ready
queryable = true
```

若还需要 transform：

```text
sync_status = ready
transform_status = pending
queryable = false
```

### Transform 最终成功

```text
transform_status = ready
queryable = true
```

### 任务失败

- 不删除上一份成功数据；
- 记录失败制品；
- 根据 SLA 将状态保持 ready 或转 stale；
- strict freshness 下禁止查询；
- warn freshness 下允许查询但必须在答案中标注水位。

状态只能由 Airflow/Flink 最终态对账推进，不能在“DAG 已提交”时推进。

---

## 7. Doris Adapter 与生成器

## 7.1 默认引擎

需要修改：

```text
backend/app/warehouse/registry.py
backend/app/services/materialization_contract.py
backend/app/schemas/warehouse.py
backend/app/agents/common.py
backend/app/api/warehouse.py
frontend/src/components/MaterializeModal.tsx
frontend/src/components/MaterializationContractPanel.tsx
frontend/src/components/artifact-spec/specFields.ts
```

目标：

```python
DEFAULT_ENGINE = "doris"
```

同时 Validation Gate 必须拒绝新建非 Doris 数仓制品，不能继续 fallback。

## 7.2 删除 Hive 权威派生

需处理：

```text
backend/app/services/warehouse_generator.py
```

废弃或重定义：

- `_hive_source()`；
- `generate_derivation()`；
- bundle 中 `authoritative: hive`；
- “Hive 权威写入，其余引擎从 Hive 派生”的 note；
- 对应测试和文档。

新规则：

```text
本体 = 语义权威
Doris = 唯一物理数仓权威
不再生成跨引擎派生作业
```

## 7.3 Doris Adapter 补齐项

现有 `DorisAdapter` 已包含类型、DDL、Unique/Duplicate Key、分区、分桶和 replace swap 基础。实施前需补齐并在真实 Doris 版本验证：

1. `render_load()` 的 Doris full/append 语义；
2. `INSERT OVERWRITE` 对目标 Doris 版本和表模型的支持；
3. staging `CREATE TABLE LIKE`；
4. `ALTER TABLE ... REPLACE WITH TABLE` 的原子性和限制；
5. Unique Key Merge-on-Write；
6. sequence column 配置；
7. CDC DELETE 语义；
8. AUTO PARTITION 版本兼容；
9. bucket 数与 BE 数量的生产默认值；
10. schema change 对 Key 列和值列的差异；
11. SQLAlchemy/PyMySQL 执行多语句和超时行为。

未完成真实实例验证的能力不能从 warning 提升为 guaranteed。

---

## 8. Airflow 执行架构

## 8.1 拆分 DAG Builder

当前 `airflow_dag_builder.py` 同时承载 Flink、DDL、swap。目标拆为：

### `IngestionDagBuilder`

只服务 sync：

```text
read_spec
→ create_ods_or_staging
→ submit_flink
→ wait_batch_completion / record_stream_job
→ quality_check
→ swap_full_table
→ update_lineage
```

Operator：

- Doris DDL/检查/swap：`SQLExecuteQueryOperator`；
- Flink 提交：`BashOperator` 或后续专用 Operator；
- batch 最终态：Flink REST 检查；
- CDC：detached 提交 + job id 持久化 + 独立健康检查 DAG。

### `DorisSqlDagBuilder`

服务 materialize/transform/metric：

```text
read_spec
→ precheck
→ create_target_or_staging
→ execute_doris_sql
→ quality_check
→ publish_or_swap
→ lineage_writeback
```

SQL 放边车 `.sql` / JSON，不内联巨型 SQL 到 DAG Python 文件。

### `PipelineCompiler`

现有 `TriggerDagRunOperator(wait_for_completion=True)` 编排方式可保留，子 DAG 改为：

| kind | 子 DAG 类型 |
|---|---|
| materialize | Doris SQL DAG |
| sync | Flink ingestion DAG |
| transform | Doris SQL DAG |
| metric | Doris SQL DAG |

## 8.2 Runner 边界

目标模块关系：

```text
materialize → DorisJobRunner
sync        → FlinkIngestionRunner
transform   → DorisJobRunner
metric      → DorisJobRunner
```

`flink_job_runner.py` 最终只能由 sync 调用。可以通过测试和 import 约束保证 transform/metric 不再依赖：

- `flink_sql_generator`；
- `FlinkEndpoint`；
- `FlinkSqlTask`；
- `flink_params`。

## 8.3 回执统一

建议所有写侧回执至少包含：

```json
{
  "execute_mode": "orchestrated",
  "compute_engine": "doris|flink",
  "target_engine": "doris",
  "artifact_id": "...",
  "dag_id": "...",
  "dag_run_id": "...",
  "state": "queued|running|success|failed",
  "run_url": "...",
  "source_tables": [],
  "target_tables": [],
  "lineage": [],
  "error": null
}
```

Sync 额外包含：

```text
mode
rows_read
rows_written
watermark_before
watermark_after
flink_job_id
checkpoint/savepoint
```

---

## 9. Data Agent Doris-only 查询架构

## 9.1 工具契约

删除或下线：

- `list_catalogs`；
- `run_sql.target`；
- `target="erp"/"crm"`；
- 源库实时查询 Prompt；
- 找不到 Doris 时改查其他 DataSource 的逻辑。

目标 `run_sql`：

```json
{
  "sql": "SELECT ...",
  "limit": 100
}
```

## 9.2 查询解析

```text
run_sql
→ SQL 逻辑语义证明
→ 提取 referenced ontology objects/properties
→ 解析当前本体版本的 Doris Deployment
→ 校验 Projection 覆盖和 queryable
→ 合并对象级表/字段映射
→ 生成 Doris 物理 SQL
→ 只读/超时/LIMIT 校验
→ 默认 Doris reader 连接执行
```

硬校验：

1. DataSource `purpose=warehouse`；
2. DataSource `kind=doris`；
3. DataSource 是唯一默认 warehouse；
4. 本体已发布；
5. Deployment 版本等于当前发布版本；
6. SQL 涉及对象都有 Projection；
7. 所有 Projection `queryable=true`；
8. freshness 满足策略；
9. 角色达到 `agent_run_sql_min_role`；
10. SQL 为单条只读 SELECT。

任何一项失败均不得查询业务源。

## 9.3 查询回执

建议返回：

```json
{
  "executed": true,
  "query_target": {
    "engine": "doris",
    "datasource_id": "doris-prod",
    "datasource_name": "生产 Doris",
    "ontology_version": 7,
    "physical_tables": ["dwd_erp.sales_order"],
    "last_sync_at": "2026-08-23T02:00:00Z",
    "sync_watermark": "2026-08-23T01:58:00Z",
    "stale": false
  }
}
```

前端结果块展示：

```text
数据来源：生产 Doris
本体版本：v7
物理表：dwd_erp.sales_order
最近同步：2026-08-23 10:00
数据水位：2026-08-23 09:58
```

## 9.4 查询相关服务统一

以下路径最终都应走同一个 Doris Query Gateway：

```text
backend/app/services/chat_bi.py
backend/app/services/data_app.py
backend/app/services/data_app_executor.py
backend/app/services/ontology_ladder.py
backend/app/services/column_profiler.py
```

禁止每个服务各自选 DataSource。

---

## 10. 权限和连接

建议 Doris 使用最小权限账号：

| 账号 | 权限 | 使用方 |
|---|---|---|
| `ontometa_ddl` | 建库、建表、ALTER、REPLACE | Airflow materialize |
| `ontometa_etl` | 读取上游层、写目标层、操作 staging | Airflow transform/metric |
| `ontometa_flink_sink` | 写 ODS 与 ODS staging | Flink |
| `ontometa_reader` | 只读已发布 serving 表 | Data Agent/Data App |

上述身份分别绑定第 6.1.1 节的 reader DSN、DDL/ETL/Flink Airflow Connection；不能用一个 Doris 管理员账号通吃全部链路。

业务源使用独立只读账号，例如：

```text
erp_readonly
crm_readonly
```

配置遵守：

1. Web 设置页配置，落数据库；
2. Airflow Connection/受管 Secret 保存实际凭据；
3. Spec 中只保存 datasource id 或 connection alias；
4. 工具结果和执行回执不返回 DSN；
5. Data Agent 只使用 `ontometa_reader`。

---

## 11. API 与前端调整

## 11.1 数据源管理

数据源界面显式区分：

- 业务数据源；
- Doris 数仓。

新增默认 Doris 配置与唯一性校验。

## 11.2 Materialize

- 移除引擎选择；
- 目标固定显示默认 Doris；
- 保留目标库、目标表、层、分区、分桶等 Doris 参数；
- 未配置 Doris 时阻止提交并给出设置入口。

## 11.3 Sync

保留 Flink 作业参数：

- parallelism；
- queue；
- deploy target；
- checkpoint；
- extra `-D`；
- full/incremental/cdc。

新增：

- 源 DataSource；
- ODS 目标；
- 主键；
- sequence column；
- incremental column；
- delete policy；
- 水位和 Flink job 状态。

## 11.4 Transform/Metric

移除所有 Flink 字段，显示：

- 执行引擎：Doris；
- 上游 Doris 表；
- 目标 Doris 表；
- Doris SQL dry-run；
- 质量规则；
- staging/swap；
- Airflow schedule。

## 11.5 Data Agent

- 不再展示 catalog 选择；
- 不再展示源库实时查询能力；
- 查询结果显示 Doris、物理表、同步时间和水位；
- 无 Doris 数据时提示“尚未同步/加工完成”，不提示切换源库。

---

## 12. 文件级影响面

### 12.1 模型与迁移

```text
backend/app/models/data_app.py
backend/app/models/warehouse.py
backend/app/models/agent.py
backend/app/models/query_binding.py                 # 建议新增
backend/alembic/versions/*_add_doris_warehouse*.py
```

### 12.2 Warehouse/Compiler

```text
backend/app/warehouse/registry.py
backend/app/warehouse/adapters/doris.py
backend/app/warehouse/logical_schema.py
backend/app/services/warehouse_generator.py
backend/app/services/materialization_contract.py
backend/app/services/metric_compiler.py
backend/app/services/job_planner.py
backend/app/services/move_job_compiler.py
```

### 12.3 Agent

```text
backend/app/agents/common.py
backend/app/agents/drafters/materialize.py
backend/app/agents/drafters/sync.py
backend/app/agents/drafters/transform.py
backend/app/agents/drafters/metric.py
backend/app/agents/executors/materialize.py
backend/app/agents/executors/sync.py
backend/app/agents/executors/transform.py
backend/app/agents/executors/metric.py
backend/app/agents/validation.py
```

### 12.4 Airflow/执行

```text
backend/app/services/materialization_runner.py
backend/app/services/flink_job_runner.py
backend/app/services/airflow_dag_builder.py
backend/app/services/ingestion_dag_builder.py        # 建议新增
backend/app/services/doris_sql_dag_builder.py        # 建议新增
backend/app/services/doris_job_runner.py             # 建议新增
backend/app/services/pipeline_compiler.py
backend/app/services/agent_pipeline.py
backend/app/services/materialize_preflight.py
```

### 12.5 查询

```text
backend/app/services/data_app.py
backend/app/services/data_app_executor.py
backend/app/services/chat_bi.py
backend/app/services/chat_bi_tool_schemas.py
backend/app/services/ontology_ladder.py
backend/app/services/column_profiler.py
backend/app/services/query_routing.py                 # 建议新增
```

### 12.6 API/Schema

```text
backend/app/api/data_app.py
backend/app/api/warehouse.py
backend/app/api/chat_bi.py
backend/app/schemas/data_app.py
backend/app/schemas/warehouse.py
backend/app/schemas/chat_bi.py
backend/app/schemas/query_binding.py                 # 建议新增
```

### 12.7 前端

```text
frontend/src/components/DataSourcesModal.tsx
frontend/src/components/MaterializeModal.tsx
frontend/src/components/MaterializationContractPanel.tsx
frontend/src/components/artifact-spec/specFields.ts
frontend/src/components/DependencyPanel.tsx
frontend/src/pages/chat-bi/ChatBiReferences.tsx
frontend/src/api.ts
frontend/src/types.ts
```

---

## 13. 分阶段实施计划

## Phase 0：决策固化与安全开关

任务：

- [x] 固化 Doris-only 架构策略模块与默认引擎常量（历史引擎仍可读，生产切流前不删除兼容路径）；
- [x] 以“是否存在启用的默认 Doris”作为迁移期间 Doris-only Gate 开关；生产切流前不删除历史只读兼容路径。
- [ ] 固化 ODS/DIM/DWD/DWS/ADS 命名；
- [ ] 固化 CDC delete、sequence 和水位策略；
- [ ] 盘点现有 DataSource、Airflow Connection、Flink Connector 和 Doris 版本；
- [ ] 建立旧架构基线测试报告。

验收：

- 尚未切换生产路径；
- 所有后续实现决策有唯一文档依据；
- 回滚开关可用。

## Phase 1：Doris 数仓基础

任务：

- [x] 显式 DataSource purpose；
- [x] 新增 `DorisWarehouseConfig` 模型、迁移与 API 契约（Web 表单接线仍待补齐）；
- [ ] 配置 9030 SQL 端点、8030 `fenodes`、默认 catalog/database；
- [ ] 配置 reader/DDL/ETL/Flink 四种最小权限身份；
- [ ] 幂等同步或逐条验证 Doris Airflow Connections；
- [ ] Doris 连接 preflight 与 Query Gateway 连接池刷新；
- [x] 唯一默认 Doris（服务层校验 + SQLite/PostgreSQL 唯一索引）；
- [x] `DEFAULT_ENGINE=doris`；
- [x] Validation Gate 在显式默认 Doris 配置后拒绝新建非 Doris 数仓任务；
- [x] MaterializationContract 默认 Doris；
- [ ] Doris Adapter 真实实例验证（待部署环境提供精确 Doris 版本）；
- [ ] Doris DDL 物化；
- [x] OntologyWarehouseDeployment / WarehouseObjectProjection 模型与迁移；
- [x] Doris 配置 API、连接别名字段与现有 preflight 基础接线；Airflow Connection 自动同步和真实实例探针仍待部署环境验证。

验收：

- 从本体确定性生成 Doris DDL；
- Airflow 可创建 ODS/DIM/DWD/DWS/ADS 表；
- materialize 不依赖 Flink；
- 成功后只有 schema ready，不误标 data ready。

## Phase 2：Flink 接入 Doris ODS

任务：

- [x] IngestionContract；
- [x] source_ref 绑定业务 DataSource；
- [x] Flink sink 固定 Doris ODS；
- [x] full/incremental/cdc（incremental 为有界 JDBC 水位 batch，CDC 为 detached 流作业）；
- [x] full staging + swap；
- [x] CDC job id/checkpoint/savepoint 字段、校验与最终态推进；
- [x] CDC detached Job ID 结构化回执 + Web/DB 配置的 Flink REST 健康检查 API；独立周期健康检查 DAG 待真实集群联调后启用；
- [x] 同步水位持久化（仅 task success 推进）；
- [x] 对象级 sync status。

验收：

- [x] 业务数据只经 Flink 进入 Doris ODS；
- [x] Flink 不写 DIM/DWD/DWS/ADS；
- [x] full 使用 staging + atomic replace，失败不破坏正式表；
- [x] CDC sequence/delete/checkpoint/savepoint/真实 Job ID 契约与生成测试通过；真实集群重启联调待部署环境；
- [x] 回执定义行数、水位和 Job ID；只在最终态/XCom 有真实值时推进。

## Phase 3：Doris 原生 Transform

任务：

- [x] TransformExecutor 移除 Flink；
- [x] 生成 ODS→DIM/DWD Doris SQL；DWD→DWS 复用同一 Doris SQL DAG/上游 Projection 机制；
- [x] 清洗规则使用 Doris 方言；
- [x] staging + SQLCheckOperator quality gate + Doris atomic replace；
- [x] 回执记录 Doris source_tables/target_tables，供表间血缘回写；
- [x] 前端移除 transform Flink/streaming 参数。

验收：

- [x] transform DAG 不包含 `flink run`/BashOperator；
- [x] dry-run 与执行共享同一份 Doris SELECT/清洗规则编译结果；
- [x] ODS Projection 非 ready 时 fail-closed；质量失败阻断 publish；
- [x] 只有 Airflow 最终 success 才推进 transform ready/queryable。

## Phase 4：Doris 原生 Metric

任务：

- [x] MetricCompiler 使用 Doris 方言；
- [x] MetricExecutor 移除 Flink；
- [x] metric/tag/rule 全部 Doris SQL；
- [x] ADS DDL 与 SQL 结果形状共用 `result_column_specs()`；
- [x] ADS staging + SQLCheckOperator + Doris atomic replace；
- [x] 前端移除 metric Flink/streaming 参数。

验收：

- [x] metric DAG 不包含 Flink/BashOperator；
- [x] 指标结果经 staging 发布到 ADS；
- [x] Data Agent 查询编译与离线指标共用 MetricCompiler；
- [x] metric/tag/rule 结果列与 DDL 一致，独立 Logic Projection 只在最终成功后 queryable。

## Phase 5：Data Agent Doris-only

任务：

- [x] 删除 `list_catalogs`；
- [x] 删除 `run_sql.target`；
- [x] 查询路由固定 Deployment 的 Doris；
- [x] 对象 Projection 覆盖校验；
- [x] 本体版本校验；
- [x] 未同步/未加工对象 fail-closed；
- [x] 查询回执增加物理表、Projection、水位与 stale 标识；
- [x] Data App/画像/预览统一 Doris Query Gateway；保存的业务源/Cube 绑定不参与执行。

验收：

```python
assert query_target.datasource.kind == "doris"
assert query_target.datasource.id == default_doris_id
```

同时断言：

- MySQL/Postgres 更新时间更新不影响选源；
- 用户不能通过 target 查询源库；
- Doris 不可用时不 fallback；
- 未 queryable 的对象不执行 SQL。

## Phase 6：生产迁移与旧路径清理

任务：

- [x] 建立持久化迁移批次、1–15 严格顺序证据、失败阻断、审批、观察窗口和回滚控制面；
- [x] shadow 差异报告只保留哈希/计数，不返回或持久化业务结果；
- [x] 运行时新任务删除 Hive/StarRocks/Postgres target fallback，历史 Artifact/receipt 只读保留；
- [ ] 建立生产默认 Doris 与四种最小权限身份（**当前阻断项**）；
- [ ] Doris 全量建表；
- [ ] 全量同步 ODS；
- [ ] 执行 transform/metric；
- [ ] 旧仓与 Doris 对账；
- [ ] Data Agent shadow query；
- [ ] 审批切流；
- [ ] 停止旧周期 DAG；
- [ ] 完成生产 as-built 最终态文档。

当前生产执行状态：**NO-GO**。应用数据库中默认 Doris/Doris 配置/Deployment/Projection/IngestionContract 均为 0，严格停在步骤 1；详见 `DORIS_PHASE6_PRODUCTION_MIGRATION_REPORT.md`。

验收：

- 生产 Data Agent 100% 查询 Doris；
- 所有活跃 sync 由 Flink 执行；
- 所有活跃 transform/metric 由 Doris 执行；
- Airflow 状态和制品状态一致；
- 旧制品只读可审计。

---

## 14. 数据迁移、切流与回滚

## 14.1 迁移步骤

1. 冻结旧架构的结构性变更；
2. 部署默认 Doris 和最小权限账号；
3. 建立新 Deployment/Projection；
4. 物化 Doris ODS 与语义层；
5. 从源库全量同步 ODS；
6. 执行 Doris transform；
7. 执行 Doris metric；
8. 对账；
9. Data Agent shadow query；
10. 审核后切换 Doris-only；
11. 观察稳定窗口；
12. 停止旧 DAG；
13. 清理兼容代码。

## 14.2 对账指标

至少包括：

- 表行数；
- 主键覆盖率；
- 主键重复数；
- 必填字段空值率；
- 最大/最小业务时间；
- 金额 SUM；
- 数量 COUNT；
- 维度分布；
- metric/tag/rule 结果；
- CDC 水位与源库时间差；
- Data Agent 典型问题结果。

## 14.3 双跑约束

双跑只用于验证：

- 不允许两个链路同时写同一张正式 Doris 表；
- 使用独立 staging 或验证库；
- Data Agent shadow query 不返回给最终用户；
- 所有差异有可追溯报告。

## 14.4 回滚

切流前保留旧 DAG 和旧查询开关。若 Doris 路径失败：

1. 停止新 Doris 周期 DAG；
2. 恢复旧只读查询开关；
3. 不删除 Doris 数据和新制品；
4. 保留失败回执和对账结果；
5. 修复后从最近成功水位继续。

进入 Phase 6 清理并删除旧运行时路径后，回滚改为版本回滚，不再承诺在线动态切回源库。

历史成功 `GovernanceArtifact` 和 `execution_receipt_json` 不允许原地重写。

---

## 15. 测试计划

## 15.1 Doris Adapter

- [ ] 类型映射；
- [ ] Unique/Duplicate Key；
- [ ] Key 列前置；
- [ ] 分区与分桶；
- [ ] sequence column；
- [ ] ODS/语义表 DDL；
- [ ] staging；
- [ ] replace swap；
- [ ] full/append/upsert；
- [ ] 不支持能力显式报错。

## 15.2 Flink Sync

- [ ] MySQL full → Doris ODS；
- [ ] PostgreSQL full → Doris ODS；
- [ ] MySQL incremental；
- [ ] MySQL CDC update/delete；
- [ ] PostgreSQL CDC；
- [ ] checkpoint 恢复；
- [ ] savepoint 升级；
- [ ] 重复提交幂等；
- [ ] full 中途失败不影响正式表；
- [ ] 字段映射与 CAST；
- [ ] 行数和水位回执。

## 15.3 Doris Transform

- [ ] ODS→DIM；
- [ ] ODS→DWD；
- [ ] DWD→DWS；
- [ ] drop_null；
- [ ] deduplicate；
- [ ] trim；
- [ ] upper/lower；
- [ ] 质量失败不 swap；
- [ ] 重跑幂等；
- [ ] dry-run 与执行 SQL 一致。

## 15.4 Doris Metric

- [ ] metric；
- [ ] tag；
- [ ] rule；
- [ ] group by；
- [ ] filter；
- [ ] 多对象 join；
- [ ] ADS DDL 与结果列一致；
- [ ] 语义证明；
- [ ] staging 和发布。

## 15.5 Airflow

- [ ] DAG SSH 投递；
- [ ] DAG parse；
- [ ] Doris SQL 最终态；
- [ ] Flink batch 最终态；
- [ ] CDC 部署与健康态；
- [ ] Pipeline 上游失败阻断下游；
- [ ] retry；
- [ ] 回执与 DagRun 对账；
- [ ] 状态最终推进 Projection。

## 15.6 Data Agent

- [ ] 永远只查默认 Doris；
- [ ] 不允许业务源查询；
- [ ] 不按更新时间选源；
- [ ] 本体版本匹配；
- [ ] Projection 覆盖；
- [ ] queryable 校验；
- [ ] stale strict/warn；
- [ ] SQL 只读；
- [ ] timeout/LIMIT；
- [ ] RBAC；
- [ ] 回执包含 Doris、物理表和水位。

## 15.7 架构防回归

增加静态或单测断言：

- `TransformExecutor` 不 import Flink runner/generator；
- `MetricExecutor` 不 import Flink runner/generator；
- `run_sql` schema 不存在 `target`；
- Query Gateway 只接受 `kind=doris`；
- 新建 MaterializationContract 只有 Doris；
- 新建 transform/metric Spec 不包含 Flink 参数；
- 非 Doris 物化/加工任务被 Validation Gate 阻断。

---

## 16. 可观测性与治理

建议新增指标：

```text
sync_rows_read_total
sync_rows_written_total
sync_lag_seconds
sync_watermark
flink_job_health
flink_checkpoint_age_seconds
doris_etl_duration_seconds
doris_etl_failed_total
doris_query_duration_seconds
doris_query_failed_total
query_projection_not_ready_total
query_stale_warning_total
```

每次查询和任务需可追溯到：

- ontology id/version；
- artifact id；
- Airflow DAG/DagRun；
- Flink job id（sync）；
- source/target dataset URN；
- Doris physical tables；
- sync watermark；
- SQL hash；
- operator/principal。

DataHub 血缘目标：

```text
业务源表
→ Flink sync job
→ Doris ODS
→ Doris transform job
→ Doris DIM/DWD/DWS
→ Doris metric job
→ Doris ADS
```

---

## 17. 风险与前置验证

| 风险 | 处理 |
|---|---|
| Doris 版本不支持计划 SQL | Phase 1 在真实实例执行 adapter contract tests |
| CDC delete/sequence 语义不一致 | Phase 0 明确策略，Phase 2 做端到端测试 |
| 长期 Flink job 被 Airflow 重复提交 | job id + 幂等键 + 独立健康检查 |
| Full swap 对 Doris 表属性有限制 | 真实实例验证 `REPLACE WITH TABLE` |
| 734+ 表 DAG 规模过大 | 按 cron、域和 max tasks 分批 |
| 本体更新后旧投影仍被查询 | Deployment 绑定 ontology_version，版本不符 fail-closed |
| 只有 ODS、无 serving 表 | 默认不可查询，明确配置 serving 后放行 |
| 查询全部集中 Doris 后容量不足 | 压测 QPS、并发、资源组和超时 |
| transform/metric 从 Flink 改 Doris 后 SQL 方言差异 | golden SQL + 真实 Doris integration tests |
| 旧测试大量依赖 Hive/Flink | 分 Phase 更新，禁止一次性删除全部旧保护网 |

实施前必须记录：

- Doris 精确版本；
- FE/BE 数量；
- Flink 精确版本；
- Doris Flink Connector 版本；
- Airflow 精确版本；
- 是否启用 Merge-on-Write；
- 支持的 CDC 源；
- checkpoint/savepoint 存储；
- Doris 资源组与账号权限。

---

## 18. 待审核关键决策

后续执行前，请逐项确认。默认推荐值如下：

| 决策 | 推荐值 | 审核结果 |
|---|---|---|
| 数仓实例 | 每个运行环境一个默认 Doris | 待确认 |
| Doris 配置入口 | DataSource + DorisWarehouseConfig，依赖组件不重复配置 | 待确认 |
| Doris SQL/HTTP 端点 | 9030 SQL + 可配置多 FE 8030 fenodes | 待确认 |
| Doris 运行身份 | reader/DDL/ETL/Flink 四种最小权限身份 | 待确认 |
| Airflow Connection | Web 保存后自动同步；做不到时至少逐条 preflight | 待确认 |
| 数仓分层 | ODS/DIM/DWD/DWS/ADS 分库 | 待确认 |
| Flink 职责 | 仅 source→ODS | 待确认 |
| Transform | Doris SQL | 待确认 |
| Metric | Doris SQL | 待确认 |
| Data Agent | Doris-only，不提供源库 target | 待确认 |
| Full | staging + atomic replace | 待确认 |
| CDC 模型 | Unique Key + sequence | 待确认 |
| CDC 调度 | Airflow 部署/巡检，Flink 长期运行 | 待确认 |
| 物理映射 | ontology version 级 Projection | 待确认 |
| Queryable | sync/transform 最终成功后开放 | 待确认 |
| 旧引擎 | 历史只读，不允许新任务 | 待确认 |
| DataHub external catalog | 可保留元数据用途，不参与查询路由 | 待确认 |
| ODS 查询 | 默认关闭，仅显式 serving 配置放行 | 待确认 |
| Freshness | 支持 strict/warn，默认 strict | 待确认 |

---

## 19. 后续新会话执行入口

新会话开始实施时，必须先：

1. 完整阅读本文；
2. 阅读 `docs/DEVELOPMENT_PRINCIPLES.md`；
3. 阅读当前 as-built：`docs/DW_IMPLEMENTATION.md`；
4. 阅读 `docs/TASK_PIPELINE_PLAN.md`；
5. 检查 `git status`，不得覆盖未提交改动；
6. 运行当前测试基线；
7. 确认第 18 节审核结果；
8. 只实施一个 Phase，不跨 Phase 大爆炸改造；
9. 每个 Phase 完成后更新本文 checklist 和 as-built 文档；
10. 每个 Phase 都必须提供迁移、测试和回滚证据。

推荐新会话提示：

```text
请按 docs/DORIS_WAREHOUSE_REFACTOR_PLAN.md 实施 Phase N。
开始前先完整阅读该文档、DEVELOPMENT_PRINCIPLES.md 和当前相关代码，
确认 git 状态与测试基线。严格限制在 Phase N 范围内，完成模型/迁移、
后端、前端、测试和文档；不要提前删除后续 Phase 仍需的兼容路径。
```

---

## 20. 最终执行矩阵

| 制品/能力 | 传输或计算引擎 | 编排/执行 |
|---|---|---|
| materialize | Doris DDL | Airflow |
| sync | Flink → Doris ODS | Airflow |
| transform | Doris SQL | Airflow |
| metric | Doris SQL | Airflow |
| pipeline | 触发上述子 DAG | Airflow |
| Data Agent query | Doris SELECT | ontoMeta 只读 Query Gateway |
| Data App/query preview | Doris SELECT | ontoMeta 只读 Query Gateway |
| profiling | Doris SELECT | ontoMeta 只读 Query Gateway |

最终架构原则：

> **Flink 只解决数据如何进入 Doris；Doris 解决数据进入后如何加工、汇总和查询；Airflow 解决何时执行、如何依赖、如何重试与对账；Data Agent 永远只面对 Doris 这一份物理数据事实。**
