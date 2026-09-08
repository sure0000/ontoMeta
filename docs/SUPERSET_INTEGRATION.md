# 数据应用：Apache Superset 集成

> **一句话**：图表与看板建在 Superset 里，ontoMeta 只做三件事——把治理好的落点变成
> Superset 数据集、把受限的图表规格编译成 Superset 能收的参数、把建出来的东西登记下来。

---

## 0. 为什么换掉自研

自研那套（`DataApp` + `DataAppWidget`，见已废弃的 [DATA_APP_GRAFANA_MODEL.md](./DATA_APP_GRAFANA_MODEL.md)）
的图表引擎是一份 117 行的手写内联 SVG：只实现了表格 / 柱状 / 指标卡三种，模型与
TypeScript 类型里声明的 `line` / `pie` 根本没有渲染分支，`viz_json` 是无 schema 的自由字典，
口径编译还在把过滤值直接拼进 SQL。

BI 呈现不是本系统的核心能力，也不该由本系统重造。ontoMeta 的核心是**本体治理与数据落点**；
呈现交给成熟开源件，平台只保留自己独有的那部分价值——**口径**。

---

## 1. 分工

```
Agent（dsh / Claude Desktop 等）
  │  MCP：ensure_superset_dataset → create_superset_chart → create_superset_dashboard
  ▼
ontoMeta
  ├─ dataset_catalog.resolve_dataset_ref("obj:<id>@serving") → 物理表（库.表）
  ├─ 把本体属性的中文名与描述推成 Superset 列的 verbose_name / description
  ├─ ChartSpec ──(确定性编译)──▶ Superset 的 params + query_context
  └─ SupersetAsset 登记簿：建了什么、建在哪个落点上、谁建的、上次对账还在不在
  ▼
Apache Superset ← 唯一的图表渲染与存储方（版本、权限、分享都归它）
```

**ontoMeta 不部署 Superset**，只连接已经跑着的实例（与 llm / datahub / airflow 同一套
「只连不部署」机制，见 [DEPENDENCY_COMPONENTS.md](./DEPENDENCY_COMPONENTS.md)）。

---

## 2. 三条刻意的设计

### 2.1 不让 Agent 手写 Superset 的 `params`

Superset **没有**按 `viz_type` 暴露参数 schema：`params` 是一个 JSON 字符串，
`query_context` 是另一套结构，两者必须自洽。让模型去凑一份自由字典，产出的多半是
一张能建出来、打开却是空的图（社区里"API 建的图 metric 那一格是空的"就是这么来的）。

所以 `services/superset_spec.py` 收一个**受限规格**并同源产出两份结构：

| 内部 viz | Superset viz_type | 形状约束（编译器强制） |
| --- | --- | --- |
| `table` | `table` | 至少一个维度或度量 |
| `bar` | `echarts_timeseries_bar` | ≥1 度量；x 轴取 `time_column` 或首个维度 |
| `line` | `echarts_timeseries_line` | 同上 |
| `pie` | `pie` | 恰好 1 维度 + 1 度量 |
| `kpi` | `big_number_total` | 恰好 1 度量，**不能有维度** |

形状不对在**任何 IO 之前**就被拒绝（`validate_chart_spec`），不会先登录 Superset 再失败。

看板布局同理：只把图关联到看板而不写进 `position_json`，Superset 会收下这个关联，
但看板打开是空的。`build_position_json` 确定性生成布局，有 golden 测试钉住。

### 2.2 数据集只能从落点建

`ensure_superset_dataset` 只接受目录里的落点引用（`obj:<id>@serving` / `obj:<id>@ods` /
`logic:<id>@ads`），并在建之前检查 `source_ready`——落点还没建出来时如实报错，
而不是建一个查不出数的数据集。

建完顺带把本体语义推过去：属性的中文名 → 列的 `verbose_name`，描述 → `description`。
**这是选它而非裸表的全部理由**：业务在 Superset 里看到的是中文字段名与口径说明。
推的时候整份列集合回传（Superset 的 PUT 是整体替换），别人手填的 `verbose_name` 不会被抹掉。

### 2.3 登记簿不冒充存在性

`superset_assets` 是**索引不是真源**——Superset 才是权威。`state` 三态：

- `unknown`：还没对过账。**不等于不存在**。
- `active` / `missing`：上次对账的结果，由显式对账（`list_superset_assets(reconcile=true)`
  或页面上的「与 Superset 对账」）写入。

对账只改状态、不删登记：登记行本身回答了"我们当时建过什么"。
解除登记（`DELETE /api/superset/assets/{id}`）也**只解除登记**，不删 Superset 里的东西——
删外部系统里的对象是另一个决定，得到那边去做。

