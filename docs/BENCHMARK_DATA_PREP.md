# 验证语料准备方案（W1）

> 配套 `EFFECTIVENESS_VALIDATION_PLAN.md`。本文只解决一件事：把两套真实开源 ERP 的
> **真 schema** 和一份**真值已知**的 O2C 业务数据，可重放地造出来。

> **⚠ 路线已变更（2026-08-13）**：另有一台 64GB 机器可装 docker，改走**真实例**路线，
> 见 [BENCHMARK_ENV_SETUP.md](./BENCHMARK_ENV_SETUP.md)。
>
> - **本文 §0–§2 降级为备用**：零安装路线仅在 docker 主机不可用时启用。
> - **本文 §3.2–§3.6 仍是权威**：业务剧本、双系统冲突配方、脏案例配方、分布、真值落盘。
> - **§4 L1/L2 变体、§5 可重复性铁律** 不受影响。
> - **§3.1 的"分层稀疏灌数"作废**——真实 ERP 里未启用模块的表本来就是空的，
>   空表是真实现象而非人为失真（理由见新文 §0）。

---

## 0. 本机实况决定的路线（备用路线）

| 项 | 实况 | 后果 |
|---|---|---|
| docker | 无 | compose 起 ERPNext/Odoo 不通 |
| MySQL | 9.6.0（非 MariaDB） | Frappe bench 官方要求 MariaDB 10.6+，装不了 |
| redis | 无 | bench 硬依赖，同样卡住 |
| Postgres | 17.9 运行中 | Odoo 侧可用 |
| Node / Python | 22.22 / 3.12.2 | 够用 |
| 已注册数据源 | `macmini-mysql`、`pg` | 双系统落点现成 |

**路线：零应用安装。** 不装 ERPNext、不长期跑 Odoo，只取两者的**模型定义**，自己建表、自己灌数。

这不是退而求其次，有三个它独有的好处：

1. **真值已知**——脏案例是按配方注入的，跨系统题的答案在造数时就确定，不依赖手写 SQL 是否正确（见 §3.4）。
2. **可重放**——固定种子，一条命令重建同一份数据。手点应用做不到，而验证期间数据一变、前面分数全废。
3. **比例可控**——坏账、部分发货、退货各占多少由配方定；靠应用自然产生这些，得点上几百次。

**要如实声明的代价**：业务数据是合成的。可接受，因为本体生成主要吃 schema + 样例值 + 行数，不吃业务真实性；问数正确性看的是能否正确 JOIN 与聚合。**可信度来自 schema 是真的**——完整 ERPNext DocType 集、真实 Odoo 模型，我们没碰过它们的设计。这句话要写进报告。

现有 `backend/scripts/seed_local_source_db.py` 造的玩具源库（10 表 22 行，只有形状没有业务）被本方案取代。

---

## 1. 四步流水线

```
S1 取模型定义与金标准  →  S2 建库建表  →  S3 造 O2C 数据 + 真值  →  S4 两档变体 + 采集
```

产物落 `benchmark/`，全部脚本进 `benchmark/prepare/`，由 `run_all.sh` 一键重放。

---

## 2. S1：模型定义与金标准从哪来

### 2.1 Frappe / ERPNext —— 零安装，直接读仓库

Frappe 把每个 DocType 存成仓库里的 JSON，与 `tabDocType`/`tabDocField` **同构**：

```
erpnext/selling/doctype/sales_order/sales_order.json
  { "name": "Sales Order", "module": "Selling", "istable": 0, "is_submittable": 1,
    "fields": [ { "fieldname": "customer", "fieldtype": "Link", "options": "Customer",
                  "label": "Customer", "reqd": 1 }, ... ] }
```

`git clone --depth 1 erpnext + frappe` 就能拿到全部 DocType 定义，**不需要跑起来**。

→ 给 [extract_gold_ontology.py](../backend/scripts/extract_gold_ontology.py) 加一路 `--system frappe-repo --repo-path <dir>`，复用已有的 `build_frappe_gold()`：把 JSON 摊平成 doctype 行与 docfield 行即可，转换逻辑一行不改。

### 2.2 Odoo —— 一次性源码初始化，之后只用 dump

Odoo 的模型定义在 Python 里（`fields.Many2one('res.partner')`），静态解析不可靠（继承、`_inherits`、动态字段都会漏）。老实装一次：

```bash
git clone --depth 1 -b 17.0 https://github.com/odoo/odoo
pip install -r requirements.txt          # 去掉 python-ldap，mac 上常编不过且用不到
odoo-bin -d odoo_gold --init=sale,crm,stock,account --stop-after-init
pg_dump -s odoo_gold > benchmark/prepare/odoo_schema.sql   # 之后只用这个
```

初始化完就得到：真 FK 约束、完整 `ir_model`/`ir_model_fields`、真实字段描述。**dump 出来后 Odoo 再也不用跑**，团队其他人不必重装。

