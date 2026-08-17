# 执行提示词

**要一口气跑完造数，用下面「一条龙」那条。** 想分阶段精细控制，用后面拆开的 0–4。

---

## 一条龙 · 从零跑完造数（docker 宿主机）

覆盖 README 步骤 0–4：起两个业务系统 → 生成 6 个月数据 → 一致性校验 → 打快照。
不含 DataHub/Airflow/Flink（那是提示词 3）。中途只有一处必须人工介入：ERPNext 的
Setup Wizard 是浏览器操作。

```
你在一台 Linux 机器上（docker 已装，分配 48GB 内存 / 200GB 存储），要为 ontoMeta 的
有效性验证准备业务语料。仓库已取到本地，工作目录是仓库根。

目标：从零跑完造数——起 ERPNext 与 Odoo，生成 6 个月 O2C 业务数据（含脏案例与跨系统
冲突），产出 truth.json，通过一致性校验，打出数据快照。
不涉及 DataHub/Airflow/Flink，那是下一轮的事。

权威步骤：deploy/benchmark/README.md 步骤 0-4，以及 benchmark/README.md。
与本提示词冲突时以 README 为准；两者都没写的，停下问我。

## 执行顺序

0. 预检，不启动任何服务
   - 确认代码齐全：benchmark/ 下的 generate_o2c.py、verify.py、generator/、tests/，
     deploy/benchmark/ 下的 compose.biz.yml、erpnext.override.yml。缺就停下报告。
   - python3 -m pytest benchmark/tests -q          应 25 条全绿
   - cd benchmark && python3 generate_o2c.py --only ledger --out /tmp/truth-pre.json
     看八个指标量级：DSO 约 55 天、按时交付率约 75%、毛利率约 27%、账龄四档都有钱。
     差一个数量级就停下报告，别往下走。
   - 检查端口占用（8069 8090 3306 5432）与可用内存
   - 拉 postgres:16 并计时；超过 3 分钟判定镜像源没配好，停下问我，不要硬拉

1. 起 ERPNext。用官方 frappe_docker 的 pwd.yml 叠 deploy/benchmark/erpnext.override.yml，
   不要自己写 compose。报 "service X not found" 就用
   docker compose -f pwd.yml config --services 对服务名，改 override 文件。

2. 停下让我做 Setup Wizard——浏览器操作你做不了。把访问地址和要填的内容告诉我：
   公司名 / 币种 CNY / 时区 Asia-Shanghai / 财年 1-1 到 12-31 / 科目表 / 默认仓库。

3. 我做完后：建只读账号、在界面里关掉 Version 记录与邮件通知（tabVersion 这张表要
   保留，它是框架噪声表的分母，只是不需要几十万行）、生成 API 密钥、
   mysqldump 出 baseline 快照。

4. 起 Odoo 并初始化：
   odoo -d odoo_o2c -i sale,crm,stock,account,sale_management,l10n_generic_coa
        --without-demo=all --stop-after-init
   建只读账号，pg_dump 出 baseline 快照。

5. 照 benchmark/.env.example 填 benchmark/.env。密钥不要回显到日志或报告里。

6. 冒烟：pip install requests
   cd benchmark && python3 generate_o2c.py --orders 40 --out /tmp/truth-smoke.json
   两个投递器从没在真实系统上跑过，这一步大概率要修字段名或方法签名。
   修 generator/deliver_*.py，改了什么写进报告。

7. 冒烟通过后把两个库恢复到 baseline 快照——那 40 张单会混进最终数据，
   让实际行数与 truth.json 对不上。

8. 全量：nohup python3 generate_o2c.py --seed 42 --concurrency 8 \
     --out /srv/ontometa/benchmark/truth.json > /srv/ontometa/benchmark/gen.log 2>&1 &
   预计 3-5 小时。后台跑，每半小时回报一次进度，不要卡在前台等。

9. 校验：pip install pymysql psycopg2-binary
   python3 verify.py --truth /srv/ontometa/benchmark/truth.json \
     --erp-dsn mysql://<ro>:<pwd>@localhost:3306/_erpnext \
     --odoo-dsn postgres://<ro>:<pwd>@localhost:5432/odoo_o2c \
     --manifest /srv/ontometa/benchmark/manifest.json

10. 全绿后打数据快照，并把镜像 digest 与 ERPNext/Odoo 版本号补进 manifest 的
    images 字段——否则半个月后重建环境说不清拿到的是不是同一套 schema。

## 必须停下问我的点
- 镜像拉不动
- 需要 sudo 改系统文件（/etc/docker/daemon.json 等）
- 第 2 步的 Setup Wizard
- 预检任一项不通过
- 校验反复不过、你判断需要改配方

## 不可违反
1. 不要改配方（generator/config.py），也不要改台账逻辑（ledger.py / truth.py）。
   台账是真值的唯一来源，改了 truth.json 就不再是真值。
2. 不要把流程改成「先在 ERPNext 造再同步到 Odoo」。现在是先造中立台账再双向投递；
   反过来等于用被验证的对象生成金标准，跨系统题的分数就没意义了。
3. 不要绕开 ERPNext 的标准转换函数（make_delivery_note / make_sales_invoice /
   get_payment_entry）。它们带出 against_sales_order、so_detail 等引用字段，
   履约周期、按时交付率、订单到发票的血缘全靠它们；手工拼的链条缺引用会让指标静默算错。
4. 不要打乱时间正序、不要调大并发。生成器按单据日期推进、日内 SO→DN→SI→PE 分阶段，
   是为了避开 ERPNext 的 Repost Item Valuation 重算风暴——乱序会让队列积压到跑一整天
   也做不完，而且是在你以为跑完之后才慢慢显现。并发 8 与 gunicorn worker 数一致，
   提交是同步执行在 web worker 里的，开更大只会排队。
5. Odoo 初始化必须带 --without-demo=all。demo 数据会混进真值统计，
   让「两系统重复客户有多少」这类题的答案对不上。
6. verify.py 的 SKIP 不等于 PASS。缺驱动或缺 DSN 时该项记 SKIP 且整体返回非零，
   补齐再跑，别拿带 SKIP 的结果当验收依据。
7. 校验不过就改生成器、恢复 baseline、重跑。绝不手工补数据——手工补的行不进
   truth.json，真值一旦和实际脱节，后面所有交叉校验全部失效。
8. 数据库端口只对 tailnet 开放，不要绑到公网可达地址。只读账号给采集用，绝不用 root。

## 完成判据
- verify.py 全项 PASS、零 SKIP、退出码 0
- 实际行数与 truth.json 的 expected_erp / expected_odoo 逐项相符
- 两个库的 baseline 快照与数据快照都在，manifest.json 的 images 字段已补全

## 报告
每完成一个编号步骤简报一次。最终报告：verify.py 逐项结果、实际行数 vs 预期、总耗时、
改了投递器哪些地方（这批代码没在真实环境跑过，改动是预期内的）、
快照文件清单与 manifest 内容。
```

