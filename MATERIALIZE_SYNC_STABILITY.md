# 同步执行稳定化方案：把失败从「运行三分钟后」提到「点提交之前」

> **本文是 `MATERIALIZE_ORCHESTRATION.md` 的修订，不是替代。**
> 那份文档定下的分工全部保留：ontoMeta 只产作业定义与依赖图、Airflow 负责编排调度、
> 搬运交专业工具、**建表固定走本体生成的 DDL**、凭据不进产物。
> 本文只改两件事——**搬运任务用什么通道落到执行侧**，以及**失败在什么时刻暴露**。
>
> 「现状」部分对照仓库实际代码核实并给出行号；「目标」部分尚未实现。
> 涉及外部系统版本与能力的断言一律标注「⚠ 需实施前验证」。

---

## 1. 症状不是一个 bug，是一类 bug

M10 上线后在真实实例上逐个踩到并修掉的（这些修复都已在代码注释里留了现场）：

| 报错原文 | 真实原因 | 修在哪 |
|---|---|---|
| `pull access denied for …, repository does not exist` | 选的工具在本部署没有可用镜像（DataX 无官方镜像） | [registry.py:47](backend/app/warehouse/jobs/registry.py:47) `resolve_docker_image` |
| `Exception rendering Jinja template … field 'command'` | DockerOperator 的 `template_ext` 默认含 `.sh`，把命令首元素当模板文件去读 | [airflow_dag_builder.py:273](backend/app/services/airflow_dag_builder.py:273) 清空 `template_ext` |
| `ParseException: extraneous input ';' expecting EOF` | 生成器产的是脚本形态 SQL（带 `;`），DAG 里逐条交给 DB-API | [airflow_dag_builder.py:102](backend/app/services/airflow_dag_builder.py:102) `_as_single_statement` |
| `No suitable driver found for ${ERP_READONLY_URL}` | SeaTunnel 的 `${…}` 只认 `-i`，不读环境变量 | [seatunnel.py:69](backend/app/warehouse/jobs/seatunnel.py:69) 改走 `-i` |
| `the options('table_name') are required` | Hive sink 要一个 `table_name`，不是 database/table 两项 | [seatunnel.py:145](backend/app/warehouse/jobs/seatunnel.py:145) |
| `ClassNotFoundException: com.mysql.cj.jdbc.Driver` | 搬运镜像因授权不带 JDBC 驱动 | [airflow_dag_builder.py:84](backend/app/services/airflow_dag_builder.py:84) `_driver_jars` 逐个挂 |

六条的共同点，比六条各自的原因重要得多：

1. **全部在提交之后才发生**，出现在 Airflow 任务日志最深处；
2. **报错文本都不指向真实原因**，读起来像环境故障；
3. **修完一条立刻换下一条**——说明问题不在某个参数写错，在**执行通道的形状**。

### 1.1 结构性原因：一次搬运要同时成立九件事

当前通道是「Airflow worker 经 docker.sock 起一个一次性兄弟容器跑 SeaTunnel」
（[airflow_dag_builder.py:293](backend/app/services/airflow_dag_builder.py:293)，
[docker-compose.yml:43](docker/orchestration/docker-compose.yml:43) 挂 sock）。它要求：

| # | 必须成立的事 | 由谁决定 | 提交时 ontoMeta 能验证吗 | 不成立时长什么样 |
|---|---|---|---|---|
| 1 | 工具镜像可拉 | 部署 | ✅ 已做 | — |
| 2 | worker 能访问 `/var/run/docker.sock` | 部署 | ❌ | `PermissionError` / 连不上 daemon |
| 3 | `jobs_host_dir` 是**宿主机**上的真实路径，且与 ontoMeta 写文件的目录是同一个 | 部署 | ❌ | 容器起来了，`--config` 指的文件不存在 |
| 4 | `docker_network` 名字正确 | 部署 | ❌ | `UnknownHostException: hive-metastore` |
| 5 | 驱动 jar 齐全且版本匹配目标库 | 部署 | ❌ | `ClassNotFoundException` / 协议不兼容 |
| 6 | Airflow Connection `erp_readonly`、`ontometa_ds_<slug>` 存在 | 部署 | ❌ | **渲染期抛错，全部任务一起红** |
| 7 | SeaTunnel 该版本的连接器参数形状与渲染一致 | 工具版本 | ❌ | `the options(...) are required` 之类 |
| 8 | Hive sink 所需的 hadoop conf / HDFS 可达 | 部署 | ❌ | 超时或权限拒绝 |
| 9 | DAG 已被 Airflow 解析完成 | 时序 | ❌ | 触发 404，且[被吞掉](backend/app/services/materialization_runner.py:198)只记进回执 |

