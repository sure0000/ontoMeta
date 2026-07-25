# ontoMeta 数据应用（Data App）能力设计方案

> 目标：在现有「智能问数（Chat BI）」之外，让用户能在对话中**创建数据应用**
> ——数据表格页面、可视化大屏——并支持**预览**与**发布**。
> 本文先给出业界成熟方案调研，再给出贴合 ontoMeta 架构的落地方案。

---

## 0. 一句话结论

ontoMeta 已具备**本体语义层 + Chat BI（NL→口径拆解→suggested_sql→落地引用）**，
但**缺少「查询执行层」**（当前只产出 SQL 建议，不连物理数据源、不返回行数据）。
「数据应用」= 在语义层之上补齐 **执行层 + 应用定义模型 + 渲染器 + 预览/发布闭环**。
推荐走 **WrenAI / SuperSonic 的「语义层驱动 GenBI」范式**，
渲染与大屏编辑器借鉴 **OpenDataV / DataRoom 的「配置 JSON 驱动 + 拖拽 + 预览/发布」**，
不整体 fork 重型 BI（Superset/Metabase），保持与本体的强绑定与自研可控。

---

## 1. 业界成熟方案调研（GitHub / 开源）

### 1.1 语义层 / GenBI（与 ontoMeta 定位最契合）

| 方案 | Stars | 技术栈 | 与本项目关系 |
| --- | --- | --- | --- |
| **Canner/WrenAI** | ~16.6k | Python | GenBI：基于「上下文/语义层」把自然语言→**可信的 dashboard/chart/SQL**，接 20+ 数据源。**范式最接近**：对话直接产出数据应用。可借鉴其 MDL（建模定义语言）→ SQL 生成与校验管线。 |
| **tencentmusic/SuperSonic** | ~5.0k | Java | 明确统一 **Chat BI（LLM）+ Headless BI（语义层）**。语义模型 + 术语 + 指标 + 对话式分析。理念与 ontoMeta 「本体作为语义权威」高度一致，可借鉴其「指标/维度/口径」到查询的映射。 |
| **cube-js/cube** | ~20.5k | Rust/TS | Headless 语义层（度量/维度/预聚合/多数据源/缓存），供 BI、AI、嵌入式复用。可作为**执行层外挂引擎**：ontoMeta 本体 → 生成 Cube schema → Cube 负责跨源查询与缓存。 |

### 1.2 大屏 / 可视化低代码编辑器（渲染 + 编辑 + 发布）

| 方案 | Stars | 技术栈 / License | 可复用点 |
| --- | --- | --- | --- |
| **gcpaas/DataRoom** | ~0.8k | Vue / Apache-2.0 | **AI 对话式生成大屏/页面**，几十种图表，20+ 数据源，前后端一体，可嵌入。与需求几乎正面命中，值得逐模块借鉴（大屏 schema、数据集、发布/分享）。 |
| **AnsGoo/openDataV** | ~1.4k | Vue3 + TS / Apache-2.0 | **纯前端拖拽式大屏低代码**编辑器 + 自定义组件接入。可直接借鉴其「组件注册表 + 画布 + 配置面板 + 快照渲染」架构，用 React 重写核心。 |
| **ggymm/data-view-web** | ~0.5k | 数据可视化大屏 + 报表设计器 | 参考大屏与报表两态设计。 |

### 1.3 表格 / 内部工具低代码（数据表格页面）

| 方案 | Stars | 说明 |
| --- | --- | --- |
| **illacloud/illa-builder**（开源版 Retool） | 高 | 拖拽搭建 CRUD/表格/后台，绑定多种数据源与 REST。参考其「组件 + 数据查询 + 事件」模型做表格页。 |
| ToolJet / Appsmith | 高 | 同类内部工具平台，参考表格分页/筛选/联动交互范式。 |

### 1.4 完整 BI（作为对照，不建议整体引入）

| 方案 | Stars | 说明 |
| --- | --- | --- |
| **apache/superset** | ~74k | 图表 + Dashboard + SQL Lab，功能全但重、耦合自身元数据体系，接入本体语义成本高。 |
| **metabase/metabase** | ~48k | 易用 + 嵌入式，Clojure 栈，二开与语义对齐成本高。 |

### 1.5 图表渲染库（组件级依赖，实现时直接用）

