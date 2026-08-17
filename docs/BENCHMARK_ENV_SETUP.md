# 业务系统安装与造数方案（docker 主机）

> 配套 `EFFECTIVENESS_VALIDATION_PLAN.md` / `BENCHMARK_DATA_PREP.md`。
> 前提变了：另有一台机器可装 docker（分配 48GB 内存 / 200GB 存储），于是改走**真实例**路线。
> 实测算下来只需 ~18GB / <30GB，配额可以再收（见 §1.3）。

---

## 0. 路线变更与取舍

| | 零安装路线（原） | docker 真实例（现） |
|---|---|---|
| schema | 自己按 DocType JSON 生成 DDL | **应用自己建**，一字不差 |
| 单据链自洽 | 靠生成器保证，易出错 | **应用业务逻辑保证**（GL 分录、库存台账、状态流转） |
| 金标准 | 从仓库 JSON 抽 | **直接读 `tabDocType`/`ir_model` 实表** |
| 造数速度 | 分钟级 | 小时级（可接受，见 §7） |

**作废两条原设计**：

1. 自己生成 DDL —— 应用装完就有全部表。
2. **对 630 张边缘表稀疏灌数** —— 原来担心"全空表会让分类器一律判 `data_table`"，那是自建表场景下的顾虑。真实 ERP 里没启用的模块（制造、项目、资产）表本来就是空的，**空表是真实现象，不是人为失真**。保持应用装完的自然状态即可。

**仍然权威、不受影响**：`BENCHMARK_DATA_PREP.md` §3.2 业务剧本、§3.3 双系统冲突配方、§3.4 脏案例配方、§3.5 分布、§3.6 真值落盘、§4 L1/L2 变体、§5 可重复性铁律。

---

## 1. 主机、网络与端口

### 1.1 先测算：这个负载有多大

拍配额之前先算数据量，否则会按"ERP 就该给很多内存"的直觉配，浪费一半。

按 §3.2 剧本的行数估：

| 表 | 行数 | 估算大小（含索引） |
|---|---|---|
| Sales Order + Item | 6000 + 15000 | ~50MB |
| Delivery Note + Item | 5200 + 13000 | ~40MB |
| Sales Invoice + Item | 5000 + 12500 | ~50MB |
| Payment Entry + Reference | 4300 + 5000 | ~15MB |
| **GL Entry** | ~48000 | ~40MB |
| **Stock Ledger Entry** | ~14500 | ~12MB |
| 主数据（客户/商品/价格） | ~2100 | ~10MB |
| 约 1000 张空表的表空间开销 | — | ~120MB |

**ERPNext 库合计 400–600MB。Odoo 库（只投 1800 张线上单）300–500MB。**

即便按 3 倍余量算也不到 2GB——**整个数据集能完整装进 1.5GB 内存**。

结论很直接：**这是写吞吐受限的负载，不是内存受限的负载。**给 MariaDB 8GB buffer pool，多出来的 7.5GB 一页都不会用到。想跑得快，要加的是并发 worker 数（§5.4），不是内存。

### 1.2 资源分配（48GB 上限，实配约 18GB）

| 容器 | `mem_limit` | 关键参数 |
|---|---|---|
| **mariadb** | 3G | `innodb_buffer_pool_size=1500M`（全库可驻留）、`skip-log-bin` |
| **backend**（gunicorn） | 5G | **8 workers** — 提交并发的真正瓶颈，见 §5.4 |
| queue-short | 1G | 2 进程 |
| queue-long | 1G | 2 进程 |
| redis-cache | 512M | `maxmemory 384mb` |
| redis-queue | 512M | |
| scheduler | 512M | |
| websocket / frontend | 384M | |
| ERPNext 小计 | **~12G** | |
| **postgres**（Odoo） | 2G | `shared_buffers=512MB`、`work_mem=32MB` |
| **odoo** | 3G | `--workers=4`、`--limit-memory-hard=2G` |
| Odoo 小计 | **~5G** | |
| 造数生成器 | 0（**跑在宿主机，不进 docker**） | 只发 HTTP/XML-RPC，无需容器化 |
| **合计** | **~17–18G** | |

**每个容器都要显式设 `mem_limit`**——不是为了省，是为了防某个容器（尤其 gunicorn 内存泄漏）把整台机器拖垮，那会毁掉跑了一夜的数据。

### 1.3 48GB 该怎么处理

取决于 docker 形态：

- **Docker Desktop（Mac/Windows）**：VM 内存是**预留**的，配 48GB 就真占住 48GB。既然只用得到 18GB，**直接把 VM 调到 24GB**，剩下 24GB 还给宿主机——这才是真正的节省。24GB 已留出 6GB 余量应付 dump 峰值和临时并发。
- **Linux 原生 docker**：cgroups 不预留，未用的内存自动当页缓存，48GB 留着无害。但 §1.2 的 per-container `mem_limit` 仍要设。

