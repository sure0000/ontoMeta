# 物化执行改造方案：同步工具 + Airflow 编排 + DataHub 血缘自动注册

> **执行通道部分已被修订**：M10 落地后在真实实例上暴露的一类失败（「每表一个一次性
> DockerOperator 容器」要求九件部署事实同时成立，而提交时只能验证其中一件），
> 见 [MATERIALIZE_SYNC_STABILITY.md](./MATERIALIZE_SYNC_STABILITY.md)。
> 本文第 3 节的分工与第 6 节的血缘设计不变，第 7 节里 DockerOperator 那条通道已被取代。

> **本文是提案（proposal），不是 as-built。** 与 `DW_IMPLEMENTATION.md`（已建成执行规格）区分开：
> 文中「现状」部分对照仓库实际代码核实并给出行号；「目标」部分是尚未实现的设计。
> 涉及外部系统版本与能力的断言一律标注「⚠ 需实施前验证」，不臆造。

---

## 1. 现状与它撑不住的地方

当前物化的执行路径（`app/services/materialization_runner.py:190`）：

```
弹窗「执行物化」→ HTTP 请求内同步执行
  ├─ generate_ddl  → execute_write(目标库 DSN, [CREATE TABLE ...])   # 单事务
  └─ generate_etl_sql → execute_write(目标库 DSN, [INSERT OVERWRITE ...])
```

即 **ontoMeta 自己就是执行器**：把生成的 SQL 通过 SQLAlchemy 直接打到目标数仓的 JDBC 连接上
（`app/services/data_app_executor.py::execute_write`）。四个问题，按严重性排序：

**① 跨源搬运根本不成立（阻断级）。**
生成的装载语句形如 `INSERT OVERWRITE TABLE dim.customer SELECT ... FROM erp_ods.tab_customer`
（`app/services/warehouse_generator.py:568`），其中源表名来自 `ObjectType.source_ref` 的 DataHub URN。
这条 SQL 是在**目标数仓**上执行的，因此隐含假设「源表在目标数仓里可见」。而真实拓扑是
ERP 的 MySQL/MariaDB 在一侧、Hive/Doris 在另一侧——除非事先配好外部 Catalog / 联邦查询，
这条 SQL 必然报表不存在。**跨库搬运本来就不是一条 INSERT…SELECT 能干的事**，这正是
DataX / SeaTunnel / Flink 这类工具存在的理由。

**② 契约里的调度与增量语义没有消费者。**
`MaterializationContract.refresh_cron`（定时策略）当前全仓无人读取；增量装载生成的
`WHERE dt >= :watermark` 里的水位占位符，注释写明「由调度器注入」，但这个调度器不存在。
换言之：存储策略能配、能写回契约，却没有任何东西按它跑。

**③ 血缘断链。**
DataHub 里看不到「`dim.customer` 由 `erp_ods.tab_customer` 经哪个作业产出」。ontoMeta 侧其实
**已经有全部信息**——`generate_dag()` 有表间依赖图、`Property.source_field_ref` 有列级映射——
只是没有任何一端把它注册出去。M7（`services/datahub_writeback.py`）回写的是业务命名/描述/术语/域，
不含血缘。

**④ 同步 HTTP 执行不可扩展。**
734 个对象的本体一次物化会在一个请求里跑成百上千条语句；无重试、无断点、无进度，网关超时即失败，
且失败后无法只重跑失败的表。

> 结论：**「生成」这一半（M3）是对的且可复用，「执行」这一半需要交给专业工具。**
> 本方案不动生成器的任何产出语义，只替换执行与调度通道。

---

## 2. 不变量（沿用 + 新增）

沿用 `DW_IMPLEMENTATION.md` 第 1 节的既有不变量（本体是一级源数据、LLM 只产声明式 Spec、
执行器确定性、幂等、不可生成项显式 `unsupported`、**凭据不进产物**）。本方案新增一条：

> **ontoMeta 不做运行时执行器。**
> 它产出「作业定义 + 依赖图 + 调度定义」，由 SeaTunnel/Flink 执行、由 Airflow 编排；
> 运行状态**回读**呈现，不代管、不重实现。

推论：物化弹窗的「执行物化」语义从「此刻建表落数」变为「提交作业并触发一次运行」，
回执从「DDL/ETL 两阶段成败」变为「DagRun 状态 + 各表任务状态 + 日志/血缘链接」。**这是一次
用户可见的行为变更**，UI 文案与交互必须同步改（见 §7）。

---

## 3. 目标架构