- **Apache ECharts**：大屏/常规图表主力（折线/柱/饼/地图/仪表盘）。
- **AntV G2/G2Plot / Recharts**：与 React 生态契合的统计图表。
- 表格：**AG Grid / TanStack Table**（虚拟滚动、分组、导出）。
- 画布交互：react-grid-layout（栅格看板）/ 绝对定位 + 缩放（自由大屏）。

### 1.6 调研结论

- **没有**一个开源项目能「零改造」满足「以某套自定义本体为语义权威 + 对话生成 + 表格&大屏 + 预览&发布」全链路——都需要二次开发。
- **最省力且可控**的路径：**自研薄应用层**（应用定义 + 渲染 + 发布），
  **执行层**优先自研轻量 SQL 执行（DuckDB/直连），中期可外挂 **Cube** 做跨源与预聚合，
  **对话生成**沿用并扩展现有 Chat BI 管线（新增「生成应用」意图）。

---

## 2. 现状盘点（代码事实）

- 语义资产：`ObjectType / Property / RelationType / BusinessLogic`（含对象/字段绑定、字段级溯源、版本）。
- Chat BI：`backend/app/services/chat_bi.py`
  - 产出 `answer / suggested_sql / caliber_decomposition / referenced_objects / referenced_logics`；
  - 强 grounding：无命中拒答；`_ReferenceResolver` 把名称回填为真实实体 id。
  - **只产出 SQL 文本，不执行**（见 `_build_mock_sql`、注释「需映射到物理表后执行」）。
- 无查询执行层、无数据源连接模型（`manual_creation.py` 仅生成建表 DDL，不执行）。
- 后端：FastAPI + SQLAlchemy + Alembic；管理面 `ONTOMETA_ADMIN_TOKEN`，对外面 App API Key + scope + 限流。
- 前端：React + react-router；页面注册在 `frontend/src/App.tsx`，API 收敛在 `frontend/src/api.ts`。
- 对外：REST `/api/v1/*` + MCP，目录同源（`external_api.py`）。

> **关键缺口**：① 数据源连接与 SQL 执行；② 数据应用定义/存储；③ 渲染器（表格/图表/大屏画布）；④ 预览与发布/分享。

---

## 3. 目标能力（用户故事）

1. 在 Chat BI 对话中输入「把近 30 天各渠道订单量做成柱状图大屏」→ 系统基于本体拆解口径、生成查询与图表配置 → **一键生成数据应用草稿**。
2. 数据应用有两种形态：**数据表格页面**（DataTable）与**可视化大屏**（Screen/Dashboard）。
3. 应用可**编辑**（数据集口径、可视化配置、布局）、**预览**（拉真实/示例数据渲染）、**发布**（生成只读可分享页面 + 版本快照）。
4. 应用中每个数据集都**落地到本体引用**（对象/字段/业务逻辑），与现有 grounding 与溯源一致。

---

## 4. 领域模型扩展

新增聚合（沿用现有「状态 + 版本 + 溯源 + 确认」范式）：

### 4.1 DataApp（数据应用，聚合根）
- id, domainContextId, ontologyId（绑定已发布本体快照）
- type：`data_table` | `screen`
- name, description, owner
- status：`draft` | `in_review` | `published` | `archived`
- currentVersion, publishedVersion, publishedAt
- spec_json：应用配置（见 §4.4）
- source：`chat_generated` | `manual`
- createdBy, updatedAt

### 4.2 DataAppDataset（应用数据集 = 一次可执行的本体查询）
- id, dataAppId
- name
- primaryObjectTypeId（主对象）
- **binding_json**：口径拆解落地——measures/dimensions/filters/timeRange，
  每项引用 `{kind, id, name}`（object_type/property/business_logic），复用 Chat BI 的 `caliber_decomposition` 结构。
- compiled_sql（由 binding 编译，评审可见）
- dataSourceId（执行目标；为空=示例/Mock）
- refreshPolicy：`on_open` | `manual` | `cron`（cron 延期）

### 4.3 DataSource（数据源连接，新增）
- id, name, kind：`postgres|mysql|duckdb|clickhouse|http`
- dsn_secret_ref（密钥仅存引用/加密，不明文）
- 与 `DomainContext` / DataHub dataset urn 的映射（把本体 name → 物理表/列）
- status, testedAt

