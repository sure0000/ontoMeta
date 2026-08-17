# 执行提示词

按阶段拆开，每条独立开一个会话。`README.md` 是唯一权威步骤来源，提示词只负责
约束行为边界（什么必须停下问人、什么绝不能自作主张）。

---

## 通用约束（每条提示词都已内嵌，改写时勿删）

1. **`deploy/benchmark/README.md` 是权威**。与提示词冲突以 README 为准；两者都没写的，停下问。
2. **不手写 ERPNext / DataHub 的 compose**。前者用官方 `frappe_docker/pwd.yml` 叠 `erpnext.override.yml`，后者用 `datahub docker quickstart`。
3. **版本号不许改**。Flink `1.13.6` 与 `tools/flink-sql-runner/sql-runner.jar` 是编译期绑定的，改版本要连带重编。
4. **口令只从 `.env` 读**，不回显到日志、不写进任何提交。
5. **每步做完必须验证**，验证不过不许往下走，也不许"先跳过后面再回来"。
6. **这批 compose 从未在真实 docker 上跑过**。发现与实际不符，改 `deploy/benchmark/` 下的文件并在报告里说明，不要绕过。
7. 需要 `sudo` 改系统文件、需要浏览器操作、镜像拉不动——**停下问人**，不要自行找替代路径。

---

## 提示词 0 · 预检（不启动任何服务）

```
你在一台准备部署 ontoMeta 验证环境的 Linux 机器上。仓库已 clone，工作目录是仓库根。
本轮只做预检，不启动任何服务、不建任何卷。

读 deploy/benchmark/README.md 全文，然后：

1. 核对宿主机现状：docker / docker compose 版本、可用内存、可用磁盘、
   已占用端口（8069 8080 8081 8082 8090 9002 3306 5432 5433）。
2. 校验 deploy/benchmark 下三个 compose 文件：
   docker compose -f <file> config  能否通过（用 .env.example 复制的临时 .env）。
3. 测镜像拉取速度：只拉最小的 postgres:16，计时。
   超过 3 分钟就判定镜像源没配好——停下报告，不要继续拉大镜像。
4. 检查 tools/flink-sql-runner/sql-runner.jar 是否存在（airflow.Dockerfile 要 COPY 它）。

输出一份预检报告：每项通过/不通过、不通过的具体原因、以及你认为需要人先处理的事项。
本轮不要修改任何文件，不要启动任何容器。
```

---

## 提示词 1 · 业务系统（README 步骤 0–2）

```
你在 ontoMeta 验证环境的 docker 宿主机上，工作目录是仓库根。
本轮目标：把 ERPNext 与 Odoo 起起来，各自建好只读账号，各自做出 baseline 快照。

权威步骤见 deploy/benchmark/README.md 步骤 0 到步骤 2，逐条执行。

硬约束：
- 镜像源没配好就停下问人。此前实测 Docker Hub 直连拉 4MB 镜像用了 6 分钟，
  大镜像根本拉不下来。不要试图硬拉，不要换 tag 碰运气。
- ERPNext 用官方 frappe_docker 的 pwd.yml 叠 deploy/benchmark/erpnext.override.yml，
  不要自己写 compose。若报 "service X not found"，用
  docker compose -f pwd.yml config --services 对一遍服务名，改 override 文件。
- Odoo 初始化必须带 --without-demo=all。demo 数据会污染后续造数的真值统计，
  这一条不能省，也不要"先带上以后再删"。
- Setup Wizard 是浏览器操作，你做不了。起完 ERPNext 后停下，把访问地址和
  要填的内容（公司/CNY/Asia-Shanghai/财年 1-1 到 12-31/科目表/仓库）告诉人，
  等人做完再继续后面的只读账号与快照。
- 数据库端口只对 tailnet 开放，不要绑到 0.0.0.0 之外的公网可达地址。
- 只读账号给采集用，绝不用 root。

完成判据：
- ERPNext web 可访问，MariaDB 端口从宿主机能连上
- Odoo web 可访问，odoo_o2c 库已初始化，Postgres 端口能连上
- 两个只读账号都能 SELECT，且都不能写
- 两份 baseline 快照文件已生成且大小合理
- docker stats 显示总内存占用在 17G 上下

报告：实际用到的服务名/端口/镜像版本、与 README 的每一处差异（并说明你是否已回改
deploy/benchmark 下的文件）、当前内存与磁盘占用、以及未完成项。
```

---

## 提示词 2 · 平台栈（README 步骤 6–7）

