# 数据看板（Dashboard）方案：从单图表到自由组合的对外看板

> 目标：把当前「一次生成一个表格 / 一个图表」的数据应用，升级为**可自由组合多个
> 图表/表格、面向外部发布的数据看板**。本文给出概念模型、数据模型、编辑器、跨组件
> 交互、对外发布与权限、Data Agent 集成、迁移与分阶段落地。

---

## 1. 现状与差距

**现状**（见 `DATA_APP_PLAN.md` 已落地部分）：
- `DataApp` 有两种类型：`data_table`（单/多数据集，Tab 呈现）、`screen`（绝对定位大屏画布）。
- `screen` 已支持多 widget + 拖拽/缩放 + 全局参数栏（ParamBar）+ 柱图下钻。
- 每个 widget 通过 `datasetIndex` 绑定**本 app 内**的 `DataAppDataset`。
- Data Agent 一次生成 = 1 数据集 + 1 widget。

**差距（为什么感觉“都是单个图表”）**：
1. **图表不可复用**：widget + dataset 被锁在单个 app 内，无法把 A 对话生成的图、B 对话
   生成的表拼到同一个看板。
2. **无“看板”这一层**：`screen` 是像素画布（大屏），不适合对外看板的**响应式栅格**布局。
3. **跨域不可组合**：一个 app 绑定一个 domain/ontology，跨数据域的图表无法同板。
4. **对外能力零散**：已有发布/版本/`/v1/data-apps/{id}/data`/embed，但缺看板级的
   分享页、主题、响应式、看板级权限与筛选联动的一等模型。

**结论**：需要引入两个一等概念——**可复用的图表资产（Widget/Chart）** 与
**组合它们的看板（Dashboard）**，并把「大屏」和「表格」统一到看板的布局模式之一。

---

## 2. 目标概念模型

```
Dashboard（看板：一次对外交付物）
 ├─ layout（响应式栅格：每个格子引用一个 Widget + 位置/尺寸）
 ├─ filters（看板级全局筛选，作用到多个 Widget）
 ├─ theme（主题/配色/间距）
 └─ tiles: [ { widget_id, x, y, w, h, param_overrides } ... ]

Widget（图表/表格资产：可复用、可独立预览）
 ├─ type（table / bar / line / pie / kpi / …）
 ├─ dataset（口径绑定：对象/维度/度量/时间/过滤，落地到本体）
 ├─ viz（编码与样式：x/y/encode/format/options）
 └─ data_source_id（mock / 真实库 / cube）

关系：Dashboard *— (tiles) —* Widget（多对多：一个 Widget 可被多个看板复用）
```

要点：
- **Widget 从 app 内“数据集+组件”提升为独立资产**，有自己的 id、绑定、可视化配置、
  可单独预览与被检索。
- **Dashboard 只持有“对 Widget 的引用 + 布局 + 看板级筛选/主题”**，不复制图表定义。
- 「表格」「大屏」不再是两种 app，而是**看板的布局模式**（`grid` 响应式 / `canvas` 像素级），
  或一个看板内混排（普通区用 grid，大屏区用 canvas）。

---

## 3. 两种落地路线（推荐 B）

### 路线 A：最小改动——`screen` 内允许“引用外部图表”
- 在现有 `screen` spec 的 widget 上增加 `widget_ref`（指向可复用 Widget），保留 `datasetIndex` 兼容。
- 优点：改动小。缺点：仍是像素画布、跨域受限、看板/图表边界模糊，治理弱。

### 路线 B（推荐）：引入 Widget 资产 + Dashboard 组合（一等模型）
- 新增 `DataAppWidget`（可复用图表）与把 `DataApp(type=dashboard)` 作为组合层。
- 响应式栅格布局；跨数据域组合；图表库可检索复用；对外发布/权限成体系。
- 优点：真正“自由组合的对外看板”，可长期演进（模板市场、图表复用、跨域）。
- 缺点：改动较大，但可分阶段（见 §9），且**向后兼容**现有 data_table/screen。

> 采用 **B**，并把现有 `screen` 画布作为看板的一种“像素级布局区”保留复用
> （`ScreenCanvas` 组件可直接改造成 canvas 模式的看板区）。

---

## 4. 数据模型变更（路线 B）

### 4.1 新增 `DataAppWidget`（可复用图表/表格资产）
- id, domainContextId, ontologyId
- name, description
- widgetType：`table` | `bar` | `line` | `pie` | `kpi` | `area` | `scatter`
- binding_json：口径绑定（复用现有 `DataAppBinding`：measures/dimensions/filters/time_range）
- viz_json：可视化编码与样式（x/y/series/format/legend/options）
- data_source_id：mock / 物理源 / cube
- compiled_sql（评审可见）、status（draft/published）、source（chat_generated/manual）
- createdBy, updatedAt

> Widget 复用现有 `DataAppDataset` 的绑定与编译/执行/预览管线（`_compile_sql` /
> `preview_dataset` / Cube 路径），只是把“数据集”与“可视化”合并为一个可复用资产。

