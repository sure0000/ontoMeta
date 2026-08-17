# 验证环境部署运行手册（docker 宿主机执行）

> 这份文档是**给那台 docker 机器逐条执行的**。mac 上只跑 ontoMeta 本体（后端 + 前端），
> 业务系统、DataHub、Airflow、Flink、目标数仓全部在这台机器上。
>
> 配套：`docs/BENCHMARK_ENV_SETUP.md`（造数配方与业务剧本）、
> `docs/BENCHMARK_DATA_PREP.md`（冲突/脏案例配方）、
> `docs/EFFECTIVENESS_VALIDATION_PLAN.md`（这一切是为了验什么）。
>
> ⚠ 本目录的 compose 与 Dockerfile **尚未在真实 docker 上跑过**（编写环境无 docker）。
> 按本手册逐步执行，遇到不符请以实际为准并回改本目录。

---

## 1. 拓扑

```
        mac（只跑 ontoMeta）                    docker 宿主机（其余全部）
   ┌───────────────────────────┐        ┌──────────────────────────────────────┐
   │ ontoMeta backend :8000    │        │  ERPNext  web:8090   MariaDB:3306    │
   │ ontoMeta frontend :5180   │◄──────►│  Odoo     web:8069   Postgres:5432   │
   │                           │tailnet │  DataHub  gms:8080   web:9002        │
   │ dags 目录 ← NFS/SMB 挂载  │◄──────►│  Airflow  web:8081                   │
   └───────────────────────────┘        │  Flink    rest:8082                  │
                                        │  目标数仓 Postgres:5433              │
                                        └──────────────────────────────────────┘
```

两台机器走 **Tailscale** 互通，不要在办公网直接暴露数据库端口。

---

## 2. 两个阶段，不同时开

内存按阶段错峰，48GB 绰绰有余；同时全开则要 43GB，没有余量。

| | 阶段 A：造数 | 阶段 B：采集与验证 |
|---|---|---|
| ERPNext | **全栈**（12G） | 只留 `db`（3G） |
| Odoo | **全栈**（5G） | 只留 `odoo-db`（2G） |
| DataHub | 停 | 12G |
| Airflow | 停 | 3.5G |
| Flink | 停 | 4G |
| 目标数仓 | 停 | 2G |
| **合计** | **17G** | **26.5G** |

造完数、做完快照，业务系统的**应用容器就没用了**——DataHub 采集和 Flink 读源都直接连数据库。停掉它们省 12G，这是错峰的关键。

---

## 3. 步骤 0：宿主机准备

```bash
# 1) 配镜像源 —— 这是第一道坎，务必先做
#    此前实测 Docker Hub 直连拉 4MB 的 alpine 用了 6 分钟，
#    erpnext(~2G)/airflow(~1.5G)/flink(~700M) 按此速率根本拉不下来。
sudo vi /etc/docker/daemon.json      # 填 registry-mirrors
sudo systemctl restart docker

# 2) 建共享网络（三个 compose 栈靠它互通）
docker network create ontometa-bench

# 3) 建 DAG 共享目录并导出给 mac
sudo mkdir -p /srv/ontometa/dags && sudo chmod 777 /srv/ontometa/dags
#    NFS 导出（或用 SMB，macOS 两者都好挂）
echo "/srv/ontometa/dags *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports
sudo exportfs -ra

# 4) 配置
cd deploy/benchmark
cp .env.example .env
tailscale ip -4                      # 填进 .env 的 BENCH_HOST_IP
vi .env                              # 改掉所有 change-me 口令

# 5) 拉镜像测速（几分钟内拉不动就是镜像源没生效，别往下走）
docker pull ${IMG_POSTGRES:-postgres:16}
```

---

## 4. 步骤 1：ERPNext

```bash
git clone --depth 1 https://github.com/frappe/frappe_docker
cd frappe_docker
docker compose --env-file ../deploy/benchmark/.env \
  -f pwd.yml -f ../deploy/benchmark/erpnext.override.yml up -d

# 等建站完成（这个容器跑完会正常退出）
docker compose -f pwd.yml logs -f create-site
```

**若报 `service X not found`**：`pwd.yml` 的服务名随上游版本变动。用
`docker compose -f pwd.yml config --services` 对一遍，改 `erpnext.override.yml`。

### 4.1 走一遍 Setup Wizard（浏览器）

`http://<宿主机>:8090`，用 Administrator 登录。**不要跟 Setup Wizard 的 API 搏斗**——它的
payload 结构逐版本变，点一遍比调通它快得多。

