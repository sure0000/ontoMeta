# 本体建模模块：以「快速审核」为核心的界面重排

> 前置文档：[ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md](./ONTOLOGY_SEGMENT_AND_BROWSE_REDESIGN.md)（§5 已论证「浏览与审核是两个任务」）。
> 本文接着那一条往下做：把审核从「浏览界面上的一个开关」变成一条**能被清空的队列**。

---

## 0. 结论先行

本体建模模块的产出不是「一个能浏览的本体」，而是**一批被人判过的语义**。衡量它的指标只有一个：
**单位时间判定数**，以及**队列清空所需的时间**。

当前界面是按「管理后台（列表 + 详情表单）」建的，审核是后来用一个 `Segmented` 开关挂上去的
（[DomainDetailPage.tsx:293](../frontend/src/pages/DomainDetailPage.tsx:293)）。结果是三件事同时不成立：

1. **判据不在判定的地方**——判定依据（`role_signals` / `role_reason`）只在详情页的第 3 个 Tab
   （[ObjectTypeDetailPage.tsx:1144](../frontend/src/pages/ObjectTypeDetailPage.tsx:1144)），列表里只有一个 hover tooltip
   （[OntologyWorkspaceView.tsx:723](../frontend/src/components/OntologyWorkspaceView.tsx:723)）。
2. **队列不是队列**——服务端按 `updated_at DESC` 排序（[ontology_query.py:396](../backend/app/services/ontology_query.py:396)），
   前端号称的「同板块＋同命名族聚堆」只对**当前这 20 条**重排（[OntologyWorkspaceView.tsx:241](../frontend/src/components/OntologyWorkspaceView.tsx:241)）。
3. **一次判定的代价太高**——见 §1，判一个对象要 ≥6 次交互、2 次整页加载，且返回后**审核态全丢**。

**主张**：审核不该是「浏览页的一个模式」，而是一个**独立的审核工作台**，工作单元从「一个对象」
升到「**一组同类对象**」，判据、判定、下一个三件事在同一屏内完成。

**只做一件事的话**：把队列的排序与分组挪到服务端（§4.1），再让一屏判一组（§4.2）。
其余都是围绕这两条的收尾。

---

## 1. 判定成本核算：现在判一个对象要付什么

以「待复核对象 → 确认为业务对象」这条最常见的路径计：

| # | 动作 | 代价 |
| --- | --- | --- |
| 1 | 在卡片墙上点一张卡 | 整页跳转，详情页重新拉取 |
| 2 | 切到「判定依据」Tab 看判据 | 1 次点击 |
| 3 | 切回「基本信息」Tab | 1 次点击 |
| 4 | 展开「复核状态」下拉、选「已确认」 | 2 次点击 |
| 5 | 点「保存」 | 1 次点击 + 1 次写请求 |
| 6 | 浏览器返回 | 整页跳转 |
| 7 | **重新进入审核模式**（`reviewMode` 是组件 state，无 URL 同步） | 1 次点击 |
| 8 | **重新选板块、重新翻到第 N 页** | 2+ 次点击 |

**≥6 次交互 + 2 次整页加载 + 上下文丢失**。按 866 个待复核对象（前置文档实测值）估算，
保守 20–30 秒/个 → **5–7 小时不间断操作**。

页内的「批量修改」路径更快但**判据全无**：进入批量态 → 勾选 → 选「设为已确认」→ 应用
（4 次点击），全程只能靠 hover tooltip 看 `role_reason`——等于闭眼判。
中间那档「**看着判据、成批判**」，今天不存在。

---

## 2. 六条硬伤（按对判定速度的伤害排序）

### R1 队列的排序键是 `updated_at`，且会边判边变

- 服务端恒定 `ORDER BY updated_at DESC`（[ontology_query.py:396](../backend/app/services/ontology_query.py:396)），
  没有 `order_by` 参数。
- 前端的审核排序 `byReviewOrder`（板块 → 命名族 → 名称）只作用于**服务端已经挑好的这一页**
  （[OntologyWorkspaceView.tsx:236-241](../frontend/src/components/OntologyWorkspaceView.tsx:236)）。
  跨页的「聚堆」从未发生。