---

# 分阶段提示词

按阶段拆开，每条独立开一个会话，顺序执行。`deploy/benchmark/README.md` 是唯一权威步骤
来源，提示词只负责划边界——什么必须停下问人、什么绝不能自作主张。

> **前置**：`benchmark/` 目录（生成器与校验脚本）此前未纳入版本库。若你在目标机器上是
> `git clone/pull` 取的代码，先确认它在——提示词 0 第一件事就是查这个。

| 顺序 | 提示词 | 在哪台机器 | 对应 README |
|---|---|---|---|
| 1 | 0 · 预检 | docker 宿主机 | — |
| 2 | 1 · 业务系统 | docker 宿主机 | 步骤 0–2 |
| 3 | 2 · 造数与校验 | docker 宿主机 | 步骤 3–4 |
| 4 | 3 · 平台栈 | docker 宿主机 | 步骤 5–7 |
| 5 | 4 · ontoMeta 接线 | **mac** | 步骤 8 |

---

## 通用约束（已内嵌进每条，改写时勿删）

1. **`deploy/benchmark/README.md` 是权威**。与提示词冲突以 README 为准；两者都没写的，停下问。
2. **不手写 ERPNext / DataHub 的 compose**。前者用官方 `frappe_docker/pwd.yml` 叠 `erpnext.override.yml`，后者用 `datahub docker quickstart`。
3. **版本号不许改**。Flink `1.13.6` 与 `tools/flink-sql-runner/sql-runner.jar` 编译期绑定。
4. **口令只从 `.env` 读**，不回显到日志、不写进任何提交。
5. **每步做完必须验证**，验证不过不许往下走，也不许"先跳过后面再回来"。
6. **compose、Dockerfile、两个投递器都没在真实环境跑过**。发现与实际不符，改仓库里的文件并在报告里说明，不要绕过。
7. 需要 `sudo` 改系统文件、需要浏览器操作、镜像拉不动——**停下问人**。

---

## 提示词 0 · 预检（不启动任何服务）