### 4.4 spec_json（应用配置，前端渲染契约）
```jsonc
// data_table
{ "layout":"table",
  "datasetId":"...",
  "columns":[{"propId":"...","name":"amount","title":"金额","format":"currency","agg":"sum"}],
  "pagination":{"pageSize":20}, "filtersUI":[...], "sort":[...] }

// screen（大屏）
{ "layout":"screen",
  "canvas":{"width":1920,"height":1080,"bg":"..."},
  "widgets":[
    {"id":"w1","type":"bar","rect":{"x":40,"y":40,"w":600,"h":360},
     "datasetId":"ds1","encode":{"x":"channel","y":"order_cnt"},"options":{...}},
    {"id":"w2","type":"kpi","rect":{...},"datasetId":"ds2","metric":"gmv"}
  ] }
```

### 4.5 DataAppVersion（发布快照，复用 VersionRecord 范式）
- id, dataAppId, version, spec_snapshot_json, datasets_snapshot_json, diffSummary, operator, createdAt
- 发布即冻结「本体引用 + spec + 编译 SQL」，可只读回看/回滚。

### 4.6 状态与规则（与现有治理闭环对齐）
- 应用先草稿后发布；发布走 `ChangeConfirmation` 二次确认。
- 数据集必须**可落地到已发布本体引用**（无命中拒绝保存，复用 grounding 规则）。
- 发布创建版本记录；已发布应用被改进入新草稿版本（不无痕覆盖）。

---

## 5. 后端接口设计（挂 `/api`，管理 Token）

```
# 数据源
GET/POST/PATCH/DELETE /api/data-sources
POST  /api/data-sources/{id}/test          # 连接测试

# 数据应用 CRUD
GET   /api/data-apps?domain_id=&type=
POST  /api/data-apps                        # 手工新建
GET/PATCH/DELETE /api/data-apps/{id}

# 数据集与查询编译/执行
POST  /api/data-apps/{id}/datasets          # 新增/更新数据集（binding→编译 SQL）
POST  /api/data-apps/{id}/datasets/{dsId}/compile   # 仅编译，回显 SQL + 校验落地
POST  /api/data-apps/{id}/datasets/{dsId}/preview   # 执行(或Mock)返回行数据/聚合结果

# 预览与发布
POST  /api/data-apps/{id}/preview           # 整页预览数据聚合
POST  /api/data-apps/{id}/publish            # 走 confirmation → 冻结版本
GET   /api/data-apps/{id}/versions           # 版本列表 / diff / snapshot

# 对话生成（扩展 Chat BI）
POST  /api/chat-bi/generate-app              # {conversation_id?, domain_id, question|message_id, type}
                                             #  → 复用 ask 的口径拆解 → 产出 DataApp 草稿
```

对外只读（App API Key，供嵌入/分享，扩展 `external_api.py` 目录）：
```
GET /api/v1/data-apps/{publishedId}          # 已发布应用 spec（只读）
GET /api/v1/data-apps/{publishedId}/data     # 已发布应用数据（scope: dataapps:read + 限流）
```

---

## 6. 查询执行层（新增，最关键）

**分层设计（本体 name → 物理执行）：**

1. **Binding Compiler**：把数据集 `binding_json`（measures/dimensions/filters/timeRange，
   引用本体对象/字段/业务逻辑）编译为标准 SQL。
   - 复用 `chat_bi._build_mock_sql` 的思路升级为「基于绑定的确定性编译」（非 LLM 猜测）。
   - 业务逻辑（指标/口径）内联其 `expressionSummary`/表达式，保证口径一致。
   - 物理映射：`Property.sourceFieldRef` / `ObjectType` → DataHub dataset urn → 物理表名/列。
2. **Executor**：
   - 起步：**DuckDB**（可读 CSV/Parquet/示例数据，零运维）+ **直连 Postgres/MySQL**（SQLAlchemy engine，只读账号）。
   - 安全：白名单只读、语句类型校验（仅 SELECT）、`LIMIT` 强注入、超时、结果行上限。
   - 中期：外挂 **Cube**——本体 → 生成 Cube data model → 由 Cube 承担跨源/预聚合/缓存/行级权限。
3. **无数据源时**：`preview` 返回**确定性 Mock 行数据**（依据字段 semanticType 造数），保证「预览」在未接数据源时仍可体验（与现有 `USE_MOCK_*` 风格一致）。