- 更糟的是筛选条件（`needs_review=true`）会**被判定动作本身改变**：判掉的行离开结果集、
  后面的行整体前移。停在第 3 页判完一批再翻页，中间会**静默跳过一整页**。没有游标、
  没有「已看过」标记，也没有可恢复的进度。

### R2 判据与判定不在同一屏

- `ObjectTypeSummary` 不带 `role_signals`，也不带 `row_count`
  （[schemas/ontology.py:390-421](../backend/app/schemas/ontology.py:390)），而这两样正是判据。
- 数据库里其实**已经存好了**：`ObjectType.role_signals` 的列注释写明「供复核界面展示『判定依据』」
  （[models/ontology.py](../backend/app/models/ontology.py)），前端也已有成熟的渲染器
  `describeSignals()`（[utils/role.ts:84](../frontend/src/utils/role.ts:84)）。
  **缺的只是把它放进列表接口**。
- 判据既然只在 tooltip 里，就无法扫读、无法排序、无法横向比较——而横向比较正是成批判定的前提。

### R3 批量只有「一个一个点」和「866 个一把梭」两档

- 选择集只在当前页有效（[OntologyWorkspaceView.tsx:296](../frontend/src/components/OntologyWorkspaceView.tsx:296)）；
  「全选符合条件」一次把整个筛选集选中（[DomainDetailPage.tsx:1012](../frontend/src/pages/DomainDetailPage.tsx:1012)）——
  那不是审核，是放弃审核。
- 应用后 `reloadBundle()` 整包重取并退出批量态（[DomainDetailPage.tsx:1010](../frontend/src/pages/DomainDetailPage.tsx:1010)），
  下一批要从头再来一遍「进入批量 → 勾选 → 选下拉 → 应用」。
- **没有撤销**。批量误判 200 个对象，只能再手工反向选一次。

### R4 关系侧根本没有审核面

- `relation_types.needs_review` 列已经加了（[models/ontology.py:298](../backend/app/models/ontology.py:298)），
  但审核模式下的「业务关系」Tab 走的是 `RelationGroupList`，它**不接受 `needs_review` 过滤**
  （[RelationGroupList.tsx:79](../frontend/src/components/RelationGroupList.tsx:79)），
  没有行内确认、没有批量端点。审核模式与浏览模式下的关系页**完全一样**。
- `VerbRefinementPanel` 唯一的动作是「提交全部建议」，且提交后把关系标成 `needs_review=True`
  （[api/ontology.py:1077](../backend/app/api/ontology.py:1077)）——**净增审核债**，还不能逐条取舍。

### R5 进度面板不动，而且统计口径漏人

- `reviewStats` 只依赖 `working_ontology_id`（[DomainDetailPage.tsx:365](../frontend/src/pages/DomainDetailPage.tsx:365)），
  批量应用后不重取 → **判完一批，进度条纹丝不动**，要刷新整页才更新。审核最重要的正反馈就这么没了。
- `get_review_mode_stats` 只统计 `table_role == "business_object"`
  （[ontology_query.py:1779](../backend/app/services/ontology_query.py:1779)），
  而「桥表未能塌缩 → 智能重判为 data_table/technical」的那批对象**全部 `needs_review=True`**
  （[evidence_builder.py:437-478](../backend/app/services/evidence_builder.py:437)）。
  它们既不在进度分母里，也不在默认队列里（审核模式恒把 `typeFilter` 设为 `business_object`，
  [DomainDetailPage.tsx:948](../frontend/src/pages/DomainDetailPage.tsx:948)）——**一批待判对象事实上不可见**。

### R6 上下文一进就丢

- 审核模式、板块筛选、页码、批量选择集**全是组件 state，零 URL 同步**。刷新或返回即回到「浏览 · 地图 · 第 1 页」。
- 从工作区地图钻进板块会跳到全局路由 `/segments/:id`，而该页的「返回工作区」把 `ontology_id`
  塞进了 `:domainId` 位（[SegmentDetailPage.tsx:114](../frontend/src/pages/SegmentDetailPage.tsx:114)）——
  路由是 `/workspace/:domainId`（[App.tsx:34](../frontend/src/App.tsx:34)），**这是一条断链**。
- 全模块只有图视图注册过键盘（[OntologyGraphView.tsx:168](../frontend/src/components/graph/OntologyGraphView.tsx:168)）。
  重复性判定却只能用鼠标。

