# 验证语料生成器

给「ontoMeta 有效性验证」造 O2C 业务数据。**在 docker 宿主机上执行**（mac 只跑 ontoMeta）。

完整部署步骤看 [`deploy/benchmark/README.md`](../deploy/benchmark/README.md)，本文只讲这个目录。

---

## 它是什么

```
中立台账 (ledger.py)  ──→  truth.json (truth.py)      真值，50 题答案的第二条来源
        │
        ├──→ ERPNext 投递器 (deliver_erpnext.py)      全部 6000 单
        └──→ Odoo   投递器 (deliver_odoo.py)          只投 1800 张线上单
                     │
                     └──→ verify.py                   一致性校验 + manifest
```

**先造台账再双向投递，不要反过来。** 先在 ERPNext 造再同步到 Odoo，会让真值依赖同步
逻辑本身的正确性——等于用被验证的对象去生成金标准，跨系统档的分数就没意义了。

| 文件 | 作用 |
|---|---|
| `generator/config.py` | **配方**：量级、脏案例条数、分布参数。改规模只动这个文件 |
| `generator/ledger.py` | 中立台账：客户/商品/订单/发货/发票/回款，不含任何 ERPNext/Odoo 概念 |
| `generator/truth.py` | 真值、八个金标准指标、两侧预期落地行数 |
| `generator/deliver_erpnext.py` | REST 投递器 |
| `generator/deliver_odoo.py` | XML-RPC 投递器 |
| `generate_o2c.py` | 入口 |
| `verify.py` | 投递后一致性校验，直连两个库跑 SQL |
| `tests/` | 台账层 25 条测试，不依赖任何外部系统 |

---

## 执行顺序

### 0. 先跑测试（不需要任何外部系统）

```bash
python3 -m pytest tests -q
```

25 条全绿再往下。这层跑不过说明配方或真值算法坏了，连系统只会浪费一夜。

### 1. 只生成台账与真值（零依赖，1 秒）

```bash
python3 generate_o2c.py --only ledger --out /srv/ontometa/benchmark/truth.json
```

人工看一眼八个指标是否合理。参考量级（默认配方）：DSO 约 55 天、按时交付率约 75%、
毛利率约 27%、账龄四档都有钱。差一个数量级就是配方或算法有问题，别往下走。

### 2. 配置凭据

```bash
cp .env.example .env    # 若无则手写；本目录的 .env 已被仓库 .gitignore 覆盖
```

```
ERP_URL=http://localhost:8090
ERP_API_KEY=...
ERP_API_SECRET=...
ODOO_URL=http://localhost:8069
ODOO_DB=odoo_o2c
ODOO_USER=admin
ODOO_PASSWORD=...
```

ERPNext 的密钥在 **用户列表 → Administrator → 设置 → API 访问 → 生成密钥**，
`api_secret` 只显示一次。

**别用 `--erp-key` 传密钥**——它会进 shell 历史和 `ps` 输出。命令行参数保留只是为了应急。

### 3. 小规模冒烟

```bash
pip install requests
python3 generate_o2c.py --orders 40 --out /tmp/truth-smoke.json
```

40 单几分钟跑完，脏案例按比例缩到每类至少 1 条，两个投递器该踩的坑都会踩到。

> **投递器从未在真实 ERPNext/Odoo 上跑过**（编写环境没有这两个系统）。冒烟阶段大概率
> 要修字段名或方法签名。修 `generator/deliver_*.py`，但**不要为了跑通去绕开标准转换
> 函数或改动台账逻辑**——那会让数据失去验证价值。

### 4. 恢复 baseline 快照

冒烟那 40 张单必须清掉，否则实际行数与 `truth.json` 对不上。

### 5. 全量（3–5 小时，跑一夜）

```bash
nohup python3 generate_o2c.py --seed 42 --concurrency 8 \
  --out /srv/ontometa/benchmark/truth.json \
  > /srv/ontometa/benchmark/gen.log 2>&1 &
tail -f /srv/ontometa/benchmark/gen.log
```

**不支持断点续跑**：中途失败就恢复 baseline 重跑。半截数据混着重试痕迹比重来更难排查。

### 6. 一致性校验

```bash
pip install pymysql psycopg2-binary
python3 verify.py --truth /srv/ontometa/benchmark/truth.json \
  --erp-dsn  mysql://datahub_ro:pwd@localhost:3306/_erpnext \
  --odoo-dsn postgres://datahub_ro:pwd@localhost:5432/odoo_o2c \
  --manifest /srv/ontometa/benchmark/manifest.json
```

**跳过不等于通过**：缺驱动或缺 DSN 时该项记 SKIP 且整体失败。补齐再跑，别拿带 SKIP
的结果当验收依据。

全绿之后才打数据快照。**校验不过就改生成器、恢复 baseline、重跑——不要手工补数据**：
手工补的行不进 `truth.json`，真值一旦和实际脱节，后面所有交叉校验就都失效了。

---

## 三条不能破的约束

1. **先台账后投递**（见上）。
2. **按单据日期的时间轴推进**，日内按 SO→DN→SI→PE 分阶段。ERPNext 对回溯的库存单据会
   排 `Repost Item Valuation`，乱序会触发成千上万次重算，队列积压到跑一整天也做不完，
   而且是在你以为跑完之后才慢慢显现。生成器已实现，别改坏。
3. **并发固定 8**，与 gunicorn worker 数一致。提交是同步执行在 web worker 里的，开更大只会排队。

另外：单据链走 ERPNext 的标准转换函数（`make_delivery_note` / `make_sales_invoice` /
`get_payment_entry`），它们会带出 `against_sales_order`、`so_detail` 等引用字段——履约周期、
按时交付率、订单到发票的血缘全靠它们。手工拼的链条缺引用，指标会**静默算错**。

---

## 改配方

改 `generator/config.py` 后必须重跑 `tests/`（脏案例条数、冲突数量都有测试钉住），
并且**重跑全部对照组**——B0/B1/B2 在不同数据上的分数不可比。

`--orders N` 会把脏案例与冲突按比例同步缩小，只适合冒烟；正式跑不要用。