```
你在 ontoMeta 验证环境的 docker 宿主机上，工作目录是仓库根。
业务系统（ERPNext + Odoo）已就绪。本轮目标：起 DataHub、Airflow、Flink 会话集群、
目标数仓 Postgres，并验证它们互相连得通。

权威步骤见 deploy/benchmark/README.md 步骤 6 到步骤 7。

先按 README 步骤 5 停掉业务系统的应用容器（backend/frontend/websocket/queue-*/
scheduler 与 odoo），只留两个数据库——腾出 12G 给本轮。数据库不能停。

硬约束：
- DataHub 用官方 datahub docker quickstart，不要手写它的 compose（十来个服务，
  版本耦合紧）。起来后按 README 收敛 ES 与 GMS 的堆大小，停掉 datahub-actions。
- Flink 用 standalone 会话集群，不要装 YARN/Hadoop。
- Flink 版本锁 1.13.6，与 sql-runner.jar 编译期绑定，不许改。
- JDBC 驱动放 Flink 集群镜像（flink.Dockerfile），不要放 Airflow 镜像。
  放错边的症状是运行期 ClassNotFoundException，且报错指不到是哪边缺。
- Airflow 镜像里的 Java 必须是 11（Dockerfile 已从 temurin 多阶段拷入）。
  若构建失败不要改成 openjdk-17 —— Flink 1.13 在 17 上会因模块访问限制直接崩。
- 构建前记得 cp tools/flink-sql-runner/sql-runner.jar deploy/benchmark/

完成判据（逐条实际执行验证，不要凭日志推断）：
- curl DataHub GMS 的 /health 返回正常
- Airflow UI 可登录；用 basic auth 调 /api/v1/dags 返回 200 而不是 401
- 在 airflow-scheduler 容器里执行 flink list -t remote，能列出集群
- 目标数仓 Postgres 能用 dwh 账号建表（它要写，不是只读）
- docker stats 总占用在 26G 上下

报告：各服务实际版本与端口、每条完成判据的实测结果、与 README 的差异及你的回改、
以及内存占用明细。
```

---

## 提示词 3 · mac 侧接线（在 mac 的会话里执行）

```
你在 mac 上，工作目录是 ontoMeta 仓库根。这台机器只跑 ontoMeta 本体，
其余组件都在另一台 docker 宿主机上，两台机器通过 Tailscale 互通。
本轮目标：把 ontoMeta 接到那台机器的各组件上。

权威步骤见 deploy/benchmark/README.md 步骤 8。

必须处理的三件事：
1. 挂载 DAG 共享目录（NFS/SMB）。两侧挂载点不同没关系——DAG 里的 SQL 目录是
   Path(__file__).parent/"jobs" 运行期解析的，不写死绝对路径。但必须是同一份文件。
2. 改 backend/.env 的三个 Flink 变量。注意它们填的都是「Airflow 容器内」的值：
   - FLINK_BIN 当前是 /Users/me/local/flink/current/bin/flink，这是 mac 本地路径，
     不改的话会被原样拼进 DAG 的 bash 命令、在容器里执行，必然 No such file or directory，
     而且报错停在 BashOperator 上看不出根因。改成 flink。
   - FLINK_DEPLOY_TARGET 缺省 yarn-per-job，必须改成 remote，否则去找根本没部署的 YARN。
   - FLINK_SQL_RUNNER_JAR 填容器内路径 /opt/ontometa/sql-runner.jar。
3. 在设置页配依赖组件（真源是 dependency_components 表，不是 .env 也不是遗留的
   airflow_settings）：Airflow 的 endpoint 与 dags 目录、ERP 源库、Odoo 源库、目标数仓。
   目标数仓的 kind 必须是 postgres，否则 resolve_engine 会回退成 hive 方言，
   症状是建表成功但不搬数。

完成判据：
- 设置页里各组件拨测通过
- 从 mac 能连上宿主机的 MariaDB / Odoo PG / 目标 PG
- 投递一个 DAG 后，宿主机 /srv/ontometa/dags/ontometa/<artifact_id>/ 出现文件
- Airflow 能解析出该 DAG

报告：改了哪些配置项（口令不要回显）、每条判据的实测结果、未通过项及你的判断。
```

---

## 提示词 4 · 造数（**依赖未就绪**）

> ⚠ `generate_o2c.py` 尚未编写，本提示词现在还不能执行。
> 生成器要先按 `docs/BENCHMARK_ENV_SETUP.md` §4–§6 实现：中立台账 + 双向投递 + `truth.json`。

```
你在 ontoMeta 验证环境的 docker 宿主机上。ERPNext 与 Odoo 已就绪并已做 baseline 快照。
本轮目标：生成 6 个月 O2C 业务数据（含脏案例），产出 truth.json，做一致性校验与数据快照。

权威步骤见 deploy/benchmark/README.md 步骤 3 到步骤 4，
配方见 docs/BENCHMARK_ENV_SETUP.md §4-§8 与 docs/BENCHMARK_DATA_PREP.md §3.2-§3.6。

三条硬约束，破任一条整夜白跑：
1. 先造系统中立的业务台账、落 truth.json，再分别投递到两个系统。不要先在 ERPNext
   造再同步到 Odoo —— 那等于用被验证的对象生成金标准。
2. 严格按时间正序提交（2 月 → 7 月）。ERPNext 对回溯的库存单据会排
   Repost Item Valuation，乱序插入会触发成千上万次重算，队列积压到跑一整天也做不完，
   而且是在你以为跑完之后才慢慢显现。
3. 并发固定 8。提交是同步执行在 gunicorn worker 里的，worker 配的就是 8，开更大只会排队。

另外：
- 单据链必须走 ERPNext 的标准转换函数（make_delivery_note / make_sales_invoice /
  get_payment_entry），不要手工拼。它们会带出 against_sales_order、so_detail 等引用字段，
  履约周期、按时交付率、订单到发票的血缘全靠它们；手工拼的链条缺引用，指标会静默算错。
- Perpetual Inventory 保持开启。为提速关掉它，库存周转天数指标就没了。
- 造数预计 3-5 小时，用后台方式跑，定期回报进度，不要卡在前台等。

一致性校验（README 步骤 4 的六项）不通过，改生成器重跑，绝不手工补数据——
手工补的行不会进 truth.json，真值一旦和实际数据脱节，后面所有交叉校验全部失效。

报告：各表实际行数 vs 台账期望、六项校验逐项结果、耗时、快照文件与 manifest 内容。
```