---

## 3. 三条原则

1. **判据、判定、下一个，必须在同一屏一次操作内闭合**。任何需要「跳过去看一眼再跳回来」的设计，
   在 866 的量级上都不成立。
2. **工作单元是「一组同类对象」，不是一个对象**。`role_confidence` 中位数 0.5、87% 挤在 0.5–0.7
   （前置文档实测），置信度排序救不了场；能救场的只有**相似度聚堆 + 成组裁决**。
3. **判定必须可逆、可恢复**。撤销、以及刷新后回到原位，是敢于快速判定的前提。

---

## 4. 目标形态：审核工作台

新路由 `/workspace/:domainId/review`，从工作区顶部的「N 个待复核」直接进入；
**浏览页保持现状不变**，不再用一个开关在同一页切换两种任务
（现在切换还会顺手把地图换成清单、改掉过滤器，视觉与状态全变）。

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ 审核 · ERP 域        进度 132/866 ▓▓▓░░░░░░░      [撤销 ⌘Z]   [完成并发布]        │
├──────────────┬──────────────────────────────────────────┬──────────────────────┤
│ 队列(240)     │ 当前组                                    │ 判据(320)             │
│              │                                          │                      │
│ 采购  12/40  │ 采购 · tabPurchase* · 12 张               │ 综合得分 2.4 (≥2.0)   │
│ 销售   0/38  │ 建议：业务对象                             │ 主键列数    1    ↑    │
│ 库存   5/22  │ ┌──┬────────┬──┬──┬──┬─────┬────┐        │ 外键入度    7    ↑    │
│ …            │ │☑ │采购订单 │1 │7 │32│1.2万│→3  │        │ 字段数     32    ↑    │
│ ─────────    │ │☑ │采购申请 │1 │3 │28│8千  │→2  │        │ 技术字段占比 9%  ↑    │
│ 未分板块 128 │ │☐ │采购日志 │0 │0 │9 │90万 │—   │← 例外  │ 板块成员数   40  ↑    │
│ 数据表   96  │ └──┴────────┴──┴──┴──┴─────┴────┘        │ ──────────────       │
│ 关系   1288  │  ↑名称  主键 入度 字段 行数 邻居            │ 判定说明（分条）      │
│              │                                          │ 字段构成（前 8）      │
│              │ [A 全组确认] [D 改判数据表] [T 技术表] [S 跳过]│ 同组已判：11 通过     │
│              │                                          │ [打开完整档案 ⏎]      │
└──────────────┴──────────────────────────────────────────┴──────────────────────┘
```

### 4.1 队列的定义（这是整个方案的地基）

新接口 `GET /api/ontologies/{id}/review-queue?kind=object|relation&cursor=&limit=`：

- **范围**：`needs_review = true` 的**全部**对象，不限角色（修 R5 的漏人）。
- **分组键**：`(segment_id, 建议角色, 命名族, 得分带)`。命名族的规则收归后端一处实现
  （前端 `reviewFamily` 那套 `split(/[_\-\s]/)[0]` 删掉，避免两处各说一套）。
- **排序**：组间按（板块待判数 desc, 组大小 desc）；组内按（得分 desc, 名称 asc, id asc）。
  **与 `updated_at` 彻底脱钩**——判定动作不再扰动队列顺序（修 R1）。
- **游标**：keyset 分页（`(排序键, id)` 之后），不是 `offset`。判掉的成员不会让后面的行前移。
- **载荷**：每个成员带 `role_signals` 精简版 + `row_count` + `top_neighbors`，
  让中栏表格的每一列都是判据（修 R2）。

配套：`ObjectTypeSummary` 增补 `role_signals`（或精简的 `review_signals`）与 `row_count`
——两者数据库里都已有列，只是没往外发。

### 4.2 一屏判一组

- **组内默认全选**，例外靠反选。同板块＋同命名族的表大概率同判，默认全选把「多数」变成 0 成本。
- **表格列即判据**，且可排序：主键列数 / 外键入度 / 字段数 / 行数 / 邻居数。
  异常值一排序就浮到顶部——「0 主键、0 入度、90 万行」一眼就是日志表，不用读任何散文。
- **一次 apply = 一次批量 PATCH**：组消失、进度条即时前进（乐观更新 + 后台校正）、
  下一组自动聚焦，**左栏不重排**（保住空间记忆）。
- 判完的板块在左栏折叠成「✓ 采购 40/40」，位置不变。

### 4.3 判据栏

直接复用现有的 `DecisionEvidencePanel` 与 `describeSignals()`
（[ObjectTypeDetailPage.tsx:111](../frontend/src/pages/ObjectTypeDetailPage.tsx:111)），不重写。
底部加两样今天没有的东西：**字段构成预览**（前 8 个字段名＋语义类型）与
**同组已判结果**（「本组已确认 11 个为业务对象」——让判定自我加强）。
「打开完整档案」以 Drawer 承载，**不跳页**。

### 4.4 键盘

| 键 | 动作 | 键 | 动作 |
| --- | --- | --- | --- |
| `J` / `K` | 上/下一行 | `A` | 采纳建议角色（整组或选中行） |
| `X` | 切换选中 | `1`–`4` | 改判 业务/数据表/关系表/技术表 |
| `⏎` | 展开完整档案 | `S` | 跳过（保持待复核，移到队尾） |
| `⌘Z` | 撤销上一次批量 | `⇧⏎` | 应用并跳到下一组 |

输入框聚焦时全部失效。

### 4.5 撤销

批量应用前，前端记录每个 id 的 `{table_role, needs_review}` 原值；撤销 = 反向批量
（通常 1–2 次请求）。10 秒 toast + `⌘Z`。**不需要后端改动**——现有 batch 端点即可反向调用。

---

## 5. 关系审核：同一套交互，对称补齐

关系的天然判定单元就是**去重组**——`RelationGroupList` 已经按 `display_name` 聚合并带
`needs_review_count` 与 `target_groups`（[types.ts:454](../frontend/src/types.ts:454)）。所以关系审核不需要新交互，
只需要把对象那套搬过来：

1. `listRelationGroups` 接 `needs_review` 过滤（修 R4）。
2. 新增 `PATCH /api/relation-types/batch`，与对象批量端点对称（今天只有对象有，
   [api/ontology.py:583](../backend/app/api/ontology.py:583)）。
3. **动词细化并入队列，不再单开面板**：一条 suggestion = 组内一行的「改名提案」，
   接受即 `needs_review=False`（而不是今天的 `True`）。这把 `VerbRefinementPanel`
   从「提建议、造债」改成「一次审核动作」。「提交全部」保留，但走同一个确认面板。

---

## 6. 落地顺序（P0–P2 已实施，2026-09-01）

### P0 · 让队列成立（后端为主）✅

- [x] `GET /ontologies/{id}/review-queue`：分组 + 确定性排序 + 可重放游标；范围覆盖全部角色。
- [x] `ObjectTypeSummary` 增补 `role_signals` / `row_count`（Agent 侧由 `_VERBOSE_KEYS` 先行丢弃）。
- [x] 浏览与审核状态进 URL（`view` / `tab` / `q` / `page` / `segment` / `cursor` / `kind`）。
- [x] §8 的四个 bug。

### P1 · 审核工作台（前端为主）✅

- [x] `/workspace/:domainId/review` 三栏页（`ReviewWorkbenchPage`）。
- [x] 成组判定 + 默认全选 + 反选例外 + 乐观进度 + 判完自动聚焦下一组。
- [x] 键盘（A / 1–4 / S / ⌘Z）与撤销（前端记原值反向批量，后端无需改动）。
- [x] 「全选符合条件」直接删除——成组判定取代了它；浏览页的批量保持按页作用域。

### P2 · 关系侧对称 + 收口 ✅

- [x] `PATCH /relation-types/batch` 与 `review-queue?kind=relation`（按「板块 × 动词 × 结构」成组）。
- [x] 动词细化改成逐条可取舍的抽屉，**采纳即已复核**（此前采纳后标回待复核，净增审核债）。
- [x] 完整档案改 Drawer（`ObjectArchiveDrawer`），看细节不离开队列。

### 实施中偏离方案的三处（都有实测依据）

1. **组间排序不按「板块待判数」**。那个数每判一个就变，某个板块判到一半会被另一个顶到
   前面，游标随之漂移。改成只用不随判定变化的量（板块名 / 命名族 / 角色 / 判定强度）；
   「哪个板块还剩多少」交给左栏的进度地形表达。有用例钉住。
2. **长尾必须并桶**（`MIN_FAMILY_SIZE = 3`）。真实库里大量表各叫各名（MySQL 系统表
   db/func/proc…），纯按命名族分组在 erpnext 866 个待复核上得到 **460 组、328 个单成员组**
   ——那等于退回逐个判。小于 3 的族并入同板块同角色同强度的「零散表」桶后是 **99 组、
   中位 4 个/组**。关系侧同理：1288 条 → **75 组**。
3. **关系的复核债需要一次回填**。`relation_types.needs_review` 加了列却从没人写，
   于是「1288 条从没人看过的机器关系」在库里显示为「全部已复核」，关系队列恒空。
   迁移 `37274306e992` 把 `origin=machine AND status=suggested` 的关系置为待复核；
   生成侧也补上（新建机器关系即待复核，再生成不回写人工确认）。
   不影响发布——关系发布只看两端是否都是已确认业务对象。

### 验收指标

| 指标 | 改造前 | 改造后（实测/实现） |
| --- | --- | --- |
| 一次判定的交互次数 | ≥6 + 2 次整页加载 | 1 次按键（A），例外多一次反选 |
| 刷新或返回后 | 回到「浏览·地图·第 1 页」 | 板块/游标/队列类型都在 URL 里，原位恢复 |
| 进度条更新 | 需刷新整页 | 每次 apply 后即时（队列与进度一起重取） |
| 误判后 | 只能手工反向再选一次 | ⌘Z 撤销（保留最近 10 次） |
| 866 个对象的判定单元 | 866 次 / 44 页 | **99 组**（中位 4 个/组） |
| 1288 条关系的判定单元 | 无审核面 | **75 组** |

---

## 7. 刻意不做的事

- **不再加模式开关**。现在「浏览/审核」与「地图/清单」两层开关叠加，进审核还强制换视图。
  审核独立成页，浏览页一个字不改。
- **不建第二套数据源**。队列接口是既有 `list_object_types` 的一个排序/分组分支，不是新模型。
- **不引新图库、不改发布流程、不改任何既有字段语义**（沿用前置文档的约束）。
- **不做「AI 自动全判」**。置信度中位数 0.5、87% 挤在 0.5–0.7，没有可用信息量；
  自动判定只会把审核债转成上线后的错。
- **不删卡片墙**。它对「我知道名字」的检索仍然最快，只是不再承担审核。

---

## 8. 顺手要修的四个真 bug（已修）

| # | 位置 | 症状 |
| --- | --- | --- |
| 1 | [DomainDetailPage.tsx:365](../frontend/src/pages/DomainDetailPage.tsx:365) | `reviewStats` 只依赖 `working_ontology_id`，批量判完后进度条不更新，需刷新整页 |
| 2 | [ObjectTypeDetailPage.tsx:113](../frontend/src/pages/ObjectTypeDetailPage.tsx:113) | 「判定依据」面板用 `isNeedsReview(role_reason)` 判复核态，而真源早已是 `needs_review` 列且后端不再写 `[待复核]` 前缀 → 同一页里「基本信息」显示待复核、「判定依据」显示已确认。连带删掉 [utils/role.ts:46](../frontend/src/utils/role.ts:46) 的 `isNeedsReview` 与其过期注释 |
| 3 | [SegmentDetailPage.tsx:114](../frontend/src/pages/SegmentDetailPage.tsx:114) | 「返回工作区」把 `ontology_id` 传进 `/workspace/:domainId` → 断链，钻进板块后回不去 |
| 4 | [DomainDetailPage.tsx:948](../frontend/src/pages/DomainDetailPage.tsx:948) | `setTypeFilter(next === "review" ? ["business_object"] : ["business_object"])` 两分支相同；而这行正是 R5「非业务对象待复核不可见」的成因 |


---

## 9. 实施后补记：一个更深的 bug

改造过程中发现 `ontology_query._loads_json()` 的函数体被另一个函数（`_loads`）**插进了中间**，
`if not value: return None` 之后直接落到下一个 `def`——于是它**永远返回 None**。

后果比看起来大：`role_signals`（判定证据）与板块的 `conflict_json` 从来没有被解析出来过。
对象详情页那句「role_signals 为空（存量未重生成）时优雅降级」的注释，说的其实不是存量数据问题，
而是这个静默失效——**判定依据面板从未显示过结构化信号**。修好后，审核工作台右栏的
10 条信号（主键列数 / 外键入度 / 技术字段占比 / 图连通性…）在真实 erpnext 本体上全部出得来。


---

## 10. 界面实现补记：三类"看不见的失败"

两处都通过了构建、类型检查与 lint，但界面是坏的——它们的共同点是**失败方式是静默的**。

### 10.1 写错设计 token = 整页没有样式

审核工作台第一版整页用的是 `--om-color-bg-container` / `--om-color-border` /
`--om-color-text-tertiary` 这类名字——本仓根本没有这些 token（真名是 `--om-surface` /
`--om-border` / `--om-text-tertiary`）。CSS 对未定义变量的处理是**静默丢弃整条声明**：
所有面板因此没有背景、没有边框、没有圆角，页面看起来"没有样式"。

抄错的源头是已被删除的 `ReviewModePanel`，同一批代码里 `OntologyOverviewPanel`、
`RelationTriples`、`SegmentsPage`、`ObjectLanding`、`chat-bi.css` 也各中了一处。

已加机器检查：`npm run lint:tokens`（`frontend/scripts/check-tokens.mjs`）扫出所有
`var(--om-*)` 里未定义的名字。这类错误人眼只会读成"怎么有点丑"，只有机器扫得出来。

### 10.2 分组只在"待判"那一半上做 = 键会漂

"本组已判 N 个"最初想在前端用 `size - members.length` 算——算不出来，判过的成员
根本不在队列载荷里。改成服务端给，又暴露了更深的问题：**分组必须在"待判 + 已判"的
完整人口上做一次**。否则长尾并桶的阈值看的是族有多大，族会随着判定不断缩水、掉进
零散桶，键跟着变，游标就又开始漂——和"按待判数排序"是同一个坑的另一个入口。
现在两者都有用例钉住。

### 10.3 一次事件里改两个查询参数 = 后一个吃掉前一个

「点板块没有切换」的真因不在点击，在 URL 写入：react-router 的 `setSearchParams`
每次都从**它自己记住的那份快照**算起，而快照要等导航生效后才更新。于是

```
setSegmentFilter(seg.id);   // 写进 ?segment=…
setCursor("");              // 基于不含 segment 的旧快照重算 → 把它抹掉
```

第二次写把第一次的键覆盖没了。同一个坑还埋在另外两处：**对象/关系切换**
（`kind` + `cursor`）和**分页改每页条数**（`page` + `size`，页码会被悄悄清掉）。

修在 `useUrlState` 内部：同一 tick 内的写入叠加在前一次结果上，微任务结束丢弃缓冲，
下一 tick 重新以路由为准。调用方不必改写法，连着调两个 setter 现在是安全的。

### 10.4 为审核速度做的界面决定（都不是装饰）

| 决定 | 服务于什么 |
| --- | --- |
| 数字列等宽 + 异常值染色（0 主键 / 0 入度 / ≥10 万行） | 扫一列比读一行快；例外不排序也会自己跳出来 |
| 组头给出组内信号跨度（主键 1 · 入度 0–7 · 行数 0–1.2万） | 全组同值就不必逐行看，有跨度才去找例外 |
| 键位印在按钮上（`A` `1`–`4` `S`） | 提速主要靠键盘，而键盘只有被看见才会被用 |
| 判定条常驻底部、表头吸顶 | 判到第 100 行不用滚回去找按钮，也不会忘了哪列是哪列 |
| 反选行压暗但**留在原位**（名字加删除线） | 一勾就消失 = 看不见自己排除了什么 |
| 判完的板块留在原位折叠成 ✓ | 空间记忆是提速的一部分，重排会把它清零 |
| "本组已判 N 个"绿条 | 判定自我加强：同族前面都确认了，后面多半一样 |
| 三栏各自滚动、页面本身不滚 | 判据、判定、下一个三件事同时在屏幕上 |