- 公司名、币种 **CNY**、时区 **Asia/Shanghai**
- 财年 **1/1–12/31**（别选 4/1，跨财年会给月度口径题额外添乱，那不是要验的东西）
- 科目表、税模板、默认仓库、UOM

### 4.2 建只读账号、关写噪声

```bash
docker compose -f pwd.yml exec db mysql -uroot -p"$ERP_DB_ROOT_PASSWORD" -e "
  CREATE USER '${RO_USER}'@'%' IDENTIFIED BY '${RO_PASSWORD}';
  GRANT SELECT ON \`_%\`.* TO '${RO_USER}'@'%'; FLUSH PRIVILEGES;"
```

在 ERPNext 界面里关掉：Version 记录（每次修改写一行 `tabVersion`）、Email Queue、Notification。
`tabVersion` 表本身**要保留**——它是框架噪声表的分母，只是不需要有几十万行。

### 4.3 baseline 快照

```bash
docker compose -f pwd.yml exec db mysqldump -uroot -p"$ERP_DB_ROOT_PASSWORD" \
  --single-transaction --databases _erpnext | gzip > /srv/ontometa/snapshots/erp_baseline.sql.gz
```

之后每次重跑造数都从 baseline 恢复，**不重走向导**——初始化就此从流程里摘掉了。

---

## 5. 步骤 2：Odoo

```bash
cd deploy/benchmark
docker compose --env-file .env -f compose.biz.yml up -d

docker compose -f compose.biz.yml exec odoo \
  odoo -d odoo_o2c -i sale,crm,stock,account,sale_management,l10n_generic_coa \
       --without-demo=all --stop-after-init
```

**`--without-demo=all` 不能省**：Odoo 的 demo 数据会塞进一批 `res.partner` 和 `product`，
和造的数据混在一起就污染真值——跨系统题问「两系统重复客户有多少」时答案会对不上。
代价是没有科目表，所以显式装 `l10n_generic_coa`。

只读账号 + baseline：

```bash
docker compose -f compose.biz.yml exec odoo-db psql -U odoo -d odoo_o2c -c "
  CREATE USER ${RO_USER} WITH PASSWORD '${RO_PASSWORD}';
  GRANT CONNECT ON DATABASE odoo_o2c TO ${RO_USER};
  GRANT USAGE ON SCHEMA public TO ${RO_USER};
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${RO_USER};
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${RO_USER};"

docker compose -f compose.biz.yml exec odoo-db \
  pg_dump -U odoo odoo_o2c | gzip > /srv/ontometa/snapshots/odoo_baseline.sql.gz
```

---

## 6. 步骤 3：造数

**生成器跑在宿主机上，不进 docker**——它只发 HTTP/XML-RPC，容器化没有收益。

分三步走，别一上来就全量——投递器接不上时，全量跑到一半才发现最贵。

```bash
python3 -m venv ~/bench-venv && source ~/bench-venv/bin/activate
pip install requests
cd benchmark

# ① 只生成台账与真值，不连任何系统。先确认配方与八个指标数值合理
python generate_o2c.py --only ledger --out /srv/ontometa/benchmark/truth.json

# ② 小规模冒烟，确认两个投递器都接得上（几分钟；脏案例条数会同比例缩）
python generate_o2c.py --orders 40 \
  --erp http://localhost:8090 --erp-key <key>:<secret> \
  --odoo http://localhost:8069 --odoo-db odoo_o2c --odoo-password <pwd> \
  --out /tmp/truth-smoke.json

# ③ 全量，跑一夜
nohup python generate_o2c.py \
  --erp http://localhost:8090 --erp-key <key>:<secret> \
  --odoo http://localhost:8069 --odoo-db odoo_o2c --odoo-password <pwd> \
  --seed 42 --concurrency 8 \
  --out /srv/ontometa/benchmark/truth.json \
  > /srv/ontometa/benchmark/gen.log 2>&1 &
```

冒烟之后、全量之前，**必须把两个库恢复到 baseline 快照**——冒烟那 40 张单会混进最终数据，
让实际行数与 truth.json 对不上。

**不支持断点续跑**：中途失败就恢复 baseline 重跑。做成可续跑要在两侧维护幂等键，
成本远高于重跑一夜，而且半截数据混着重试痕迹比重来更难排查。

三条约束照 `docs/BENCHMARK_ENV_SETUP.md` 执行，任一条破了整夜白跑：

1. **先造中立台账再双向投递**，不要先在 ERPNext 造再同步到 Odoo——那等于用被验证的对象生成金标准。
2. **按时间正序（2 月 → 7 月）**。ERPNext 对回溯的库存单据会排 `Repost Item Valuation`，
   乱序插入会触发成千上万次重算，队列积压到跑一整天也做不完，而且是在你以为跑完之后才慢慢显现。