**不要把省下的内存加回给数据库。** 前面算过，数据集装得下 1.5GB，加到 8GB 不会快哪怕 1%。

### 1.4 存储预算（200GB 远超所需）

| 项 | 占用 |
|---|---|
| 镜像（erpnext 2G + odoo 1.5G + mariadb/postgres/redis ~1G） | ~4.5G |
| 数据卷（两系统 × L1/L2 四份库） | ~2.5G |
| 快照（baseline + 数据快照，gzip 后各 ~150MB，留 10 份） | ~1.5G |
| docker 构建缓存 / overlay 层 | 预算 20G |
| **合计** | **< 30G** |

200GB 绰绰有余，无需调整（Docker Desktop 的磁盘映像是稀疏的，用多少占多少）。三条防膨胀措施：

- 容器日志限长：`--log-opt max-size=50m --log-opt max-file=3`，否则跑一夜的日志能涨到几 GB
- MariaDB 关 binlog（`skip-log-bin`）——单机无复制需求，能省几 GB 写入
- 每轮造数后 `docker system prune -f` 清构建缓存

### 1.2 网络

ontoMeta 与 DataHub 在 mac 上，业务系统在另一台——**采集要能连到那台机器的数据库端口**。

mac 上已装 Tailscale，两台机器进同一个 tailnet 最省事：直接用 `100.x.x.x` 连库，不必在路由器上开端口，也避免把数据库暴露到局域网。

| 服务 | 端口 | 是否需要对 mac 开放 |
|---|---|---|
| ERPNext 前端 | 8080 | 否（造数期间人工看一眼用） |
| **MariaDB** | 3306 | **是**——DataHub 采集 |
| Odoo 前端 | 8069 | 否 |
| **Odoo Postgres** | 5432 | **是**——DataHub 采集 |

数据库账号给**只读**用户供采集用，不要用 root。

---

## 2. ERPNext 安装

用官方 `frappe/frappe_docker` 的 `pwd.yml`（一条命令起完整生产栈），锁 **v15**：

```bash
git clone https://github.com/frappe/frappe_docker
cd frappe_docker
docker compose -f pwd.yml up -d
```

起完包含 mariadb、redis-cache、redis-queue、backend、frontend、websocket、queue-short/long、scheduler，并自动建站。装完确认 `create-site` 容器已正常退出，站点可访问。

### 2.1 必须做的三处调整

1. **暴露 MariaDB 端口**：`pwd.yml` 默认不映射 3306，加 `ports: ["3306:3306"]`，并建只读账号：
   ```sql
   CREATE USER 'datahub'@'%' IDENTIFIED BY '...';
   GRANT SELECT ON `_*`.* TO 'datahub'@'%';
   ```
2. **调 gunicorn worker，不是队列 worker**：单据提交走 REST 时是**同步**执行在 web worker 里的，后台队列只处理重算、邮件这类异步活。所以要加的是 `backend` 的 gunicorn workers（配 8），`queue-short`/`queue-long` 各 2 个进程就够（前提是按 §5.4 时间正序造数）。
3. **关掉写噪声**：Version 记录（每次修改写一行 `tabVersion`）、Email Queue、Notification。它们对验证无用，却显著拖慢提交。`tabVersion` 表本身要保留——它是框架噪声分母，只是不需要有几十万行。

### 2.2 初始化：走 UI 一次，然后做 baseline 快照

Setup Wizard 也有 API（`frappe.desk.page.setup_wizard.setup_wizard.setup_complete`），但**它的 payload 结构逐版本变**，跟它搏斗不划算。老实在浏览器里点一遍：

- 公司、币种 CNY、时区 Asia/Shanghai
- 财年 **1/1–12/31**（别用 4/1，跨财年会给月度口径题额外添乱，那不是我们要验的东西）
- 科目表、税模板、默认仓库、UOM

点完立刻 `mysqldump` 成 **`baseline.sql`**。之后生成器每次重跑都从 baseline 恢复，**不重走向导**。这一步把"初始化"从流程里彻底摘出去了。

启用模块：Selling / Buying / Stock / Accounts 必开；Manufacturing / Projects / Assets 装着但不灌数据——它们的空表正是要的噪声。

---

## 3. Odoo 安装

`odoo:17` + `postgres:16`，两个容器：

```bash
docker run -d --name odoo-db -e POSTGRES_PASSWORD=... -p 5432:5432 postgres:16
docker run -d --name odoo -p 8069:8069 --link odoo-db:db odoo:17
docker exec odoo odoo -d odoo_o2c -i sale,crm,stock,account,sale_management,l10n_generic_coa \
  --without-demo=all --stop-after-init
```