---

## 7. 前端设计

新增一级/二级导航与页面（注册于 `App.tsx`，API 加入 `api.ts` 的 `api.dataApps.*`）：

```
/data-apps                     列表（按 domain / type 过滤，状态徽标复用 StatusBadge）
/data-apps/new?type=           新建
/data-apps/:id/edit            编辑器（表格 or 大屏）
/data-apps/:id/preview         预览（拉数据渲染，只读）
/apps/:publishedId             已发布只读页（可分享/嵌入 iframe）
```

**编辑器（配置 JSON 驱动，借鉴 openDataV/DataRoom）：**
- 左：组件/图表面板（bar/line/pie/kpi/table/text…，组件注册表）。
- 中：画布——表格页用表单式布局；大屏用绝对定位 + 缩放自适应（1920×1080 基准）。
- 右：属性面板 = 数据集绑定（选本体对象/字段/业务逻辑，复用 Chat BI 的 `ChatBiReferences` 选择器）+ 可视化编码 + 样式。
- 渲染器 `<AppRenderer spec>` 预览/发布共用一套（ECharts + AG Grid/TanStack Table）。

**对话生成入口（改造 `ChatBiPage`）：**
- 每条 assistant 回答（含 `caliber_decomposition` + `suggested_sql`）下方加按钮：
  「生成表格」「生成大屏」→ 调 `/api/chat-bi/generate-app` → 落 DataApp 草稿 → 跳编辑器。
- 复用现有 grounding 与引用结构，天然继承「不编造、可溯源」。

---

## 8. 与现有能力的对齐

| 现有机制 | 数据应用如何复用 |
| --- | --- |
| Grounding / `_ReferenceResolver` | 数据集绑定必须落地到真实本体实体，无命中拒绝保存。 |
| `caliber_decomposition` | 直接作为 DataAppDataset 的 `binding_json` 骨架。 |
| ChangeConfirmation | 发布应用走二次确认。 |
| VersionRecord 范式 | DataAppVersion 冻结快照、diff、回滚。 |
| 字段级溯源 origin/user_created | 应用 `source=chat_generated/manual`，编辑保留操作者。 |
| External App Key + scope + 限流 | 新增 `dataapps:read` scope，支撑嵌入/分享的对外只读。 |
| Alembic | 新增表走迁移；SQLite 开发 / PG 生产一致。 |

---

## 9. 分阶段实施

> 实现状态（截至当前提交）：阶段 1 ✅ 完成、阶段 2 ✅ 基本完成、阶段 3 🟡 部分完成。

**阶段 1（MVP，纯语义 + Mock 数据，不接物理源）— ✅ 完成**
- 表：`data_apps` / `data_app_datasets` / `data_app_versions`（Alembic）。
- Binding Compiler（本体→SQL 文本）+ Mock Executor（造数）。
- 后端 CRUD + preview(mock) + publish(confirmation+version)。
- 前端：列表 + 表格编辑器 + `<AppRenderer>`（表格 + ECharts）+ 预览/发布只读页。
- Chat BI「生成表格/大屏」按钮 → 生成草稿。
- 交付：对话即可生成、预览（示例数据）、发布可分享的应用。

**阶段 2（真实数据）— ✅ 基本完成**
- ✅ `data_sources` 模型（含 `mapping_json` 物理映射）+ 连接测试
- ✅ 只读安全执行器 `data_app_executor`：仅 SELECT/WITH、禁危险关键字、强制 LIMIT、
  SQLite/DuckDB 时间函数方言适配、物理映射整词替换；SQLAlchemy 直连（sqlite/duckdb/pg/mysql）
- ✅ `preview` 优先真实数据源执行、失败降级 Mock
- ✅ 大屏拖拽画布编辑器（增删组件、拖动/缩放、属性面板、绑定数据集、等比缩放）
- ✅ 对外只读 API `/api/v1/data-apps[/{id}][/data]` + `dataapps:read` scope + iframe 嵌入页 `/embed/apps/:id`
- 🟡 物理映射的可视化配置为 JSON 输入（进一步做成表单映射为后续项）