3. **并发 8 即可**。提交是同步执行在 gunicorn worker 里的，worker 配了 8，开到 32 只会排队。

预计 ERPNext ~20500 次提交、3–5 小时；Odoo ~6000 次、约 1 小时。跑一夜。

---

## 7. 步骤 4：一致性校验与数据快照

校验有脚本，直连两个库跑 SQL（聚合校验走 REST 又慢又容易被分页坑）：

```bash
pip install pymysql psycopg2-binary
cd benchmark
python3 verify.py --truth /srv/ontometa/benchmark/truth.json \
  --erp-dsn  mysql://datahub_ro:<pwd>@localhost:3306/_erpnext \
  --odoo-dsn postgres://datahub_ro:<pwd>@localhost:5432/odoo_o2c \
  --manifest /srv/ontometa/benchmark/manifest.json
```

它逐项比对 `truth.json` 里的 `expected_erp` / `expected_odoo`：单据行数、提交态金额合计、
尾差订单数、GL 借贷平衡、库存台账与结存一致、未下传的 60 张确实不在 ERP、难匹配客户
两边都在且名称确实对不上。

**跳过不等于通过**：缺驱动或缺 DSN 时该项记 `SKIP` 且整体返回非零。补齐再跑，别拿带
SKIP 的结果当验收依据——那正是这套方案从头到尾在防的假绿。

**不通过就改生成器、恢复 baseline、重跑，不要手工补数据**——手工补的行不会进
`truth.json`，真值一旦和实际数据脱节，后面所有交叉校验全部失效。

全绿之后才打数据快照（这才是重放的单位，恢复是分钟级，不是重跑生成器的小时级）：

```bash
# 同前面的 mysqldump / pg_dump，文件名换成 erp_data_v1 / odoo_data_v1
# manifest.json 由 verify.py 写出，但 images 字段是空的——
# 打快照时把镜像 digest 与 ERPNext/Odoo 版本号补进去，否则半个月后重建环境
# 说不清拿到的是不是同一套 schema
```

---

## 8. 步骤 5：错峰——停掉业务应用容器

```bash
cd frappe_docker
docker compose -f pwd.yml stop backend frontend websocket queue-short queue-long scheduler
cd ../deploy/benchmark
docker compose -f compose.biz.yml stop odoo
```

省下 12G，留给 DataHub 和执行栈。数据库仍在跑，采集与 Flink 读源都够。

---

## 9. 步骤 6：DataHub

用官方 CLI，**不要手写它的 compose**（十来个服务，版本耦合紧）：

```bash
pip install acryl-datahub
datahub docker quickstart --version v1.6.0
```

起来后收敛内存（默认堆配得很大）：给 Elasticsearch `ES_JAVA_OPTS=-Xms1g -Xmx1g`、
GMS `JAVA_OPTS=-Xms1g -Xmx2g`。只做采集的话 `datahub-actions` 可以直接停掉。

### 9.1 采集 recipe 必须屏蔽金标准表

`tabDocType` / `tabDocField` / `tabCustom Field` / `ir_model` / `ir_model_fields` 是**评分答案**，
泄漏进本体输入集，全部分数作废。

```yaml
source:
  type: mysql
  config:
    host_port: "<宿主机>:3306"
    username: "${RO_USER}"
    password: "${RO_PASSWORD}"
    platform_instance: erp_l2          # L1/L2 用不同 instance，避免 URN 撞车
    table_pattern:
      deny: ["_erpnext\\.tabDocType", "_erpnext\\.tabDocField",
             "_erpnext\\.tabCustom Field"]
sink: {type: datahub-rest, config: {server: "http://localhost:8080"}}
```

采完立刻在 mac 上复核（它会报扫描基数，空本体不会假绿）：

```bash
cd backend && source .venv/bin/activate
python -m scripts.extract_gold_ontology --check-leak
```

---

## 10. 步骤 7：Airflow + Flink + 目标数仓

```bash
cd deploy/benchmark
cp ../../tools/flink-sql-runner/sql-runner.jar .     # airflow.Dockerfile 要 COPY 它
docker compose --env-file .env -f compose.exec.yml build
docker compose --env-file .env -f compose.exec.yml up -d
```

验证 Flink 客户端能连上集群：

```bash
docker compose -f compose.exec.yml exec airflow-scheduler flink list -t remote
```

