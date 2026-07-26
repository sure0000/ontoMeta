# 数据应用概念重构：Panel / Dashboard 统一模型（Grafana 范式）

> 本文是数据应用的**权威概念定义**，取代此前 `DATA_APP_PLAN.md` / `DASHBOARD_PLAN.md`
> 中「数据表格 / 数据大屏 / 数据看板」三分的模型。
> 参照 **Grafana** 的成熟范式，把数据应用收敛为两个一等概念：**Panel** 与 **Dashboard**。

---

## 0. 一句话结论

不再区分「数据表格 / 数据大屏 / 数据看板」。数据应用只有两层：

- **Panel（面板）** = 最小单位，对应**一个数据逻辑**（一次落地到本体的查询口径），
  以某种可视化形态（表格 / 柱状 / 折线 / 饼 / 指标卡 / …）呈现。
- **Dashboard（仪表盘）** = Panel 的**唯一容器**。Panel 必须在 Dashboard 中展示；
  Dashboard 负责**布局（自由拖拽/缩放）**、**主题风格切换**、**全局筛选联动**、**发布/分享**。

```
Dashboard（仪表盘：一次交付物、布局 + 主题 + 全局筛选）
 └─ panels: [ { panel_id, x, y, w, h, param_overrides } ... ]   ← 自由布局

Panel（面板：最小单位 = 一个数据逻辑）
 ├─ type       表格 / bar / line / pie / kpi / area / scatter / …
 ├─ dataset    口径绑定：对象 / 维度 / 度量 / 时间 / 过滤（落地到本体）
 ├─ viz        可视化编码与样式
 └─ data_source_id   mock / 物理库 / cube
```

---

## 1. 为什么要重构

现状（`DATA_APP_PLAN.md` + `DASHBOARD_PLAN.md` 已落地）存在三个概念并存：

| 旧概念 | 本质 | 问题 |
| --- | --- | --- |
| `data_table` app | 表格页（多数据集 Tab） | 只是「表格形态的容器」，与 dashboard 重叠 |
| `screen` app | 像素级大屏画布 | 只是「另一种布局模式」，不该是独立 app 类型 |
| `dashboard` app | 响应式栅格 + Widget 引用 | 才是真正的「容器」 |
| `DataAppWidget` | 可复用图表资产 | 命名与 Grafana「Panel」等价，但概念未收敛 |

**三种 app_type 本质是「同一个容器的不同布局/展现」**，却被建模成三种应用，导致：
概念冗余、编辑器分叉、Data Agent 要区分「生成表格 / 生成大屏 / 生成看板」、
对外渲染要三套分支。参照 Grafana 收敛后，**只剩「往 Dashboard 里放 Panel」一条主线**。

---

## 2. 概念映射（旧 → 新）

| 旧模型 | 新模型（Grafana 范式） | 说明 |
| --- | --- | --- |
| `DataApp(app_type=dashboard)` | **Dashboard** | 唯一容器，保留 |
| `DataApp(app_type=data_table)` | Dashboard（单 Panel / 全宽表格布局） | 迁移为一个含表格 Panel 的看板 |
| `DataApp(app_type=screen)` | Dashboard（`layout=canvas` 像素模式） | 布局模式之一，非独立类型 |
| `DataAppWidget` | **Panel（可复用）** | 直接改名/收敛为 Panel 资产 |
| `DataAppDataset`（app 内数据集） | Panel 内联的 `dataset` 绑定 | Panel 自带一个数据逻辑绑定 |
| `spec.tiles[].datasetIndex` | `spec.panels[].panel_id` 引用 | 一律以 Panel 引用组板，弃用局部数据集索引 |

> **数据逻辑 = 一个 Panel = 一次落地本体的查询绑定**。这是「最小单位」的准确定义，
> 与现有 `binding_json`（measures/dimensions/filters/time_range，元素引用本体对象/字段/业务逻辑）
> 完全一致——沿用现有 grounding 与溯源，不改口径引擎。

---

## 3. 数据模型（目标态）