**阶段 3（增强）— 🟡 部分完成**
- ✅ MCP 工具暴露：`list_data_apps` / `get_data_app` / `query_data_app`（目录同源）
- ✅ 导出 CSV（前端客户端导出）
- ✅ 参数化筛选联动 / 下钻：preview 支持运行时 filters；大屏全局参数栏 + 柱图点击下钻
  （编辑器/只读页共用 ParamBar，Mock 与真实数据源均生效）
- ✅ **CubeConnector 已实现（生产级）**（`backend/app/connectors/cube.py`）：本体→Cube data model
  生成（含 **pre_aggregations + refresh_key + joins**）、绑定→Cube 查询翻译、Load API 执行、
  HS256 JWT（含 securityContext 行级权限）、可部署文件导出（model/cubes/*.js + cube.js 含
  RLS queryRewrite）；`kind=cube` 数据源接入 preview；对外 `/v1/data-apps/{id}/data` 以应用为租户
  注入 securityContext；`USE_MOCK_CUBE` 开关本地零依赖。部署 checklist 见 §10.1。
  **预聚合定时刷新交由 Cube Refresh Worker**，因此也覆盖了下方“cron 定时刷新”。
- ⛔ cron 定时刷新（非预聚合类任务，如定时导出/推送）：需多实例任务队列（与 B5.1 同批），延期

**阶段 2/3 遗留（原文）**

---

## 10. 风险与取舍

- **SQL 执行安全**：只读账号 + 仅 SELECT + 强制 LIMIT + 超时 + 语法白名单；密钥加密存储。
- **本体 name→物理表映射不完整**：MVP 用 Mock 兜底；映射缺失时明确提示「需配置数据源映射」。
- **不整体引入 Superset/Metabase**：避免元数据体系割裂与重运维；改为借鉴 + 自研薄层。
- **大屏编辑器工作量大**：阶段 1 先做表格 + 栅格看板，阶段 2 再做自由大屏画布。
- **口径一致性**：查询一律经 Binding Compiler + 业务逻辑表达式内联，避免 LLM 随手 SQL 造成口径漂移。

---

## 10.1 生产部署 Cube（外挂，checklist）

> ontoMeta 只做中枢：生成模型/翻译查询/签发带 securityContext 的令牌；执行、缓存、
> **预聚合定时刷新**、行级权限均由 Cube 承担。

1. **起 Cube 服务**：`docker-compose.yml` 取消注释 `cube` 与 `cube_refresh_worker`
   （或独立部署）。`cube_refresh_worker` 设 `CUBEJS_REFRESH_WORKER=true` → 预聚合定时刷新。
2. **配 ontoMeta 环境变量**：
   ```
   USE_MOCK_CUBE=false
   CUBE_API_URL=http://cube:4000
   CUBE_API_SECRET=<与 Cube 的 CUBEJS_API_SECRET 相同>
   CUBE_PREAGG_REFRESH=1 hour         # 预聚合刷新间隔
   CUBE_TENANT_DIMENSION=tenant_id    # 行级权限列（可选）
   ```
3. **生成并落盘模型**（本体发布/变更时）：
   ```
   GET /api/ontologies/{id}/cube-model/files   # 返回 {model/cubes/*.js, cube.js}
   ```
   把返回的每个文件写入 Cube 挂载目录 `./cube/`（`cube.js` 含 RLS queryRewrite）。
   模型已含 **pre_aggregations + refresh_key + joins**（跨对象关系）。
4. **建数据源**：ontoMeta 中新增 `kind=cube` 的数据源，数据集选它 → 走 Cube 执行。
5. **行级权限**：对外 `/api/v1/data-apps/{id}/data` 以「外部应用」为租户，ontoMeta 自动
   在 JWT 注入 `securityContext.tenant=app_id`；`cube.js` 对含 `tenant_id` 列的 cube
   强制追加 `tenant_id = tenant` 过滤。

> 物理库连接、预聚合存储（可选 ClickHouse）、跨源（可选 Trino）在 **Cube 侧**配置，
> ontoMeta 不感知。

## 11. 参考项目

- WrenAI: https://github.com/Canner/WrenAI
- SuperSonic: https://github.com/tencentmusic/supersonic
- Cube: https://github.com/cube-js/cube
- DataRoom: https://github.com/gcpaas/DataRoom
- OpenDataV: https://github.com/AnsGoo/openDataV
- Superset: https://github.com/apache/superset ・ Metabase: https://github.com/metabase/metabase