**九件事里只验证了一件。** 第 3 条尤其隐蔽：ontoMeta 写的是**自己文件系统**的路径，
DAG 里却当**宿主机**路径挂载——ontoMeta 一旦容器化部署，这两个路径必然不同，
而且没有任何一侧会报错，只是挂进去一个空目录。

这种结构靠逐个修参数收敛不了：每加一个部署环境，就重走一遍这九关。

### 1.2 三个与「跑不起来」同样贵的语义缺陷

| 缺陷 | 位置 | 后果 |
|---|---|---|
| 全量模式 sink 是 `DROP_DATA`，**先删后写、无回滚** | [seatunnel.py:141](backend/app/warehouse/jobs/seatunnel.py:141) | 搬到一半失败 = 目标表被清空，且原数据没了。这是最贵的一种「不稳定」 |
| 凭据经 `-i KEY=值` 走命令行 | [seatunnel.py:69](backend/app/warehouse/jobs/seatunnel.py:69) | 明文进 `docker inspect`、进容器内 `ps`、进 Airflow 的 rendered fields |
| 一个本体一个 DAG、一个 DagRun 装全部表，层内无闸门、`max_active_runs=1` | [airflow_dag_builder.py:321](backend/app/services/airflow_dag_builder.py:321) | 734 张表 = 734 个并发容器；任一张表失败，整轮显示为红；重跑粒度只有「整轮」 |
| DAG 的 `schedule` 取各表 cron 的**众数** | [materialization_runner.py:103](backend/app/services/materialization_runner.py:103) | 少数派表的定时策略静默失效，UI 上却显示已配置 |

---

## 2. 目标

**把九件必须同时成立的事压到三件，并且这三件在点「提交」之前全部可验证。**

| | 现状 | 目标 |
|---|---|---|
| 执行单元 | 每表一个一次性容器，由 worker 经 docker.sock 起 | 常驻 sync-runner 服务，Airflow 只发一次 HTTP |
| 作业配置传递 | 落宿主机目录 → bind mount 进容器 | 随请求体传输，不落盘、不挂载 |
| 驱动 | 部署方提供 jar，逐个 bind mount | 构建期烘进 runner 镜像 |
| 凭据 | Airflow Connection → 渲染进命令行 | runner 按 alias 自解析，Airflow 与产物两侧都不碰 |
| 失败暴露时刻 | 提交后 1–3 分钟，任务日志最深处 | 提交前，preflight 逐项给出可执行的下一步 |
| 全量落地 | 先 DROP 后写 | staging 表 → 校验 → 原子切换 |
| 失败粒度 | 整个 DagRun | 单表任务，可单独重跑 |

---

## 3. 方案

### 3.1 执行通道：常驻 sync-runner + HTTP 调用

```
Airflow DAG（纯 PythonOperator，不需要 docker provider）
   │  POST /jobs {job_spec, alias, mode, watermark}
   ▼
ontometa-sync-runner（常驻服务，仓库内自建镜像）
   ├─ backend=native   ：内置 JDBC 分批搬运（默认）
   └─ backend=seatunnel：转调 SeaTunnel Zeta REST / 本地 shell
   │
   ├─ 凭据：按 alias 从自己的 secrets 解析，请求体里只有 alias
   ├─ 驱动：构建期烘进镜像，运行期零挂载
   └─ 落地：staging 表 → 行数校验 → 原子切换
```

一次改动消掉的失败模式：**#2 docker.sock、#3 宿主机路径、#4 网络名、#5 驱动挂载、
#7 连接器参数（native 档不存在）、以及 DockerOperator 的 `template_ext` 陷阱**。
Airflow 侧从此只剩「发一个 HTTP、轮询状态」，没有任何环境耦合。

**接口契约**（runner 侧，版本化）：