### 3.1 Panel（面板，最小单位）— 由 `DataAppWidget` 收敛

```
Panel
├─ id, domain_id, ontology_id
├─ name, description
├─ type          # table | bar | line | pie | kpi | area | scatter
├─ dataset:      # 一个数据逻辑（落地本体）
│   ├─ primary_object_type_id
│   ├─ binding_json     # measures / dimensions / filters / time_range（本体引用）
│   ├─ compiled_sql     # 由 binding 确定性编译，评审可见
│   └─ data_source_id   # mock / 物理库 / cube
├─ viz_json       # x/y/series/format/legend/options
├─ status         # draft | published
├─ source         # chat_generated | manual
└─ createdBy, updatedAt
```

- **物理表**：沿用 `data_app_widgets`（无需改表名，语义即 Panel）。
- **可复用**：一个 Panel 可被多个 Dashboard 引用（多对多）。
- **可独立预览**：Panel 单独可预览/被图表库检索（复用现有 widget 预览管线）。

### 3.2 Dashboard（仪表盘，唯一容器）— 由 `DataApp` 收敛

```
Dashboard   # data_apps 表，app_type 收敛为单一值 'dashboard'
├─ id, domain_id, ontology_id
├─ name, description, owner
├─ status / source / version / published_* / public_*   # 全部保留
└─ spec_json:
    {
      "layout": "grid",          # grid（响应式栅格，默认）| canvas（像素大屏区）| mixed
      "grid":  { "cols": 12, "rowHeight": 40, "gap": 12, "breakpoints": {...} },
      "theme": { "preset": "light", ... },   # 主题风格，见 §5
      "filters": [ { "id":"f1", "label":"渠道", "column":"channel", "op":"eq" } ],
      "panels": [
        { "panel_id": "p_abc", "x":0, "y":0, "w":6, "h":6,
          "title": "各渠道订单量", "param_bindings": { "f1": "channel" } },
        { "panel_id": "p_def", "x":6, "y":0, "w":6, "h":6 }
      ]
    }
```

- `app_type` 字段**保留但只取 `dashboard`**（避免破坏性 schema 变更；旧值一次性迁移）。
- `spec.tiles` → `spec.panels`；`tile.widget_id` → `panel.panel_id`（渲染器兼容读旧字段）。
- **像素大屏**不再是 `screen` app，而是 Dashboard 的 `layout=canvas`（或 mixed 中的 canvas 区）。

### 3.3 版本 / 发布 / 分享（不变）

- `DataAppVersion` 快照 `spec.panels` + 每个引用 Panel 的绑定/编译SQL/viz（发布即冻结，图表后续编辑不影响已发布看板）。
- 公开分享 `public_token` / embed / `/v1` 对外 API 全部保留，只是渲染统一走「Dashboard + Panel」。

---

## 4. 自由布局（Panel 在 Dashboard 中的自由调整）

- **grid 模式（默认，对外看板）**：`react-grid-layout` 响应式栅格，Panel 支持拖拽换位、
  边角缩放、断点自适应（lg/md/sm/xs）。已具备（`DashboardGrid.tsx`），把 `tiles`→`panels`。
- **canvas 模式（像素大屏）**：绝对定位 + 等比缩放（1920×1080 基准），复用现有 `ScreenCanvas`
  作为 Dashboard 的一种布局区。
- **mixed**：同一 Dashboard 内普通区用 grid、展示大屏区用 canvas。
- 布局仅存于 `spec.panels[].{x,y,w,h}`（+ canvas 的绝对 rect），**不改 Panel 定义**——
  同一 Panel 在不同看板可有不同尺寸/位置。

---

## 5. 主题风格切换（Dashboard 级）

主题是 **Dashboard 属性**（`spec.theme`），对该看板内所有 Panel 生效：

```jsonc
"theme": {
  "preset": "light",        // light | dark | 具体主题名（可扩展主题市场）
  "bg": "#f5f7fa",          // 背景
  "accent": "#2563eb",      // 主强调色（图表主色/交互色）
  "panelBg": "#ffffff",     // 面板底色
  "text": "#1f2937",
  "palette": ["#2563eb", "#16a34a", "#f59e0b", ...],  // 图表配色序列
  "radius": 8, "density": "comfortable"               // 圆角/密度
}
```