```
你在一台准备部署 ontoMeta 验证环境的 Linux 机器上。仓库已取到本地，工作目录是仓库根。
本轮只做预检：不启动任何服务、不建任何卷、不修改任何文件。

读 deploy/benchmark/README.md 全文，然后按顺序检查：

1. 代码是否齐全。确认这些存在：
   benchmark/generate_o2c.py, benchmark/verify.py, benchmark/generator/, benchmark/tests/
   deploy/benchmark/ 下的 compose.biz.yml, compose.exec.yml, erpnext.override.yml,
   airflow.Dockerfile, flink.Dockerfile
   tools/flink-sql-runner/sql-runner.jar
   缺任何一项就停下报告——代码没传全，后面全都做不了。

2. 台账层自检（不需要任何外部系统，零依赖）：
   python3 -m pytest benchmark/tests -q          → 应 25 条全绿
   cd benchmark && python3 generate_o2c.py --only ledger --out /tmp/truth-preflight.json
   把 metrics_observation_window 里的八个指标贴进报告。参考量级：
   DSO 约 55 天、按时交付率约 75%、毛利率约 27%、账龄四档都要有钱。
   差一个数量级说明配方或真值算法坏了，停下报告，别往下走。

3. 宿主机现状：docker / docker compose 版本、可用内存、可用磁盘、
   已占用端口（8069 8080 8081 8082 8090 9002 3306 5432 5433）。

4. compose 文件语义校验（用 .env.example 复制的临时 .env）：
   docker compose -f <file> config  能否通过。

5. 镜像拉取测速：只拉最小的 postgres:16 并计时。
   超过 3 分钟就判定镜像源没配好——停下报告，不要继续拉大镜像。

输出预检报告：每项通过/不通过、不通过的具体原因、需要人先处理的事项。
本轮不要修改任何文件，不要启动任何容器。
```

---

## 提示词 1 · 业务系统（README 步骤 0–2）

```
你在 ontoMeta 验证环境的 docker 宿主机上，工作目录是仓库根。预检已通过。
本轮目标：起 ERPNext 与 Odoo，各自建只读账号，各自做出 baseline 快照，
并把造数需要的凭据写进 benchmark/.env。

权威步骤见 deploy/benchmark/README.md 步骤 0 到步骤 2，逐条执行。

硬约束：
- 镜像源没配好就停下问人。此前实测 Docker Hub 直连拉 4MB 镜像用了 6 分钟，
  大镜像根本拉不下来。不要试图硬拉，不要换 tag 碰运气。
- ERPNext 用官方 frappe_docker 的 pwd.yml 叠 deploy/benchmark/erpnext.override.yml，
  不要自己写 compose。若报 "service X not found"，用
  docker compose -f pwd.yml config --services 对一遍服务名，改 override 文件。
- Odoo 初始化必须带 --without-demo=all。demo 数据会污染造数的真值统计，
  这条不能省，也不要"先带上以后再删"。
- Setup Wizard 是浏览器操作，你做不了。起完 ERPNext 后停下，把访问地址和要填的内容
  （公司名 / 币种 CNY / 时区 Asia-Shanghai / 财年 1-1 到 12-31 / 科目表 / 默认仓库）
  告诉人，等人做完再继续。
- 数据库端口只对 tailnet 开放，不要绑到公网可达地址。只读账号给采集用，绝不用 root。

做完 Setup Wizard 后还要做三件事：
- 在 ERPNext 界面关掉 Version 记录、Email Queue、Notification（写噪声，拖慢提交）。
  tabVersion 这张表要保留——它是框架噪声表的分母，只是不需要几十万行。
- 生成 API 密钥：用户列表 → Administrator → 设置 → API 访问 → 生成密钥。
  api_secret 只显示一次。
- cp benchmark/.env.example benchmark/.env 并填写：ERP_URL / ERP_API_KEY /
  ERP_API_SECRET / ERP_COMPANY / ERP_WAREHOUSE / ODOO_URL / ODOO_DB / ODOO_PASSWORD。
  这个文件已被 .gitignore 覆盖。**不要把密钥回显到日志或报告里。**

完成判据：
- ERPNext web 可访问，MariaDB 端口从宿主机能连上
- Odoo web 可访问，odoo_o2c 库已初始化，Postgres 端口能连上
- 两个只读账号都能 SELECT、都不能写
- 两份 baseline 快照文件已生成且大小合理
- benchmark/.env 已填全（只报告"已填"，不要报告值）
- docker stats 总内存占用在 17G 上下

报告：实际服务名/端口/镜像版本、与 README 的每一处差异（并说明你是否已回改仓库文件）、
内存与磁盘占用、未完成项。
```