比 Frappe 好装（只要 Postgres + pip，不需要 MariaDB/redis/node），但仍是本步骤唯一的中风险项。
**降级方案**：按 Odoo 官方模型文档，为 O2C 相关的 ~25 个模型手写一份模型定义 YAML，喂给同一个 `build_odoo_gold()`。损失是覆盖面（拿不到全部 ~900 模型当噪声分母），主命题 S3 不受影响。

---

## 3. S2 + S3：建库与造数

### 3.1 建表范围：全量建，分层灌

| 层 | 表数 | 灌数 |
|---|---|---|
| O2C 核心 | ~40 | 6 个月完整单据 |
| 周边（主数据、组织、科目） | ~60 | 数十~数百行 |
| 其余 DocType | ~630 | 0–20 行稀疏数据 |

**只建 O2C 那 40 张是作弊。** 本体生成的难度恰恰在于"从 734 张表里认出业务对象"，砍掉噪声等于把最难的一步免了，S1 分数会虚高。

但第三层也不能留空表：ontoMeta 靠样例值与行数做分类信号，全空会让分类器一律判 `data_table`，分数向下失真——那同样不是真实水平。所以稀疏灌几行，保证信号存在。

**库名**：`_erp_o2c_l1` / `_erp_o2c_l2`（MySQL）、`odoo_o2c_l1` / `odoo_o2c_l2`（Postgres）。两档各建独立库，**不要在同一库上改来改去**——采集会串味，且不可重放。

**Frappe 列类型映射**（DDL 生成器用，与真实采集到的原生类型一致）：

| fieldtype | 列类型 | | fieldtype | 列类型 |
|---|---|---|---|---|
| Data / Link / Select | `varchar(140)` | | Int | `int(11)` |
| Currency / Float / Percent | `decimal(21,9)` | | Check | `int(1)` |
| Date | `date` | | Text / Long Text | `longtext` |
| Datetime | `datetime(6)` | | Small Text | `text` |

外加框架列 `name`(主键) / `owner` / `creation` / `modified` / `modified_by` / `docstatus` / `idx`，子表另加 `parent` / `parenttype` / `parentfield`。

### 3.2 业务剧本

一家消费品分销商。**生成 6 个月（2026-02-01 ~ 2026-07-31），指标观察窗取最后 3 个月**——账龄要凑齐 0-30 / 31-60 / 61-90 / 90+ 四档，只造 3 个月的话 90+ 档几乎没有样本，坏账和 DSO 直接失去区分度。

| 主数据 | 量 | | 交易 | 量 |
|---|---|---|---|---|
| 客户 | 500 | | 销售订单 | ~6000 |
| SKU / Item | 800 | | 订单行 | ~15000 |
| 仓库 | 6 | | 交货单 | ~5200 |
| 销售员 | 12 | | 销售发票 | ~5000 |
| | | | 回款 | ~4300 |

本机 MySQL/Postgres 上是秒级查询量级，又足够让指标形成分布。

### 3.3 双系统的重叠与冲突（S3 十道题的靶子）

必须**可控且有真值**，这是跨系统档能不能打分的前提：

| # | 冲突 | 注入方式 | 量 |
|---|---|---|---|
| 1 | 客户主数据重复 | 两边都有的 120 个客户中：45 个名称完全一致（易）、45 个有后缀/拼写差异（"上海远洋贸易有限公司" vs "上海远洋贸易"）、30 个仅税号或电话相同而名称完全不同（难） | 120 |
| 2 | 商品粒度差 | Odoo 用 `product.template`(SPU) + `product.product`(SKU)，ERP 只有 `Item`(SKU)；200 SPU ↔ 800 SKU | 200 |
| 3 | 订单双写 | 1800 张线上订单两边都有，单号规则不同（`SO0001` vs `SAL-ORD-2026-00001`），靠 `po_no` 关联；**其中 60 张 ERP 侧缺失**（未下传） | 1800 |
| 4 | 状态机差异 | Odoo `state ∈ {draft,sent,sale,done,cancel}` vs ERP `status ∈ {Draft,To Deliver and Bill,To Bill,To Deliver,Completed,Cancelled,Closed}`，映射表落盘为真值 | 全量 |
| 5 | 币种与税 | 少量外币订单，两边税率口径不同 | ~180 |

第 3 条那 60 张缺失单，就是跨系统题「线上下单但 ERP 里没有的有多少」的答案；第 1 条那 30 个难匹配客户，是「同一客户两系统应收合计」的胜负手。

### 3.4 脏案例配方

不造这些，指标验不出真问题：