- 提供**内置预设**：`light` / `dark`（起步），后续可加行业主题（大屏深色科技风等）。
- 前端：主题经 Context/CSS 变量下发，`<DataAppRenderer>` 与 grid/canvas 均读同一套变量；
  切换主题**即时预览**，随 `spec.theme` 持久化，发布/embed/public 只读页一致生效。
- Panel 层可选 `viz.overrideTheme`（个别面板覆盖），默认继承 Dashboard 主题。

---

## 6. 编辑器（统一为一条主线）

不再有「表格编辑器 / 大屏编辑器 / 看板编辑器」之分，只有 **Dashboard 编辑器**：

- **左：Panel 库**——列出可复用 Panel（搜索、按域/类型过滤、跨数据域开关），拖入画布。
  入口：「+ 新建 Panel」「+ 从 Data Agent 生成」。
- **中：看板画布**——grid（自由拖拽/缩放）/ canvas（像素）/ mixed；每格用 `<DataAppRenderer>` 渲染 Panel。
- **右：属性面板**——
  - Dashboard 级：**主题切换**、布局模式与列数、全局筛选。
  - Panel（tile）级：标题、尺寸、类型切换、参数映射。
- **新建 Panel** = 现有 Widget 编辑器（绑定本体对象/维度/度量/时间/过滤 + 选类型 + 样式）。

---

## 7. Data Agent 集成（收敛为一个动作）

对话回答下方**不再三个按钮**（生成表格 / 生成大屏 / 生成看板），统一为：

- **「生成 Panel 并加入看板」**：用当前回答已展示的口径（不重调 LLM）生成一个 Panel，
  追加到「新建 / 选定」的 Dashboard；类型可在生成后一键切换（表格↔柱↔折线…）。
- **「看板内问数」**：在 Dashboard 编辑器里直接问 → 生成 Panel 落到当前看板空位。

效果：业务用自然语言逐个问出面板，边问边拼成一个可对外的看板。

---

## 8. API 变更（最小化、向后兼容）

- CRUD 路径不变（`/api/data-apps`、`/api/data-app-widgets`）；语义上 widget=Panel、app=Dashboard。
- 新建 Dashboard 一律 `app_type=dashboard`；不再暴露 `data_table` / `screen` 创建入口。
- Data Agent：`/api/chat-bi/generate-app` 收敛为「生成 Panel + 入板」（保留兼容参数）。
- 对外 `/v1/data-apps/{id}` 与 public 返回 `panels`（同时兼容返回旧 `tiles` 一段时间）。
- 主题：随 `spec.theme` 存取，无需新端点。

---

## 9. 迁移（一次性，向后兼容）

1. **数据迁移脚本**（Alembic data migration 或后端一次性任务）：
   - `app_type=data_table` → `dashboard`，spec 生成单个全宽表格 Panel 引用。
   - `app_type=screen` → `dashboard` + `spec.layout=canvas`，widgets 平移为 canvas panels。
   - `app_type=dashboard` → `spec.tiles`→`spec.panels`、`widget_id`→`panel_id`。
2. **渲染兼容层**：`<DashboardGrid>` / 只读页同时读 `panels`（新）与 `tiles`（旧），
   逐步下线旧字段。
3. **DataAppWidget** 无需改表；文档/UI 文案统一改称 **Panel（面板）**。
4. 旧端点保持可用；新建入口只留 Dashboard。

---

## 10. 分阶段落地

> **落地状态（本次提交）**：G1 ✅ / G2 ✅ 已实现；G3 🟡 迁移与旧字段清理待续；G4 ⛔ 延期。