### 4.2 `DataApp` 增加类型 `dashboard`
- app_type 扩展：`data_table` | `screen` | **`dashboard`**
- spec_json（dashboard）：
```jsonc
{
  "layout": "grid",                 // grid（响应式）| canvas（像素大屏）| mixed
  "grid": { "cols": 12, "rowHeight": 40, "gap": 12, "breakpoints": {...} },
  "theme": { "preset": "light", "bg": "#f5f7fa", "accent": "#2563eb" },
  "filters": [                      // 看板级全局筛选（复用 ScreenParam 结构）
    { "id": "f1", "label": "渠道", "column": "channel", "op": "eq" }
  ],
  "tiles": [
    { "widget_id": "w_abc", "x": 0, "y": 0, "w": 6, "h": 6,
      "param_bindings": { "f1": "channel" } },   // 全局筛选→该 widget 列映射
    { "widget_id": "w_def", "x": 6, "y": 0, "w": 6, "h": 6 }
  ]
}
```

### 4.3 关联表 `DataAppDashboardTile`（可选，或内联在 spec）
- 若需强一致与检索：`dashboard_id, widget_id, x, y, w, h, param_bindings_json`。
- MVP 可先**内联在 spec.tiles**（少一张表，快）；规模化后再抽表。

### 4.4 版本与发布
- 复用 `DataAppVersion`：发布看板时**快照 tiles + 每个引用 Widget 的绑定/编译SQL/viz**，
  保证已发布看板不受后续 Widget 编辑影响（冻结）。
- Widget 也可独立发版；看板发布时记录所引用 Widget 的版本，避免“图表悄悄变了”。

---

## 5. 组合编辑器（前端）

**布局引擎**：引入 `react-grid-layout`（响应式、可拖拽/缩放、断点自适应）作为 `grid`
模式；`canvas` 模式复用现有 `ScreenCanvas`。二者可在一个看板内分区（mixed）。

**编辑器三区**：
- 左：**图表库面板**——列出当前/跨数据域的可复用 Widget（搜索、按域/类型过滤），拖入画布。
  另有「+ 新建图表」「+ 从 Data Agent 生成」入口。
- 中：**看板画布**——响应式栅格，拖拽摆放/调整大小；每格渲染 Widget（复用 `<AppRenderer>`）。
- 右：**属性面板**——看板级（主题/全局筛选/布局列数）、或选中 tile 级（尺寸/标题/参数映射）。

**添加图表的三种来源**（对应“自由组合”）：
1. **图表库**：拖入已存在的 Widget（跨对话、跨域复用）。
2. **Data Agent 生成**：在看板内直接问一句 → 生成 Widget 并落到当前看板（见 §7）。
3. **手工新建**：现有 `DatasetEditor` 升级为「Widget 编辑器」（绑定 + 选图表类型 + 样式）。

**渲染器**：`<AppRenderer widget>` 统一供图表库预览 / 看板编辑 / 对外只读页复用。

---

## 6. 跨组件交互（看板级）

- **全局筛选联动**：看板 `filters` → 通过 tile 的 `param_bindings` 注入每个 Widget 的
  `runtime_filters`（后端 `preview_dataset` 已支持 runtime_filters，直接复用）。
- **下钻**：图表点击 → 生成临时过滤 → 可选“只影响本图”或“作为看板级筛选广播”。
- **时间范围**：看板级时间选择器 → 映射到各 Widget 的 time_range。
- **联动高亮/交叉过滤**（进阶）：点 A 图的某维 → 过滤 B/C 图（基于共享维度列名）。

> 这些**后端能力已具备**（runtime_filters/编译器/Cube），主要是前端看板级状态编排。

---

## 7. Data Agent 集成（“对话即拼板”）

- 对话回答下方在「生成表格 / 生成大屏」之外，新增 **「加入看板」**：
  - 选择目标看板（或新建）→ 用已展示口径生成 **Widget**（复用 §2 修复后的
    “复用 caliber、不重调 LLM”逻辑）→ 追加为看板一个 tile。
- 「看板内问数」：在看板编辑器里直接问一句，生成的 Widget 自动落到当前看板空位。
- 效果：业务/运营用自然语言逐个问出图，**边问边拼**成一个对外看板。

---

## 8. 对外发布与权限（“真正对外”）

- **发布**：走 `ChangeConfirmation` 二次确认 + `DataAppVersion` 冻结快照（含所有 tile 与 Widget 绑定）。
- **分享页**：`/apps/{id}`（控制台内只读）+ `/embed/apps/{id}`（无壳 iframe，已具备，升级为响应式栅格渲染）。
- **公开分享链接**（可选）：发布时生成 `public_token`，`/public/dashboards/{token}` 免登录只读
  （限流 + 可设有效期/口令），区别于需 API Key 的 `/v1`。
- **外部 API**：扩展 `/api/v1/data-apps/{id}`（看板 spec）与 `/data`（各 Widget 数据），
  沿用 `dataapps:read` scope；行级权限经 Cube `securityContext`（已具备）。