---

## 提示词 2 · 造数与校验（README 步骤 3–4）

```
你在 ontoMeta 验证环境的 docker 宿主机上，工作目录是仓库根。
ERPNext 与 Odoo 已就绪，baseline 快照已做，benchmark/.env 已填好。
本轮目标：生成 6 个月 O2C 业务数据，产出 truth.json，通过一致性校验，打数据快照。

权威步骤见 deploy/benchmark/README.md 步骤 3-4 与 benchmark/README.md。
配方在 benchmark/generator/config.py，不要改它（改了要重跑全部对照组）。

按五步走，不要跳：
1. python3 -m pytest benchmark/tests -q                     # 25 条全绿再往下
2. cd benchmark && python3 generate_o2c.py --only ledger --out /srv/ontometa/benchmark/truth.json
   人工看八个指标是否合理
3. pip install requests
   python3 generate_o2c.py --orders 40 --out /tmp/truth-smoke.json    # 冒烟
4. 冒烟通过后**必须把两个库恢复到 baseline 快照**——那 40 张单会混进最终数据，
   让实际行数与 truth.json 对不上
5. nohup python3 generate_o2c.py --seed 42 --concurrency 8 \
     --out /srv/ontometa/benchmark/truth.json > /srv/ontometa/benchmark/gen.log 2>&1 &
   预计 3-5 小时。用后台跑，定期回报进度，不要卡在前台等。

三条硬约束（生成器已实现，你的任务是别把它们改坏）：
1. 先造系统中立的台账、落 truth.json，再分别投递。不要改成先在 ERPNext 造再同步到
   Odoo —— 那等于用被验证的对象生成金标准，跨系统档的分数就没意义了。
2. 按单据日期的时间轴推进，日内按 SO→DN→SI→PE 分阶段。ERPNext 对回溯的库存单据会排
   Repost Item Valuation，乱序会触发成千上万次重算，队列积压到跑一整天也做不完，
   而且是在你以为跑完之后才慢慢显现。
3. 并发固定 8，与 gunicorn worker 数一致。提交是同步执行在 web worker 里的，
   开更大只会排队。

两个投递器从未在真实 ERPNext/Odoo 上跑过，冒烟阶段大概率要修字段名或方法签名。
修 benchmark/generator/deliver_*.py 并在报告里说明改了什么。
但**不要为了跑通而绕开 ERPNext 的标准转换函数**（make_delivery_note /
make_sales_invoice / get_payment_entry）——它们带出 against_sales_order、so_detail
等引用字段，履约周期、按时交付率、订单到发票的血缘全靠它们，手工拼的链条缺引用会让
指标静默算错。也**不要改台账逻辑**，那会让 truth.json 不再是真值。

跑完做一致性校验：
   pip install pymysql psycopg2-binary
   python3 verify.py --truth /srv/ontometa/benchmark/truth.json \
     --erp-dsn mysql://<ro用户>:<pwd>@localhost:3306/_erpnext \
     --odoo-dsn postgres://<ro用户>:<pwd>@localhost:5432/odoo_o2c \
     --manifest /srv/ontometa/benchmark/manifest.json

**SKIP 不等于 PASS**：缺驱动或缺 DSN 时该项记 SKIP 且整体返回非零。补齐再跑，
别拿带 SKIP 的结果当验收依据。
校验不过就改生成器、恢复 baseline、重跑，**绝不手工补数据**——手工补的行不进
truth.json，真值一旦和实际脱节，后面所有交叉校验全部失效。

全绿后打数据快照（mysqldump + pg_dump），并把镜像 digest 与 ERPNext/Odoo 版本号
补进 manifest.json 的 images 字段——否则半个月后重建环境说不清是不是同一套 schema。

报告：verify.py 的逐项结果、实际行数 vs truth.json 的 expected_erp/expected_odoo、
耗时、改了投递器哪些地方、快照文件与 manifest 内容。
```

---

## 提示词 3 · 平台栈（README 步骤 5–7）