| 端点 | 用途 | 谁调 |
|---|---|---|
| `GET /healthz` | 存活 | preflight、Airflow |
| `GET /capabilities` | `contract_version` + 支持的源/目标平台 + 已装驱动清单 + backend 档位 | preflight |
| `POST /probe` | `{alias}` → 该连接能否连通、能否读到指定表 | preflight |
| `POST /jobs` | 提交一个 JobSpec，返回 `job_id`（**幂等键 = `dag_run_id + task_id`**，重复提交返回同一个） | Airflow task |
| `GET /jobs/{id}` | 状态、已搬行数、水位、错误 | Airflow task 轮询 |
| `GET /jobs/{id}/log` | 该作业日志 | 回执跳转 |

**为什么是「常驻服务」而不是「Airflow worker 里直接 PythonOperator 搬」**——
后者组件更少，但 ontoMeta **无法在提交前问它任何问题**：想知道「能不能连到源库」，
只能跑一个 DAG 试。而「提交前可验证」正是这次改造的全部目的，所以多一个可被直接询问的
服务是值得的。附带好处：JDBC 驱动与数据库客户端不必塞进 Airflow 镜像，不与 Airflow
自身依赖打架。

**凭据归属**：runner 按 alias 从自己的 secrets 后端解析
（`SYNC_CONN_<ALIAS>_{URL,USER,PASSWORD}` 环境变量，或挂载的 secrets 目录），
Airflow Connection 不再参与搬运（仍用于 `create_tables` 的 SQL 任务）。
这样凭据只有一个归属地，`POST /probe` 才有意义，且顺带消掉了失败模式 #6 里
「Connection 不存在导致渲染期全部任务爆炸」。**「凭据不进产物」不变量不变**：
DAG、spec、作业配置里始终只有 alias。

**native backend 的能力边界**（不吹）：按主键/分区键分片、批量 `SELECT` → 批量写入，
覆盖 full 与 incremental。**CDC 不做**，`load_strategy=cdc` 的表路由到 seatunnel 档；
runner 的 `GET /capabilities` 如实声明，planner 据此把不支持的表列进 `unsupported`——
沿用既有的「不静默降级」约定（[job_planner.py:118](backend/app/services/job_planner.py:118)）。

### 3.2 提交前自检（Preflight Gate）

新增 `POST /api/warehouse/materialize/preflight`，物化弹窗在「提交」前必须跑一次，
未全绿则按钮禁用（可显式忽略非阻断项）。每一项失败都给**可执行的下一步**，
照 [preflight.sh](docker/orchestration/preflight.sh) 的风格，不只是报错。

| 检查项 | 怎么检 | 失败时给什么 |
|---|---|---|
| Airflow 可达 | `GET /health` | endpoint 是否写错、是否被反向代理挡了登录页 |
| Airflow API 鉴权真的可用 | `GET /api/{v}/dags?limit=1`（[ping_api](backend/app/connectors/airflow.py:120) 已实现） | `AIRFLOW__API__AUTH_BACKENDS` 该怎么配 |
| REST 版本 | `GET /openapi.json` 自探 v1/v2，不照抄文档 | 自动纠正 `api_version`，并在回执里说明 |
| runner 可达且契约匹配 | `GET /capabilities`，比对 `contract_version` | 版本不匹配即拒绝提交，并给出该升哪一侧 |
| 源库可连 | `POST /probe {source_alias}` | 该 alias 在 runner 侧的 secret 没配 / 网络不通 |
| 目标仓可连 | `POST /probe {target_alias}` | 同上 |
| 目标平台有 sink 实现 | `capabilities.sinks` 包含目标 engine | 换 backend 档位或换目标引擎 |
| DAG 目录双向可见 | ontoMeta 写一个 sentinel 文件，`GET /dags` 里能否看到它被解析 | **专治失败模式 #3**：路径两侧不一致会在这里现形 |
| 建表连接可用 | Airflow `GET /connections/{warehouse_conn_id}` | 该建哪个 conn_id、指向哪 |
| 批次规模 | 表数 vs `max_tasks_per_dag` | 将拆成几个 DAG、每批多少张表 |

⚠ 需实施前验证：Airflow 2.x 的 `GET /connections/{id}` 需要相应权限；只读账号可能 403，
此时降级为「无法确认」而不是「失败」。

### 3.3 落地语义：staging + 原子切换、水位回读