**用 standalone 会话集群，不搭 YARN。** `airflow_dag_builder` 的 `deploy_target` 是 env 可配的，
设成 `remote` 即可。在 docker 里搭 YARN 要多耗 8G 且对验证毫无增益——要验的是 SQL 生成与
落数正确性，不是资源调度。

> 已知无害现象：`FLINK_YARN_QUEUE` 为空时代码会兜底成 `default`，命令行里始终带一个
> `-Dyarn.application.queue=default`。Flink 会把未知 `-D` 键当动态配置收下，不影响 `-t remote`。

---

## 11. 步骤 8：mac 侧 ontoMeta 配置

### 11.1 挂载 DAG 共享目录

```bash
sudo mkdir -p /Volumes/ontometa-dags
sudo mount -t nfs -o resvport <宿主机tailscaleIP>:/srv/ontometa/dags /Volumes/ontometa-dags
```

两侧挂载点不同**没关系**：DAG 里的 SQL 目录是 `Path(__file__).parent / "jobs"` 在
Airflow worker 里运行期解析的，不写死绝对路径。但必须是同一份文件。

### 11.2 `backend/.env`

```
DATAHUB_GMS_URL=http://<宿主机tailscaleIP>:8080
FLINK_DEPLOY_TARGET=remote
FLINK_BIN=flink
FLINK_SQL_RUNNER_JAR=/opt/ontometa/sql-runner.jar
```

**这三个 Flink 变量填的都是「Airflow 容器内」的值**，不是 mac 上的。它们只是被拼进
DAG 的 bash 命令字符串，真正执行发生在容器里。

⚠ **`FLINK_BIN` 必须改。** 当前 `backend/.env` 里是
`/Users/me/local/flink/current/bin/flink`（mac 本地 Flink 的路径，已实测生效）。
不改的话生成的命令会去容器里找这个 mac 路径，必然 `No such file or directory`——
而且报错停在 BashOperator 上，看不出根因是配置指错了机器。改成 `flink` 即可
（`airflow.Dockerfile` 已把 `$FLINK_HOME/bin` 放进 PATH）。

同理 `FLINK_DEPLOY_TARGET` 缺省是 `yarn-per-job`，不改成 `remote` 会去找根本没部署的 YARN。

### 11.3 设置页里配（不是 .env）

依赖组件的真源是 `dependency_components` 表，不是遗留的 `airflow_settings`：

- **Airflow**：endpoint `http://<宿主机tailscaleIP>:8081`，admin / 你设的口令，
  **dags 目录填 `/Volumes/ontometa-dags`**（mac 侧挂载点）
- **数据源**：ERP 源库（MariaDB `<宿主机>:3306`，只读账号）、Odoo 源库（`:5432`）、
  目标数仓（Postgres `<宿主机>:5433`，`dwh` 账号——它要建表，不能只读）

---

## 12. 验收清单

- [ ] mac 上 `curl http://<宿主机>:8080/health` 通（DataHub）
- [ ] mac 上 Airflow UI `:8081` 可登录，`/api/v1/dags` 用 basic auth 返回 200（不是 401）
- [ ] `flink list -t remote` 在 Airflow 容器里能列出集群
- [ ] `extract_gold_ontology --check-leak` 返回 0 **且扫描基数 > 0**
- [ ] ontoMeta 投递一个 DAG 后，宿主机 `/srv/ontometa/dags/ontometa/<artifact_id>/` 出现文件
- [ ] Airflow 解析出该 DAG（`dag_exists` 轮询能命中）
- [ ] 一个 transform 任务端到端跑出非零行到目标数仓
- [ ] 故意停掉源库再跑一次，回执必须是 `state=failed` 而不是假绿

---

## 13. 已知会绊人的地方

| 症状 | 真因 |
|---|---|
| 镜像拉不动 | 镜像源没配。先解决它，别试图硬拉 |
| Airflow `/api/v1/*` 全 401 | 没开 `basic_auth` 后端（本栈已配，改动时别删） |
| `flink run` 报 Java 模块访问错误 | 客户端用了 Java 17。Flink 1.13 只支持 8/11，本镜像已从 temurin 拷 JRE 11 |
| 运行期 `ClassNotFoundException: com.mysql.cj.jdbc.Driver` | 驱动放错边——它要在 **Flink 集群** 的 lib，不是 Airflow 客户端 |
| 造数跑了一天还没完 | 乱序造数触发了 `Repost Item Valuation` 风暴。按时间正序重来 |
| 采集到 `tabDocField` | 金标准泄漏，分数作废。改 recipe 的 deny 后重采 |
| 目标数仓建表成功但不搬数 | 目标数据源 kind 要是 `postgres`，否则 `resolve_engine` 回退成 hive 方言 |