```
你在 ontoMeta 验证环境的 docker 宿主机上，工作目录是仓库根。
业务数据已造好并通过一致性校验、已打快照。
本轮目标：起 DataHub、Airflow、Flink 会话集群、目标数仓 Postgres，并验证互相连得通。

权威步骤见 deploy/benchmark/README.md 步骤 5 到步骤 7。

先按步骤 5 停掉业务系统的应用容器（backend/frontend/websocket/queue-*/scheduler 与
odoo），只留两个数据库——腾出 12G 给本轮。数据库不能停：DataHub 采集和 Flink 读源
都直连数据库。

硬约束：
- DataHub 用官方 datahub docker quickstart，不要手写它的 compose（十来个服务，
  版本耦合紧）。起来后按 README 收敛 ES 与 GMS 的堆大小，停掉 datahub-actions。
- 采集 recipe 必须屏蔽金标准表：tabDocType / tabDocField / tabCustom Field /
  ir_model / ir_model_fields。它们是评分答案，泄漏进本体输入集全部分数作废。
  L1/L2 用不同 platform_instance，避免 URN 撞车。
- Flink 用 standalone 会话集群，不要装 YARN/Hadoop（多耗 8G 且对验证零增益）。
- Flink 版本锁 1.13.6，与 sql-runner.jar 编译期绑定，不许改。
- JDBC 驱动放 Flink 集群镜像（flink.Dockerfile），不要放 Airflow 镜像。
  放错边的症状是运行期 ClassNotFoundException，且报错指不到是哪边缺。
- Airflow 镜像里的 Java 必须是 11（Dockerfile 已从 temurin 多阶段拷入）。
  构建失败也不要改成 openjdk-17 —— Flink 1.13 在 17 上会因模块访问限制直接崩。
- 构建前记得 cp tools/flink-sql-runner/sql-runner.jar deploy/benchmark/

完成判据（逐条实际执行验证，不要凭日志推断）：
- curl DataHub GMS 的 /health 返回正常
- Airflow UI 可登录；用 basic auth 调 /api/v1/dags 返回 200 而不是 401
- 在 airflow-scheduler 容器里执行 flink list -t remote，能列出集群
- 目标数仓 Postgres 能用 dwh 账号建表（它要写，不是只读）
- 两个业务库都已被 DataHub 采集，且采集结果里没有任何金标准元数据表
- docker stats 总占用在 26G 上下

报告：各服务实际版本与端口、每条判据的实测结果、采集到的表数量、
与 README 的差异及你的回改、内存占用明细。
```

---

## 提示词 4 · ontoMeta 接线（**在 mac 上执行**）

```
你在 mac 上，工作目录是 ontoMeta 仓库根。这台机器只跑 ontoMeta 本体，
其余组件都在另一台 docker 宿主机上，两台通过 Tailscale 互通。
本轮目标：把 ontoMeta 接到那台机器的各组件上。

权威步骤见 deploy/benchmark/README.md 步骤 8。

必须处理的三件事：
1. 挂载 DAG 共享目录（NFS/SMB）。两侧挂载点不同没关系——DAG 里的 SQL 目录是
   Path(__file__).parent/"jobs" 在 Airflow worker 里运行期解析的，不写死绝对路径。
   但必须是同一份文件。
2. 改 backend/.env 的三个 Flink 变量。它们填的都是「Airflow 容器内」的值：
   - FLINK_BIN 当前是 /Users/me/local/flink/current/bin/flink，这是 mac 本地路径。
     不改的话会被原样拼进 DAG 的 bash 命令、在容器里执行，必然 No such file or
     directory，而且报错停在 BashOperator 上看不出根因。改成 flink。
   - FLINK_DEPLOY_TARGET 缺省 yarn-per-job，必须改成 remote，否则去找没部署的 YARN。
   - FLINK_SQL_RUNNER_JAR 填容器内路径 /opt/ontometa/sql-runner.jar。
   - DATAHUB_GMS_URL 改成 http://<宿主机tailscaleIP>:8080
3. 在设置页配依赖组件（真源是 dependency_components 表，不是 .env 也不是遗留的
   airflow_settings）：Airflow 的 endpoint 与 dags 目录、ERP 源库、Odoo 源库、目标数仓。
   目标数仓的 kind 必须是 postgres，否则 resolve_engine 会回退成 hive 方言，
   症状是建表成功但不搬数。

完成判据：
- 设置页里各组件拨测通过
- 从 mac 能连上宿主机的 MariaDB / Odoo PG / 目标 PG
- python -m scripts.extract_gold_ontology --check-leak 返回 0 且扫描基数 > 0
  （空本体上"未发现泄漏"是空跑通过，不算通过）
- 投递一个 DAG 后，宿主机 /srv/ontometa/dags/ontometa/<artifact_id>/ 出现文件
- Airflow 能解析出该 DAG
- 一个 transform 任务端到端跑出非零行到目标数仓
- 故意停掉源库再跑一次，回执必须是 state=failed 而不是假绿

报告：改了哪些配置项（口令不要回显）、每条判据的实测结果、未通过项及你的判断。
```