| 项 | 做法 | 理由 |
|---|---|---|
| 全量 | 写 `<表>__stg_<run_id>` → 行数/非空校验 → **原子切换**到正式表 | 搬到一半失败时正式表原封不动。现在的 `DROP_DATA` 是先删后写，失败即数据丢失 |
| 增量 | 水位由 runner **回读目标表** `max(<分区键>)`，不信 `data_interval_start` | 手动触发、`catchup=False`、补数三种场景下 data_interval 与「上次成功到现在」并不等价，会漏数或重复 |
| 幂等 | staging 名带 `run_id`；`POST /jobs` 幂等键 = `dag_run_id + task_id` | 重跑不撞表、不重复搬 |
| 回执 | 每表记 `rows_read / rows_written / watermark_before / watermark_after` | 「跑成功了但没数据」当前完全看不出来 |

⚠ 需实施前验证：各引擎的原子切换语法与代价不同——Doris 有 `ALTER TABLE … REPLACE WITH TABLE`，
Hive 分区表走 `INSERT OVERWRITE` 到目标分区，非分区表只能 rename 两次（有短暂窗口）。
这一层落在 Dialect Adapter 里（与建表 DDL 同一处），不进 runner。

### 3.4 DAG 形状：按 cron 分组 + 分批 + 闸门 + 等解析

| 改动 | 现状 | 目标 |
|---|---|---|
| schedule | 取众数，少数派静默失效 | **一个 cron 一个 DAG**：`ontometa_materialize_<本体短id>__<cron哈希>`；无 cron 的表进 `__manual` DAG |
| 单 DAG 规模 | 全部表塞一个 DAG | 上限 `ONTOMETA_MAX_TASKS_PER_DAG`（默认 50），超出按层/域分批，回执列出批次与各自的 run_url |
| 并发 | 层内一次性全放开 | Airflow pool + `max_active_tasks`，可配 |
| 重试 | 无 | task 级 `retries=2`, 指数退避；搬运是可重跑的（staging 保证） |
| 触发时序 | 落盘后立刻触发，404 被吞成回执里的 error | 落盘后**轮询 `GET /dags/{dag_id}` 直到出现**（超时 60s ⚠ 待实测解析间隔），再触发；超时给「Airflow 尚未解析到 DAG，请检查 dags 目录是否双向可见」 |

### 3.5 版本锁定

- runner 镜像随 ontoMeta 一起发版，`GET /capabilities` 返回 `contract_version`；
  ontoMeta 只接受自己认识的版本，不匹配即拒绝提交（而不是发过去再看会不会炸）。
- seatunnel 档位显式声明支持的版本区间，**未知版本拒绝渲染**——现在的渲染器对真实版本
  一无所知，参数形状全靠实测撞出来（`table_name` 那条就是这么来的）。

---

## 4. 改动面（文件级）

**新增**

| 路径 | 职责 |
|---|---|
| `docker/sync-runner/Dockerfile` | runner 镜像：Python + 各 JDBC/DB 驱动，构建期装齐 |
| `sync_runner/`（独立小服务） | FastAPI：`healthz` / `capabilities` / `probe` / `jobs`；backend 分 `native` 与 `seatunnel` 两档 |
| `app/connectors/sync_runner.py` | ontoMeta 侧客户端（比照 `connectors/airflow.py` 的错误封装与 `trust_env=False`） |
| `app/services/materialize_preflight.py` | §3.2 的逐项检查，返回结构化结果 |
| `app/api/warehouse.py::preflight` | 暴露给弹窗 |

**修改**

| 文件 | 改动 |
|---|---|
| [airflow_dag_builder.py](backend/app/services/airflow_dag_builder.py) | DAG 模板由 DockerOperator 改 PythonOperator（`inlets`/`outlets` 保留，M11 血缘不受影响）；按 cron 分组、按上限分批；spec 里去掉 `jobs_host_dir` / `driver_jars` / `docker_network` |
| [materialization_runner.py](backend/app/services/materialization_runner.py) | 提交前调 preflight；`_schedule_of` 众数逻辑删除，改分组；触发前等解析 |
| [seatunnel.py](backend/app/warehouse/jobs/seatunnel.py) | `data_save_mode` 不再用 `DROP_DATA`，改写 staging；凭据不再进 `-i`（runner 自解析） |
| [job_planner.py](backend/app/services/job_planner.py) | 按 runner 的 `capabilities` 判定可搬性，替代现在的硬编码平台表 |
| `settings_service.py` / `config.py` | 增 runner endpoint；`airflow_sync_drivers_dir` / `sync_tool_images` / `airflow_docker_network` 随 docker 通道一起标记为 deprecated |
| `MaterializeModal.tsx` | 提交前展示 preflight 结果；回执按批次展示多个 DagRun |