---

## 3. 代码地图

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 连接 | `services/dependency_service.py` | `superset` 组件目录项、连接 schema、`_probe_superset` |
| 连接 | `services/settings_service.py` | `SupersetRuntimeConfig` / `get_superset_runtime` |
| 客户端 | `connectors/superset.py` | 登录（JWT + CSRF）、dataset/chart/dashboard、guest token |
| 编译 | `services/superset_spec.py` | `ChartSpec` → `(params, query_context)`；看板 `position_json` |
| 编排 | `services/superset_service.py` | 落点→数据集、建/改图、建看板、对账、guest token |
| 登记 | `models/superset.py` | `SupersetAsset` |
| MCP | `mcp/tools/superset.py` | 6 个工具（见 [MCP_README.md](./MCP_README.md)） |
| 指引 | `mcp/skills/ontometa-viz/SKILL.md` | Agent 的操作顺序与准确性规则 |
| REST | `api/superset.py` | 前端用：状态、资产列表、对账、guest token、解除登记 |
| 前端 | `pages/VizAssetsPage.tsx` / `VizAssetViewPage.tsx` | 资产列表 + 跳转；看板内嵌预览 |

---

## 4. 配置

设置页「基础设施 → Superset BI」，五个字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `base_url` | ✅ | **后端**访问 Superset 的地址 |
| `username` / `password` | ✅ | 服务账号。密码是机密字段，掩码回显、留空 = 保持原值 |
| `database_id` | | Superset 里那条指向数仓（Doris）的 database 编号 |
| `public_base_url` | | **用户浏览器**可达的地址；留空回落到 `base_url` |

两处刻意**不推导**：

- `database_id` —— 由 Superset 侧建好并自测通过，ontoMeta 只认这个 id。否则就得把数仓
  凭据再复制一份推给 Superset，那是另一套密钥的事。
- `public_base_url` —— 后端从哪儿访问 Superset 与用户从哪儿访问它是两回事（内网 ip vs
  对外域名），跳转链接与嵌入 SDK 只能用后者。

拨测是三步：登录拿 JWT → 用它打一次真实 REST（`/api/v1/me/`）→ 校验 `database_id` 存在。
三步都不能省，每一步对应一种"填了等于没填"（只看登录状态码时，反代或静态页返回的
200 + HTML 会给假绿灯）。

### Superset 侧的前置条件（属于部署，不由 ontoMeta 配）

- 内嵌预览需要 `EMBEDDED_SUPERSET` 特性开关，以及允许 ontoMeta 前端源的嵌入域名配置；
  没开只是不能内嵌，跳转链接照常可用（建看板的回执里会写明 `embed_error`）。
- 跨域取 guest token 需要 `ENABLE_CORS` 放行 ontoMeta 前端源。
- 指向 Doris 的 database 需要 Superset 镜像里装着对应 driver（Doris 走 MySQL 协议）。

---

## 5. 验证

```bash
# 后端
cd backend && .venv/bin/pytest -q tests/test_superset_component.py \
  tests/test_superset_spec.py tests/test_superset_service.py \
  tests/test_superset_api.py tests/test_mcp_superset.py

# 前端
cd frontend && npm run build && npm run lint:tokens
```

真实实例上还要走一遍（这些是单测覆盖不到的）：

1. 设置页填连接 → 拨测应为「已连接」；故意把 `base_url` 指向 Superset 前端反代或错端口
   → 必须**失败**而不是绿灯。
2. `get_playbook(topic="ontometa-viz")` → Agent 拿到指引。
3. `list_datasets` 找一个 `source_ready` 的落点 → `ensure_superset_dataset` →
   Superset UI 里数据集存在、列带中文 `verbose_name`。
4. `create_superset_chart` 逐个试 bar/line/pie/kpi/table → 打开返回的 url，
   **图要真的出数**，metric 那一格不能是空的。
5. `create_superset_dashboard` → 看板里图要真的排上（验证 `position_json`）。
6. ontoMeta「数据应用」页看到资产 → 看板内嵌能加载（验证 guest token 与嵌入配置）。

---

## 6. 已知边界

- **口径漂移**：数据集建好之后，本体侧改了字段中文名或口径，Superset 不会自动跟。
  `ensure_superset_dataset` 是幂等可重跑的（重跑即回推最新语义），但"何时重跑"目前
  靠人工/Agent 触发，没有自动化。
- **单图不能内嵌**：Superset 的 Embedded SDK 只支持看板。图表只给跳转链接。
- **不做版本**：图表版本、权限、分享全归 Superset，ontoMeta 不复制一份——复制出来的
  副本迟早与那边被人手改过的版本分叉，分叉之后没人知道该信哪一份。