```
本体 + 物化契约
   │
   ├─(已有 M3)→ generate_ddl        → 建表语句（含本体反补的 COMMENT/分区/主键声明）
   ├─(已有 M3)→ generate_dag        → 表间依赖序（已处理 ERP 血缘大环，Tarjan）
   └─(新)     → JobPlanner          → JobSpec[]（与工具无关的搬运声明：源/目标/列映射/模式/水位）
                     │
                     ├─(新) SyncToolAdapter ──→ SeaTunnel 作业配置（默认）
                     │        ├ DataX job.json（小表/无 SeaTunnel 环境回退）
                     │        └ Flink SQL（CDC/流式）
                     │
                     └─(新) AirflowDagBuilder → DAG：create_tables → sync_<表> ×N（按依赖序）→ verify
                                  │
                                  ├─ Airflow 执行（重试/补数/水位/并发由 Airflow 管）
                                  └─ DataHub Airflow Plugin 自动发 DataFlow/DataJob/Lineage
                                                │
                                        DataHub 血缘闭环 ← ontoMeta 兜底 emitter（插件缺位时）
```

**关键分工（必须写死，否则本体的价值会被绕过）**：

| 职责 | 归谁 | 理由 |
|---|---|---|
| 建表 DDL | **Airflow 里的 SQL 任务，执行 M3 生成的 DDL** | 表结构必须由本体决定：COMMENT 由 `display_name`/`description` 反补、分区键来自契约、主键/外键以 TBLPROPERTIES 声明。若交给 SeaTunnel 的 auto-create schema，这些全部丢失——**本体反补注释是本项目的关键价值点，不能让搬运工具绕过** |
| 数据搬运 | SeaTunnel / DataX / Flink | 跨源、并行、断点、类型转换是它们的专业 |
| 编排与调度 | Airflow | 依赖序、重试、补数（backfill）、水位、并发闸门 |
| 血缘注册 | Airflow DataHub Plugin（主）+ ontoMeta emitter（兜底） | 作业级血缘天然产在执行侧 |
| 作业定义与依赖图 | ontoMeta | 它是唯一知道「本体 → 物理」映射的一方 |

---

## 4. 搬运工具选型

| 工具 | 强项 | 弱项 | 与本仓既有关系 |
|---|---|---|---|
| **SeaTunnel** | 批流一体；连接器覆盖广；支持 CDC；可跑在 Spark/Flink/Zeta 引擎上 | 版本间配置格式有变动 | **已有先例**：`app/agents/executors/sync.py` 就是产 SeaTunnel 作业配置的；Bigtop Manager 的 Extra stack 已纳管 SeaTunnel（`DW_IMPLEMENTATION.md:350`） |
| DataX | 单机部署简单、稳定、配置直观 | 单机无分布式；无 CDC；社区活跃度低 | 无 |
| Flink SQL | 流式与复杂转换最强；CDC 生态成熟 | 为批量映射搬运引入 Flink 集群，运维成本高 | Bigtop stack 含 Flink |

**推荐**：**SeaTunnel 作为默认**（与仓库既有 SyncExecutor 和 BM 纳管范围一致，零新增运维面），
但**按 Dialect Adapter 的同一套路做成可插拔**——`load_strategy=cdc` 的契约路由到 CDC 能力更强的
实现（SeaTunnel-CDC 或 Flink CDC），小规模/无 SeaTunnel 环境可切 DataX。

这与仓库既有约定同构：`app/warehouse/registry.py::get_adapter(engine)` 之于方言，
`app/warehouse/jobs/registry.py::get_job_adapter(tool)` 之于搬运工具。生成器主干**不含任何工具特定逻辑**。

**选哪个工具不由使用者逐次决定**（`services/sync_tool_resolver`）：工具是部署事实。
runner 通道（默认）下 ontoMeta 根本不指定工具——可搬性按 runner 声明的 `capabilities` 判、
档位由 runner 逐表自选（native 优先，搬不了的交 SeaTunnel）；docker 通道按
「本次所需装载方式 ∩ 工具能力 ∩ 镜像可用」挑，优先级 seatunnel > flink > datax。
设置页的 `sync_tool`（空 = 自动）是唯一的人工覆盖入口，决策结果进 preflight 与执行回执。

---

## 5. 调度：Airflow

**为什么是 Airflow 而不是 DolphinScheduler**：血缘自动注册是选型的决定性因素。DataHub 官方维护
Airflow 插件（`acryl-datahub-airflow-plugin`，基于 OpenLineage），能自动把 DAG/Task 注册为
DataFlow/DataJob 实体并把 inlets/outlets 连成 Dataset 级血缘；DolphinScheduler 无同等成熟度的方案。
⚠ **这是对 `DW_IMPLEMENTATION.md:351`「调度器(DolphinScheduler)」的架构决策变更**，方案落地时该行需同步更新。
⚠ 需实施前验证：插件版本 × Airflow 版本 × DataHub 版本的兼容矩阵（三方版本耦合，官方矩阵随版本变动）。