- **主题与自适应**：亮/暗主题、移动端断点；大屏区按分辨率等比缩放。
- **看板级权限（进阶）**：看板可见性（私有/组织/公开）、按外部应用/租户过滤（复用 Cube RLS）。

---

## 9. 分阶段实施

**阶段 D1（看板骨架，复用现有能力）**
- `DataApp` 增 `dashboard` 类型 + spec.tiles（内联，不新增表）。
- 引入 `react-grid-layout`，看板编辑器（grid 模式）+ 统一 `<AppRenderer>`。
- tile 先引用**本 app 内数据集**（把现有多数据集 app 直接渲成看板），打通「多图一板 + 发布 + 只读页」。

**阶段 D1（看板骨架，复用现有能力）— ✅ 已落地**
- ✅ `DataApp` 新增 `dashboard` 类型 + `spec.tiles`（内联，未新增表）；grid 默认 spec。
- ✅ 引入 `react-grid-layout`，`DashboardGrid` 组件（拖拽/缩放/响应式）+ 统一渲染器（table/bar/kpi）。
- ✅ 编辑器 dashboard 分支：多数据集 + 添加图表 tile + 每格选数据集/图表类型 + 全局筛选(ParamBar)+ 下钻。
- ✅ 只读页 / embed 渲染看板；列表新建支持「数据看板」；Data Agent 新增「生成看板」。
- ✅ 发布/版本/对外 API 与现有应用同源。后端测试覆盖创建/组合/发布/对话生成。

**阶段 D2（图表成为可复用资产）— ✅ 已落地**
- ✅ 新增 `DataAppWidget` 表 + CRUD + 预览（抽取通用执行核 `_execute_binding`，与数据集共用）。
- ✅ 图表库面板（`WidgetLibraryModal`：搜索 / 新建 / 加入看板 / 删除）；一图可被多看板复用。
- ✅ 看板 tile 支持引用 `widget_id`（与本地 datasetIndex 兼容）；编辑/只读/embed 均可渲染图表 tile。
- ✅ Data Agent「加入看板」：基于当前回答口径生成可复用图表并追加到选定/新建看板（不重调 LLM）。
- ✅ 后端测试：图表 CRUD/预览/加入看板/跨看板复用/对话生成入板。

**阶段 D2 遗留 / D3（对外与联动增强）**
- 新增 `DataAppWidget` 表 + CRUD + 图表库面板（搜索/跨域）。
- tile 改为引用 `widget_id`；`DatasetEditor` 升级为 Widget 编辑器。
- Data Agent「加入看板」。

**阶段 D3（对外与联动增强）**
- 看板级全局筛选/时间/交叉过滤联动；公开分享链接 + 有效期/口令；主题与移动端自适应。
- 看板/图表模板库；看板级权限与 Cube RLS 打通。

**阶段 D4（治理与规模）**
- Widget/看板独立发版与引用版本锁定；使用统计、血缘（看板→Widget→本体对象/字段）。

---

## 10. 兼容与迁移

- 保留 `data_table` / `screen`；新增 `dashboard`，三者共用发布/版本/预览管线。
- **一次性迁移**：把现有 `screen` app 的 widgets 平移为 dashboard tiles（canvas 区）；
  `data_table` app 转为单列 grid 看板。
- 现有 `/apps/{id}`、`/embed`、`/v1` 端点保持可用；看板走同一套只读渲染。

---

## 11. 技术选型与风险

| 主题 | 选择 | 说明 |
| --- | --- | --- |
| 响应式栅格 | **react-grid-layout**（新增依赖） | 成熟、可拖拽/缩放/断点；canvas 大屏保留 ScreenCanvas |
| 图表渲染 | 现有无依赖 SVG（table/bar/kpi）起步，D3 起可选引入 **ECharts** | 折线/饼/散点/地图等丰富图型时再引 ECharts |
| 跨组件状态 | 看板级 filter/param store（前端） | 后端 runtime_filters 已就绪 |
| 执行/权限 | 复用直连执行器 + Cube（RLS/预聚合） | 对外看板高并发走 Cube 缓存 |

**主要风险**：
- 图表可复用带来的**引用一致性**（Widget 改动影响多看板）→ 用“看板发布锁 Widget 版本”解决。
- 跨域看板的**权限与口径统一**→ 依赖本体语义 + Cube RLS。
- 前端复杂度上升 → 分阶段（D1 先打通多图一板即有对外价值）。

---

## 12. 一句话总结

把「数据集+图表」提升为**可复用的 Widget 资产**，新增 **Dashboard** 作为响应式栅格
组合层，让分别（甚至跨域、跨对话）生成的表格/图表能被**自由拖拽拼装成一个对外看板**，
并复用已具备的发布/版本/参数联动/Cube/RLS/embed 能力对外交付。建议先做 **D1**
（多图一板 + 发布 + 只读页），即可获得“真正对外看板”的核心价值。