| 案例 | 量 | 冲击的指标 |
|---|---|---|
| 部分发货（1 张 SO 分 2–3 次 DN） | 800 | 履约周期、按时交付率 |
| 超发 / 短发（DN 数量 ≠ SO 数量） | 120 | 履约率口径 |
| 退货（负数量） | 260 | 退货率、毛利 |
| 部分回款 + 多单合并回款 | 640 | DSO、账龄 |
| 坏账（超期 90 天未回款） | 95 | 账龄分布、坏账率 |
| 跨期（6 月订单 7 月开票） | 430 | 月度口径归属 |
| 改价（SO 修订，`amended_from` 链） | 150 | 金额口径、版本追溯 |
| 取消单 | 210 | 分母该不该含取消 |
| 客户名前后空格 / 全半角混用 | 40 | **JOIN 陷阱** |
| 分摊金额尾差（0.01） | ~300 | **聚合口径陷阱** |

最后两类是专门给 B1（无本体直接问）设的坎——不知道语义只按字面 JOIN，会静默漏行或对不上账。

### 3.5 分布要像真的

- 客户下单频次走幂律（20% 客户贡献 ~70% 订单）
- 订单金额对数正态
- 回款账期按信用等级分 30 / 60 / 90 天三档，叠正态噪声
- 周末订单量下降、月末冲量

否则 DSO、账龄、复购率算出来接近均匀分布，题目失去区分度，B1 和 B2 都能蒙对。

### 3.6 真值同时落盘（关键设计）

造数脚本写业务表的同时，写一份 `benchmark/truth.json`：

- 每个跨系统冲突的答案（哪 60 张单未下传、哪 120 个客户是同一人、SPU↔SKU 映射）
- 8 个金标准指标按月 / 按客户维度的期望值

于是 50 题的答案有**两条独立来源**：手写金标准 SQL，和造数真值。两者不一致 → 其中一个是错的，必须查清再往下走。

这条交叉校验专防一个最隐蔽的失败模式：**金标准 SQL 自己写错了**。没有它，B2 答错但金标准也错时会被判成"答对"，整份报告失去意义。

---

## 4. S4：两档 schema 变体与采集

### 4.1 脚本是双向的

两个系统的原生形态正好相反：

| 系统 | 原生 | 需要做什么 |
|---|---|---|
| ERPNext | **天然 L2**——Frappe 不写列注释、不建 FK 约束（应用层管） | 造 L1 要**反向补**：读 gold 生成 `COMMENT` 与 `FOREIGN KEY` DDL |
| Odoo | **天然 L1**——有真 FK、字段有 description | 造 L2 要**剥掉**：删 FK 约束、清空 COMMENT |

所以 `schema_variant.py` 同时提供 `--enrich`（补，造 L1）与 `--strip`（剥，造 L2）。

顺带一个真实感红利：ERPNext 表名带空格（`` `tabSales Order` ``）、Odoo 全小写下划线，方言与引用鲁棒性一并压测。

### 4.2 采集隔离

- DataHub recipe 的 `table_pattern.deny` **必须屏蔽**：`tabDocType`、`tabDocField`、`tabCustom Field`、`ir_model`、`ir_model_fields`
- L1 / L2 用不同 `platform_instance`，避免 URN 撞车
- 采集完立刻跑 `python -m scripts.extract_gold_ontology --check-leak`，它现在会报扫描基数，空本体不会假绿

---

## 5. 可重复性铁律

- 固定随机种子；生成器幂等（先 DROP 再建）
- 一条命令跑完，产出 `manifest.json`：各表行数、内容校验和、seed、生成时间、erpnext/odoo 的 commit sha
- 数据冻结后打 tag。之后任何改动都要重跑**全部**对照组——B0/B1/B2 在不同数据上的分数不可比

---

## 6. 工作量与风险

| 任务 | 人日 | 风险 |
|---|---|---|
| Frappe repo → gold + DDL 生成 | 0.5 | 低 |
| Odoo 一次性源码初始化 + dump | 0.5–1 | **中**（C 依赖编译；降级见 §2.2） |
| **O2C 造数生成器 + 真值** | **2** | 中——最大一块，也最值钱 |
| schema 变体 + 采集接线 | 0.5 | 低 |
| 合计 | **3.5–4** | 落在 W1 内，但没有余量 |

**排期建议**：造数生成器先开工，别等 Odoo 装完——Frappe 侧完全不依赖 Odoo，两条线可并行。Odoo 若在 W1 中段仍未装通，立即切 §2.2 降级方案，不要让它拖住主线。

---

## 7. 待写脚本清单

```
benchmark/prepare/
├── build_erp_schema.py      # DocType JSON → MySQL DDL（全量建表）
├── odoo_schema.sql          # 一次性产物，pg_dump -s
├── generate_o2c_data.py     # 造 6 个月单据 + 脏案例 + truth.json
├── schema_variant.py        # --enrich / --strip，造 L1/L2
├── datahub_recipe_l1.yml    # 含金标准表 deny 规则
├── datahub_recipe_l2.yml
└── run_all.sh               # 一键重放
```

外加对 `backend/scripts/extract_gold_ontology.py` 增加 `--system frappe-repo`（复用现有转换逻辑，不改 `build_frappe_gold`）。