**G1（概念收敛，无破坏）— ✅ 已实现**
- ✅ 新建入口只留「数据看板」（`DataAppsPage` 创建仅 `app_type=dashboard`；旧 data_table/screen 列表兼容展示为「（旧）」）。
- ✅ UI 文案统一为 **面板（Panel）**：面板库 / 添加面板 / 生成面板（`WidgetLibraryModal`、`DashboardGrid`、编辑器、血缘）。
- ✅ Data Agent 三按钮（生成表格/大屏/看板）收敛为 **「生成面板并加入看板」+「生成新看板」**（`ChatBiReferences`）。

**G2（布局 + 主题一等化）— ✅ 已实现**
- ✅ 主题预设系统 `dashboardThemes.ts`（light/dark/科技蓝/清新绿/雅致紫），编辑器主题**下拉切换**。
- ✅ 主题经 CSS 变量（accent→`--om-primary`、面板底色/文字/边框）下发；`DashboardGrid` 统一解析，
  **编辑器 / 只读页 / embed / public 一致生效**，图表/表格随主题变色。
- ✅ 自由拖拽/缩放/断点布局沿用 `react-grid-layout`（`DashboardGrid`）。

**G3（迁移 + 清理）— ✅ 已实现（含 screen 并入 canvas）**
- ✅ 存储字段改名（后端）：`spec.tiles`→`spec.panels`、面板内 `widget_id`→`panel_id`；
  服务层 `_spec_panels`/`_panel_ref_id`/`_set_spec_panels` 兼容读旧写新（publish/render/lineage/add_widget/生成均已切换）。
- ✅ 前端 `getSpecPanels`/`getPanelRefId` 兼容层：编辑器/只读页/embed/public 统一读 `panels`（回退 `tiles`）、写 `panels` 并清理 `tiles`。
- ✅ **screen 彻底并入 Dashboard canvas 布局**：`layout: grid | canvas` 两种模式共一套 `panels`；
  编辑器顶部「栅格 / 大屏画布」切换（`changeLayout`），canvas 复用 `ScreenCanvas`（拖拽/缩放/属性面板），
  grid 复用 `DashboardGrid`；只读页/embed/public 按 `layout` 渲染 canvas（像素等比）或 grid。
- ✅ Alembic 迁移：`d8e9f0a1b2c3`（`tiles`→`panels`、`data_table`→dashboard）+ `e9f0a1b2c3d4`（`screen`→dashboard/`layout=canvas`、`widgets`→`panels`）；发布快照同步；已验证上/下行。
- ✅ 前端编辑器不再区分表格/大屏/看板三类，统一为「看板 + 面板（grid/canvas）」一条主线（旧 data_table 的 Tabs 编辑分支已移除）。
- 🟡 后端 `APP_TYPES` 仍保留 `data_table`/`screen`（API 向后兼容 + 现有测试），但前端仅创建 `dashboard`；存量已迁移。

**G4（增强，沿用现有）— ⛔ 延期**
- 主题市场；Panel 独立发版与引用版本锁定；血缘增强。

---

## 11. 与现有能力对齐（全部复用，不重造）

| 现有机制 | 新模型如何复用 |
| --- | --- |
| `binding_json` / grounding / `_ReferenceResolver` | Panel 的「一个数据逻辑」= 落地本体的绑定，无命中拒存 |
| Binding Compiler / 只读执行器 / Cube | Panel 数据执行与口径一致性，完全不变 |
| `react-grid-layout` / `ScreenCanvas` | Dashboard 自由布局（grid/canvas/mixed） |
| `DataAppVersion` / ChangeConfirmation | Dashboard 发布冻结 Panel 快照，二次确认 |
| public_token / embed / `/v1` + `dataapps:read` | 对外只读统一渲染 Dashboard + Panel |
| Cube RLS securityContext | 对外看板行级权限，不变 |

---

## 12. 一句话总结

**数据应用 = Dashboard（唯一容器）+ Panel（最小单位，一个数据逻辑）**。
取消「表格 / 大屏 / 看板」三分，Panel 必须在 Dashboard 中展示，支持在看板内**自由拖拽布局**
与 **Dashboard 级主题切换**，其余（本体绑定、口径编译、执行、发布、版本、对外分享、Cube/RLS）
全部复用既有能力。