**DAG 投递方式**（两案，推荐 A）：

| | A. ontoMeta 生成 DAG 文件 | B. Airflow 侧 DAG Factory 反向拉取 |
|---|---|---|
| 机制 | 生成 `.py` 写入 git 仓库 / 共享卷，Airflow git-sync 加载 | Airflow 放一个工厂 DAG，解析时调 ontoMeta REST 拉 JobSpec 动态建 DAG |
| 优点 | **产物即治理制品**：可 diff、可 review、可回滚，与「制品可审计」一致；Airflow 不依赖 ontoMeta 在线 | 无需文件通道 |
| 缺点 | 需要一条文件投递通道（git 或共享卷） | DAG 解析期依赖 ontoMeta 可用；动态 DAG 难 review；ontoMeta 挂了调度即空 |

**触发与调度**：
- 弹窗「提交并运行」= 生成/更新 DAG 文件 + 调 Airflow REST 触发一次 DagRun
  （`POST /api/v1/dags/{dag_id}/dagRuns`，⚠ Airflow 2 与 3 的 REST 路径/鉴权不同，需按目标版本核实）。
- DagRun 的 `dag_run_id` 用**制品 id** 作确定性 id → 重复提交天然幂等，不会产生第二次运行。
- 契约的 `refresh_cron` 直接成为 DAG 的 `schedule`——**上一轮做的定时策略选择器到这里才真正生效**；
  `不定时`（空 cron）→ `schedule=None`，只能手动触发。
- 增量水位：Airflow 的 `data_interval_start` 注入 SeaTunnel 作业的 `${start_time}`，替换掉现在
  ETL SQL 里那个无人注入的 `:watermark` 占位符。

---

## 6. 血缘注册

**主路径（自动）**：Airflow 任务显式声明 inlets/outlets（Dataset URN），插件自动上报：
- `DataFlow` = DAG（一次物化 = 一个 DAG）
- `DataJob` = 每张表的 sync task
- `Dataset lineage` = 源表 URN → 目标表 URN

URN 两侧都是现成的：源侧就是 `ObjectType.source_ref`（本来就是 DataHub URN），
目标侧按目标数仓平台 + `库.表` 构造，`connectors/datahub.py::_extract_dataset_name` 已有反向解析可复用。

**列级血缘**：`Property.source_field_ref` → `SELECT src AS prop` 的映射表 M3 已经有了，
可直接产出 `fineGrainedLineages`。⚠ 需实施前验证目标 DataHub 版本对字段级血缘的支持程度。

**兜底路径**：ontoMeta 侧 `services/lineage_emitter.py` 在 DagRun 成功回调时直接向 DataHub 发血缘
（复用 M7 的 GraphQL 通道）。理由：插件版本不匹配、Airflow 未接入时不至于整条血缘断掉；
且与 M7 的 preview/apply 安全约束保持一致。**两条路径产同一份 URN，重复上报是幂等的**。

---

## 7. 代码改动面（文件级）

**新增**

| 文件 | 职责 |
|---|---|
| `app/warehouse/jobs/base.py` | `JobSpec` 数据类：源/目标/列映射/模式(full/incremental/cdc)/分区键/水位表达式；与工具无关 |
| `app/warehouse/jobs/{seatunnel,datax,flink}.py` | `render(JobSpec) -> dict/str`，各工具的配置渲染 |
| `app/warehouse/jobs/registry.py` | `get_job_adapter(tool)`，比照 `app/warehouse/registry.py` |
| `app/services/job_planner.py` | 逻辑计划 + 契约 → `JobSpec[]`，依赖序复用 `generate_dag()` |
| `app/services/airflow_dag_builder.py` | `JobSpec[] + DDL` → DAG 文件文本（含 inlets/outlets 声明） |
| `app/connectors/airflow.py` | Airflow REST 客户端（触发/查 DagRun/取日志 URL），比照 `connectors/bigtop_manager.py` 的写法与错误封装 |
| `app/services/lineage_emitter.py` | 兜底血缘上报 |

**修改**