两个要点：

- **`--without-demo=all`**。Odoo 的 demo 数据会塞进一批 `res.partner` 和 `product`，与我们造的混在一起就污染了真值统计——跨系统题问"两系统重复客户有多少"时，多出来的 demo 客户会让答案对不上。
- 不要 demo 就没有科目表，所以显式装 `l10n_generic_coa`（通用科目表）。要更贴中国场景可换 `l10n_cn`。

同样在初始化后 `pg_dump` 成 baseline。

---

## 4. 造数架构：中立台账 + 双向投递

**这是整套造数最关键的设计。**

```
                    ┌──────────────────────────┐
                    │   business_ledger        │  系统中立的业务事实台账
                    │   客户 / 商品 / 订单 /   │  ← 真值就是它
                    │   发货 / 发票 / 回款     │     直接落 truth.json
                    └────────────┬─────────────┘
                                 │
                  ┌──────────────┴──────────────┐
                  ▼                             ▼
        ┌───────────────────┐         ┌───────────────────┐
        │  ERPNext 投递器   │         │   Odoo 投递器     │
        │  REST API         │         │   XML-RPC         │
        │  投全部 6000 单   │         │   只投 1800 线上  │
        └───────────────────┘         └───────────────────┘
                  └──── 投递时按配方注入差异 ────┘
              名称拼写变体 / 60 张不下传 / 状态映射 / SPU-SKU 粒度差
```

**先造台账，再分别投递**，不要反过来。

反过来做（先在 ERPNext 造，再同步到 Odoo）会让真值依赖同步逻辑本身的正确性——**等于用被验证的对象去生成金标准**，跨系统档的分数就没有意义了。

台账是纯 Python 数据结构，先整体生成、落 `truth.json`，再投递。好处：

- 真值天然存在，不必事后从两个系统反推
- 冲突是**故意注入**的，数量精确可控（"60 张未下传"是配方参数，不是跑出来的现象）
- 任一系统投递失败可单独重跑，台账不动

---

## 5. ERPNext 投递器

认证用 API Key/Secret（Administrator 账号生成），Header `Authorization: token <key>:<secret>`。

### 5.1 主数据

`POST /api/resource/<DocType>`：Customer、Item、Warehouse、Sales Person、Item Price。

### 5.2 单据链——必须走标准转换函数

不要手工拼 Delivery Note。用 ERPNext 自己的映射方法：

```
POST /api/method/erpnext.selling.doctype.sales_order.sales_order.make_delivery_note
     { "source_name": "SAL-ORD-2026-00001" }
  → 返回已映射好的 DN doc（改数量后再提交，即为部分发货）

POST /api/method/erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice
POST /api/method/erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry
     { "dt": "Sales Invoice", "dn": "ACC-SINV-2026-00001" }
```

**这点很重要**：标准转换函数会自动带出 `against_sales_order`、`so_detail` 这些引用字段。履约周期、按时交付率、订单到发票的血缘，全靠它们。手工拼的单据链会缺引用，指标算不出来，而且是静默算错。

提交用 `PUT /api/resource/<DocType>/<name>` 带 `{"docstatus": 1}`。

### 5.3 脏案例怎么落地

| 案例 | 做法 |
|---|---|
| 部分发货 | `make_delivery_note` 后改 qty，分 2–3 次 |
| 超发 / 短发 | 改 qty 越过 SO 数量（需放开 `Over Delivery Allowance`） |
| 退货 | `is_return=1` 的 Delivery Note + Credit Note |
| 部分回款 / 合并回款 | Payment Entry 的 `references` 挂多张发票、金额少于应收 |
| 坏账 | **不建** Payment Entry，`posting_date` 拉到 90 天前 |
| 改价 | cancel 后 amend，产生 `amended_from` 链 |
| 跨期 | 发票 `posting_date` 与订单 `transaction_date` 分属不同月 |
| 名称脏数据 | Customer 名字带前后空格、全半角混用 |

### 5.4 性能：瓶颈在哪，以及一个能毁掉整夜的坑

单据提交要写 GL Entry + Stock Ledger Entry，**每张 0.5–2 秒**。~20500 次提交在 8 并发下约 **3–5 小时**，跑一夜就完。

**并发上限由 gunicorn worker 数决定**（提交是同步执行在 web worker 里的），不是由内存决定。生成器开到 8 个并发进程即可，开到 32 也没用——请求只会排在 8 个 worker 后面。