**保留（不删）**

现有 DockerOperator 通道保留在 `sync_channel=docker` 下，默认切到 `runner`。
理由与 M9 保留 `direct` 同：**已经跑通过的路径不在改造期一起动**，出问题可一键切回对照。

---

## 5. 分阶段落地

| 阶段 | 内容 | 外部依赖 | 验收标准 | 测试 |
|---|---|---|---|---|
| **M13** | Preflight Gate（§3.2） | 无（对现有通道也生效） | 九类失败中 #1/#3/#6/#9 在提交前被拦下，且提示能直接照做 | `test_materialize_preflight.py`（httpx MockTransport，比照 `test_airflow_connector.py`） |
| **M14** | sync-runner + native backend + `sync_channel=runner` | runner 镜像 | ERP MariaDB → Doris 单表全量搬通，产物与 spec 里无凭据、无宿主机路径 | `test_sync_runner_client.py`、runner 侧 `test_native_backend.py` |
| **M15** | staging + 原子切换 + 水位回读（§3.3） | 目标数仓 | 人为让作业中途失败，正式表数据不变；重跑后行数正确、不重复 | `test_staging_swap.py`（各 Dialect 的切换语句 golden） |
| **M16** | DAG 分组分批 + 闸门 + 等解析（§3.4） | Airflow | 734 张表提交后产出 N 个 DAG，每个 ≤50 任务；单表失败可单独重跑；各表 cron 均生效 | `test_airflow_dag_builder.py` 扩充（`DagBag` 解析 + 分批断言） |

**M13 没有任何新依赖，且对现在这条通道立刻有效**——先做它，把「三分钟后才知道」这件事
本身解决掉，后面几阶段就变成可控的迭代，而不是一次次线上试错。

---

## 6. 如果不接受新增 runner 服务

§3.2 / §3.3 / §3.4 与执行通道**无关**，可以单独落地，收益是这份方案的大部分：

- 提交前自检 → 消掉「三分钟后才知道」；
- staging + 原子切换 → 消掉「失败即清空目标表」；
- 分组分批 + 等解析 → 消掉「整轮红」「cron 静默失效」「首次提交必失败」。

留在 DockerOperator 通道上的残余风险仍是失败模式 #2/#3/#4/#5/#7——preflight 能**检出**
#3，检不出 #2/#4/#5（它们只有真起容器时才知道）。这是这条退路的明确代价。

---

## 7. 风险与取舍

| 风险 | 缓解 |
|---|---|
| runner 是新组件，多一个要运维的东西 | 单进程、无状态、无外部依赖（驱动烘进镜像）；挂了 preflight 立刻红，不会静默 |
| native backend 覆盖不了大表 / CDC | `capabilities` 如实声明，超出的表路由到 seatunnel 档并在 `unsupported` 列出；不静默降级 |
| 原子切换各引擎语法不同，Hive 非分区表有窗口 | 落在 Dialect Adapter（与建表 DDL 同处）；无法原子切换的组合显式 `unsupported`，不假装能做 |
| 两条通道并存期的行为差异 | `sync_channel` 记进回执与制品；两条通道跑同一组 fixture 的对照测试 |
| preflight 变成走过场（用户一律忽略） | 阻断项与提醒项分开：阻断项不可忽略，提醒项才可 |
| 凭据归属从 Airflow 移到 runner，迁移期两边都要配 | M14 期间 runner 支持「回退读 Airflow Connection」，M16 后移除 |

---

## 8. 待验证清单

1. Airflow 的 dags 目录被扫描到的实际延迟（`dag_dir_list_interval` 默认 300s ⚠，若是这个量级，
   §3.4 的「等解析」超时要相应放大，或改用 `POST /dags/{id}/parse`（Airflow 版本相关）。
2. Airflow 2.x `GET /connections/{id}` 对只读账号的权限行为。
3. Doris `ALTER TABLE … REPLACE WITH TABLE` 的原子性与代价；Hive 非分区表切换的可行做法。
4. 目标 SeaTunnel 版本的 Zeta REST 提交接口形状（seatunnel 档要用）。
5. `inlets`/`outlets` 在 PythonOperator 上的血缘上报行为与 DockerOperator 是否一致
   （DataHub 插件基于 OpenLineage，理论上与 Operator 类型无关，但要实测）。
6. native backend 在 ERP 最大表上的吞吐，确定「多大算大表、该路由到 seatunnel」的阈值。