| 文件 | 改动 |
|---|---|
| `app/services/materialization_runner.py` | `run()` 增 `execute_mode`：`orchestrated`（默认，产 DAG + 触发）/ `direct`（保留现有直连落库，**仅开发与本地验证用**）。不删旧路径——没有 Airflow 的开发机仍要能跑通 |
| `app/agents/{drafters,executors}/materialize.py` | Spec 增 `sync_tool` / `schedule` / `execute_mode`；Executor 产出 DAG 与作业配置作为制品内容，dry-run 展示将生成的 DAG 与任务列表 |
| 制品回执结构 | 增 `dag_id` / `dag_run_id` / `state` / `log_url` / `datahub_url`；新增 `GET /api/.../materialize/{artifact_id}/status` 供前端轮询 |
| `app/services/settings_service.py` | 增 Airflow 连接配置（endpoint / 鉴权 / DAG 投递路径），比照既有 `DatahubSetting` / `CubeSetting` 的 DB-backed 做法 |
| `frontend/.../MaterializeModal.tsx` | 按钮语义改「提交并运行」；回执区改为 DagRun 状态轮询 + Airflow/DataHub 跳转链接；`direct` 模式在 UI 上显式标注「开发模式，直连落库」 |
| `DW_IMPLEMENTATION.md` | 第 2 节六步闭环图、第 350–351 行的调度器归属（DolphinScheduler → Airflow）需同步更新 |

**凭据处理（沿用既有不变量）**：作业配置里只写数据源别名，实际连接串由 SeaTunnel 侧解析 /
Airflow Connection 提供——与 `app/agents/executors/sync.py` 现有做法完全一致，不新开口子。
Validation Gate 里既有的凭据字段扫描（`*_ref`/`*_alias` 放行，`password`/`secret`/`token` 阻断）
对新制品同样生效。

---

## 8. 分阶段落地

| 阶段 | 内容 | 外部依赖 | 验收标准 | 测试 |
|---|---|---|---|---|
| **M9** | JobSpec + SeaTunnel Adapter + JobPlanner | 无（纯生成） | fixture 本体产出作业配置；两次生成逐字节一致；配置内无任何凭据；不可搬运项显式 `unsupported` | `test_job_planner.py`、`test_sync_tool_adapters.py` |
| **M10** | DAG 生成 + Airflow REST 触发/回读 | Airflow 实例 | DAG 文件可被 Airflow 解析；同一制品重复提交只产生一个 DagRun；状态可回读 | `test_airflow_dag_builder.py`（解析用 `DagBag`）、`test_airflow_connector.py`（httpx MockTransport，比照 BM 测试） |
| **M11** | 血缘注册（插件为主 + 兜底 emitter） | DataHub + 插件 | 一次运行后 DataHub 能查到 源表→目标表 血缘与 DataJob | `test_lineage_emitter.py`（mock GraphQL，比照 `test_datahub_writeback.py`） |
| **M12** | CDC/水位、质量校验任务、DataX/Flink Adapter | Flink（可选） | `load_strategy=cdc` 路由到 CDC 实现；增量水位由 Airflow 注入并生效 | `test_incremental_watermark.py` |

M9 完全没有外部依赖，可以先做、先测、先看产物是否正确——**在引入任何新基础设施之前就能验证方案是否成立**。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **双写不一致**：orchestrated 与 direct 两条执行路径并存 | `execute_mode` 显式记入制品回执；UI 上 direct 标注「开发模式」；生产环境用配置禁用 direct |
| **DDL 被搬运工具绕过**，丢掉本体反补的注释/分区/主键 | 建表固定为 DAG 的第一个任务、执行 M3 生成的 DDL；SeaTunnel 侧显式关闭 auto-create schema |
| 三方版本耦合（Airflow × 插件 × DataHub） | M9 无依赖先行；M10/M11 前先在非生产实例验证兼容矩阵（比照 M7「首次 apply 必须在非生产实例」的既有约束） |
| DAG 文件投递通道（git-sync/共享卷）在目标环境不存在 | 备选方案 B（DAG Factory 反向拉取）已设计，切换成本限于 `airflow_dag_builder` 一个模块 |
| 大本体 DAG 任务数爆炸（734 对象） | 按分层/域切分多个 DAG；Airflow 侧配并发闸门；`generate_dag` 已有的环检测结果直接作为「不可编排项」显式列出 |
| 现有测试受影响 | `test_materialization_runner.py` 现有 7 条用例断言的是直连落库行为 → 保留为 `execute_mode=direct` 的用例，新增 orchestrated 用例，不删除既有覆盖 |

---

## 10. 待验证清单（不臆造，实施前逐条核实）

1. Airflow 版本（2.x / 3.x）→ REST API 路径、鉴权方式、`schedule` 参数名。
2. `acryl-datahub-airflow-plugin` 与目标 Airflow / DataHub 版本的兼容矩阵。
3. 目标 DataHub 版本对字段级血缘（`fineGrainedLineages`）的支持程度。
4. SeaTunnel 版本的配置格式（HOCON/JSON）、Hive Sink 对分区表覆盖写的支持、CDC 连接器可用性。
5. 目标环境是否具备 DAG 文件投递通道（git-sync 或共享卷）。
6. Bigtop Manager 纳管的 SeaTunnel 版本是否满足 4 的要求（BM Extra stack 1.0.0）。