**必须按时间正序造数（2 月 → 7 月）。** ERPNext 对**回溯**的库存单据会排 `Repost Item Valuation` 后台任务，重算该物料该仓库此后的全部库存流水。造 6 个月历史数据时若乱序插入，会触发成千上万次重算，队列积压到跑一整天都做不完——而且是在你以为已经跑完之后才慢慢显现。按日期正序提交就完全不会产生回溯，这个坑直接绕过去。

**Perpetual Inventory 保持开启**——库存周转天数指标需要 Stock Ledger Entry。为提速关掉它，指标就没了。

---

## 6. Odoo 投递器

XML-RPC：`/xmlrpc/2/common` 认证，`/xmlrpc/2/object` 的 `execute_kw` 调用。

- **主数据**：`res.partner`；商品建 `product.template`(SPU) 再建 `product.product`(SKU 变体)——**SPU/SKU 粒度差就在这里注入**，200 SPU ↔ 800 SKU。
- **单据流**：`sale.order` create → `action_confirm` → `stock.picking.button_validate` → `_create_invoices()` → `account.move.action_post` → `account.payment` create + `action_post`。
- **只投 1800 张线上订单**，不是全部 6000。Odoo 在剧本里的角色是"前端电商/CRM 系统"，它本来就只该看见线上那部分——这个不对称本身就是跨系统题的题面。

Odoo 的 create/write 比 Frappe 快不少，~6000 次调用约 1 小时。

---

## 7. 执行顺序与快照

```
起环境 → UI 初始化 → baseline 快照 ──┐
                                      ├→ 生成台账(truth.json) → 双向投递 → 校验 → 数据快照
        （重跑时从 baseline 恢复）────┘
```

| 阶段 | 耗时 |
|---|---|
| 起两个栈 | 1–2 小时 |
| UI 初始化 + baseline | 1 小时 |
| 生成台账 | 分钟级 |
| ERPNext 投递 | 3–5 小时 |
| Odoo 投递 | ~1 小时 |
| 校验 + 快照 | 1 小时 |

**快照是重放的单位，不是生成器。** 造完 `mysqldump` + `pg_dump` 存进 `benchmark/snapshots/`，之后恢复是分钟级。验证期间数据一变前面分数全废——快照 + manifest 校验和是唯一能守住这条的办法。

`manifest.json` 记：各表行数、内容校验和、生成 seed、**docker 镜像 digest**、ERPNext/Odoo 版本号、生成时间。

---

## 8. 一致性校验（跑完必做，不通过不进入 W2）

| 检查 | 判据 |
|---|---|
| 台账 vs ERPNext | 订单数 / 行数 / 金额合计逐项相等 |
| 台账 vs Odoo | 同上（对 1800 张线上单） |
| 冲突注入生效 | 那 60 张确实不在 ERP；120 个重复客户两边都在 |
| GL 自洽 | `tabGL Entry` 借贷合计相等 |
| 库存自洽 | Stock Ledger Entry 累计 = Bin 结存 |
| 金标准未泄漏 | `extract_gold_ontology --check-leak` 返回 0（且扫描基数 > 0） |

任一项不过，**改生成器重跑，不要手工补数据**——手工补的那几行不会进 `truth.json`，真值就此和实际数据脱节，后面所有交叉校验全部失效。

---

## 9. 工作量

| 任务 | 人日 | 风险 |
|---|---|---|
| 两个栈起环境 + 网络打通 | 0.5 | 低 |
| UI 初始化 + baseline 快照 | 0.5 | 低 |
| **台账生成器 + 真值** | **1.5** | 中 |
| ERPNext 投递器 | **1.5** | 中——标准转换函数的参数细节最费时间 |
| Odoo 投递器 | 1 | 中 |
| 全量跑 + 校验 + 快照 | 0.5（含跑一夜） | 中 |
| 合计 | **5.5** | 比零安装路线多 1.5 人日 |

多花的 1.5 人日买到的是：真 schema、应用保证的单据自洽、直读实表的金标准。**值得**，但 W1 因此更紧——台账生成器和 ERPNext 投递器要并行开工。

---

## 10. 风险

- **提交太慢**：若 8 并发下仍超过 8 小时，把订单量从 6000 降到 3000。指标仍成立，只是分布变稀、长尾案例变少；不要为提速关掉 Perpetual Inventory。
- **两系统口径不一致**：时区统一 `Asia/Shanghai`、财年统一 1/1–12/31。不统一的话月度指标对不上，会被误判成产品的问题。
- **`--without-demo=all` 后 Odoo 缺基础配置**：科目表、税、UOM 都要显式装或建。
- **端口暴露**：走 Tailscale，别在公网或办公网直接开 3306/5432；采集账号只读。
- **ERPNext 版本漂移**：`pwd.yml` 默认拉 latest tag，锁定具体版本并把镜像 digest 记进 manifest，否则半个月后重建环境拿到的不是同一套 schema。
