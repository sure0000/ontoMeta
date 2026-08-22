# 本体生命周期重构方案：预生成 / 二次生成 / 人工修订的闭环

> 状态：**已实施** · 2026-08-21
> 范围：数据域预生成、再生成（二次草稿）、人工修订与发布的**使用逻辑**闭环
> 前提：**不考虑历史数据**（可加唯一约束、可改列语义、可删接口）

---

## 0. 结论先行

**诊断：** 三方合并（`ontology_merge.py`）这套「人工修订优先于机器」的机制本身是对的，但它**只在同一个 draft 本体行内成立**。而 `publish()` 会把这一行就地翻成 `published`，于是「发布」这个动作把工作台从系统里抽走了——之后任何一次再生成、人工生成，都只能新建一个**空白草稿行**从零开始。人工修订的优先级在发布边界上断裂，域里开始出现两个平行本体，UI 又用 `max(updated_at)` 在两者之间来回切换主体。

**主张：** 一个数据域**恒定只有一个本体行**，它既是工作台也是发布载体；发布不再改变"工作台在哪"，只做**实体提升 + 版本快照 + 把已发布字段升格为人工权威**。人工修订优先级正式定义为三级：

```
已发布值（结构性字段）  >  人工修改/钉住  >  机器最新输出  >  机器基线
```

---

## 1. 现状：数据层实际发生了什么

### 1.1 三个入口，同一个目标选择逻辑

| 入口 | 代码 | 目标本体的选法 |
| --- | --- | --- |
| 预生成 full/objects | [draft_task_service.py:751](backend/app/services/draft_task_service.py:751)、[:864](backend/app/services/draft_task_service.py:864) | `_get_draft_ontology()` 找 `status=draft`，**找不到就新建空的** |
| 预生成 relations | [draft_task_service.py:931](backend/app/services/draft_task_service.py:931) | 同上，找不到直接失败 |
| 人工生成 | [manual_creation.py:153](backend/app/services/manual_creation.py:153) | 同上，找不到就新建空的 |

三处共用同一个前提：**「工作台 = 状态为 draft 的那个本体行」**。

### 1.2 发布把这个前提打碎

[publish.py:433](backend/app/services/publish.py:433) 就地把本体行 `draft → published`，同时 `version + 1`、写 `VersionRecord` 快照、把「已确认的 business_object + 其属性 + 两端都已发布的关系」提升为 `published`（部分发布，[publish.py:442-479](backend/app/services/publish.py:442)）。

发布之后，该域**不存在 status=draft 的本体**。于是：

- 再点「生成本体草稿」→ `_get_draft_ontology()` 返回 `None` → 新建一个**空**本体行 → 机器输出合并进去。
- 新行的 `machine_baseline`、`overridden_fields` 全空 → 上一版所有人工改名、改角色、复核确认**在新草稿里根本不存在** → 机器结论原样复活。
- 三方合并的三个输入里，`ours` 退化成 `theirs`，`base` 是空——合并等价于「全新生成」。

**这就是"人工修订优先级不闭环"的根因**：优先级只在行内连续，跨发布边界即失忆。

### 1.3 唯一的正路很容易被堵死

[ontology_revision.py](backend/app/services/ontology_revision.py) 提供了正确解法——从已发布本体克隆一份修订草稿，并把已发布值播种成人工权威（`overridden_fields = 全字段`）。但：

- 它在**已有草稿时直接报错**（[:65](backend/app/services/ontology_revision.py:65)）。
- 前端按钮只在 `latest_ontology_status === "published"` 时才渲染（[DomainDetailPage.tsx:518](frontend/src/pages/DomainDetailPage.tsx:518)）。
- 而**全系统没有任何删除/丢弃草稿的入口**（`_purge_draft_ontologies` 只在生成内部调用，无 API、无 UI）。

⇒ 用户发布后误点一次「生成本体草稿」，就永久失去走修订路的能力，且无法自救。

---

## 2. 断点清单

### 🔴 致命（数据正确性）

**B1 · 发布后再生成 = 静默开一条平行世界**
见 §1.2。用户视角：工作区突然从"我修过的 v1"跳成一张全新机器草稿，人工成果像是丢了（其实在另一行里，只是页面不指向它了）。

**B2 · 第二次发布 = 一个域出现两个 published 本体**
新草稿 `version` 从 0 起（`create_revision_draft` 也不继承 `published.version`），发布后又是 v1；旧的 published 行**没有任何代码把它置 archived**（`OntologyStatus.ARCHIVED` 全库只有 [warehouse.py:486](backend/app/api/warehouse.py:486) 读过，从未被写入）。后果：

- [`_published_ontology_ids()`](backend/app/services/ontology_query.py:191) 返回**全部** published 本体 → 同一个业务对象在本体浏览页出现两份；Data Agent 的可检索集与知识包同样翻倍。
- 版本历史挂在 `VersionRecord.entity_id = ontology.id` 上，而「版本历史」按钮用 `published_ontology_id`（按 `published_at desc` 取新的那个）→ 旧本体的 v1..vN 从历史里整段消失。

**B3 · 「确认发布」一个按钮两种语义**
它发布 `latest_ontology_id`，而 latest = 全域按 `updated_at` 取最大、**不区分状态**（[workspace_service.py:191](backend/app/services/workspace_service.py:191)）：

- latest 是 published 行 → 原地 v+1（正确的演进）
- latest 是新 draft 行 → 造出第二个 published 兄弟（B2）

用户完全无法从界面判断自己在哪条路上——页头只有一个状态徽标。

### 🟠 严重（治理语义）

**B4 · 复核状态寄生在 `role_reason` 字符串里，而它又是可合并字段**
`[待复核]` 前缀是全库复核状态的唯一真源（[edit.py:37](backend/app/services/edit.py:37)），同时 `role_reason` 出现在 [`OBJECT_FIELDS`](backend/app/services/ontology_merge.py:41) 里参与三方合并。连带三个后果：

- 人一旦确认（去掉前缀），`role_reason` 与基线不同 → 该字段被**永久钉住** → 机器再也刷新不了角色依据文本。
- 机器换个措辞 → 双改 → 冲突面板出现一条「角色依据」冲突 → 点「采纳上游」([provenance_service.py:141](backend/app/services/provenance_service.py:141)) → `[待复核]` 前缀被写回 → **该对象被静默重新打标 → 下次发布它掉出发布集**。一次文案取舍改变了治理状态。
- `resolve-all + accept_theirs` = 一键作废全域人工复核。

叠加已知案例（odoo 源无 PK → 100% 打标 → 发布提升 0 个 → 本体浏览页空白），这是"发布看起来没反应"的直接原因。

**B5 · 人工权威只有"永久冻结"一档，且放开入口没接线**
[ontology_merge.py:141](backend/app/services/ontology_merge.py:141) `pinned = f in overridden` → `user_changed` 恒为 True → 机器永不再更新该字段。放开只有两条路：冲突面板选「采纳上游」，或 `POST /api/fields/pin`——而 `api.setFieldPin` 在前端**定义了零调用**（[api.ts:257](frontend/src/api.ts:257)）。于是人工投入越多，再生成的有效面越小，且这个"冻结面"对用户完全不可见。

**B6 · 编辑已发布实体会把它从对外可见集里摘掉**
[`update_object_type`](backend/app/services/edit.py:148) 不校验本体状态，published 实体被编辑后 `status → edited`；而对外可见集按实体 `status=published` 过滤。用户只是改了个中文名，那个对象就从本体页 / Agent 里消失，直到他再点一次发布——且界面上没有任何提示告诉他这件事发生了。与 [DOMAIN_MODEL.md §5.3](docs/DOMAIN_MODEL.md) 冲突。

### 🟡 体验（能力已有但没交付）

**B7 · 机器这次到底改了什么，用户看不到**
后端每次运行都落了完整合并报告（新增/更新/保留/冲突/上游消失，`_store_merge_report` + `GET /tasks/{id}/merge-report`），前端 `api.getMergeReport` **零调用**（[api.ts:232](frontend/src/api.ts:232)）。用户点完生成只得到一句 toast「生成完成」，然后自己去几百个对象里翻。**二次生成最核心的价值（"告诉我上游变了什么"）根本没有交付。**

**B8 · 三个生成范围里两个在工作区点不到**
后端有 full/objects/relations，前端 `handleGenerate` 也写好了三种确认文案，但下拉菜单只挂了 full + 人工生成（[DomainDetailPage.tsx:538](frontend/src/pages/DomainDetailPage.tsx:538)）。objects/relations 只能从 Data Agent 的提案块触发（[ChatBiReferences.tsx:1178](frontend/src/pages/chat-bi/ChatBiReferences.tsx:1178)）——**Agent 能做的事，工作区做不到**。

**B9 · 三种范围的"新鲜度"语义不一致且不可见**
`full` 会清 checkpoint + evidence 缓存强制重抓上游；`objects`/`relations` 不清，复用 TTL 内的磁盘缓存。同一个"生成"心智下三种新鲜度，界面上没有任何提示，用户无法回答"我这次生成到底是不是拿的最新元数据"。

**B10 · 工作区主体不稳定 + 域卡片状态闪烁**
`latest` 跨状态按 `updated_at` 取——任何一次编辑或合并都可能让页面主体在 draft / published 之间跳；域卡片的 `status` 同理。后端已算好的 `draft_count` / `published_count` 在卡片上没有渲染，列表页看不出哪个域有未处理草稿。

---

## 3. 目标模型

### 3.1 一域一本体（Single Working Ontology）

```
DomainContext ──1:1── Ontology（常驻工作台 + 发布载体）
                        ├─ version：最近一次发布的版本号
                        ├─ 实体状态：suggested / edited → published
                        └─ VersionRecord[]：每次发布的快照（历史在这里，不在多余的本体行里）
```

- 数据库加 `UNIQUE(domain_context_id)`。三个生成入口 + 人工生成统一改调 `get_or_create_working_ontology(db, domain_id)`。
- 本体行的 `status` 语义降级为「是否发布过」，**不再作为"工作台在哪"的判据**。
- **删除** `create_revision_draft` / `POST /domains/{id}/create-revision` / 前端「创建修订草稿」按钮——单本体模型下它的价值（播种人工权威基线）由 §3.2 在发布时就地完成，不需要克隆。
- 实体 id 全程稳定 ⇒ 业务逻辑绑定、物化契约、数据应用数据集、语义索引**不会在发布时断链**（这是不能走"发布即另建新本体行"路线的硬约束）。

**直接消灭：B1、B2、B3、B10 的一半。**

### 3.2 三级权威 = "人工修订优先级"的正式定义

在 `ontology_merge.py` 引入字段权威分级常量：

| 层级 | 字段 | 再生成时的行为 |
| --- | --- | --- |
| **结构性**（发布后即人工权威） | `name`、`display_name`、`table_role`、`data_type`、`semantic_type`、`cardinality`、`structure_type` | 实体已发布 ⇒ 视为已钉住；机器改动**只提冲突不改值** |
| **描述性**（机器可持续刷新） | `description`、`role_reason` | 除非人工显式钉住，机器改动直接采纳，不产生冲突 |

实现方式：`publish()` 在提升实体状态的同时，把该批实体的**结构性字段**写入 `overridden_fields`（`ontology_merge.seed_published_authority`）——即原先 `create_revision_draft` 的"播种权威"动作，改成**发布时就地做**，不克隆。

> 实施修正：**不推进 `machine_baseline`**（原方案写了"并推进"）。基线必须停在机器上次的输出上：机器再给出同一个值时 `inc == base`，人工值静默保留；若把基线推到已发布值，机器每跑一次都会重报同一条冲突，"冲突只提示一次"的约定就破了。

于是优先级链条完整且跨版本连续：

```
已发布结构性字段  >  人工修改/显式钉住  >  机器本次输出  >  机器基线
```

同时把描述性字段从冲突通道里放出去，避免二次生成在 700+ 表的域上产出几千条无意义的文案冲突。

**直接消灭：B5 的"永久冻结"歧义；缓解冲突洪水。**

### 3.3 复核状态升格为真列

- `ObjectType` 加 `needs_review: bool`，成为复核状态唯一真源。（`reviewed_at` / `reviewed_by` 未实施：现有 `EntityChangeLog` 已记录谁在何时改了什么，再加两列是重复记账。）
- `role_reason` 回归纯描述文本，属描述性字段，机器可自由刷新。
- `publish()` 的部分发布判据从 `role_reason.startswith("[待复核]")` 改为 `needs_review == False`。
- `list_object_types(needs_review=...)` 的 `ilike('%待复核%')` 改为列过滤（顺带去掉一处全表 LIKE 扫描）。

**直接消灭：B4 全部三个后果。**

### 3.4 人工编辑已发布实体：立即生效 + 待固化提示

**推荐（A 案）**：人工是最高权威，改了就**立即对外生效**，不再降级 `status`；页头常驻「N 项已发布内容被修改，尚未固化版本」提示条，发布按钮带数字徽标。发布语义收敛为「打版本快照 + 提升新确认的实体」。

理由：与 §3.2 的权威模型自洽——**人工即权威、机器才需要闸门**。

> 严格变体（B 案）：编辑写入"待发布变更层"，对外继续服务上一版值。需要给实体加 `published_snapshot_json` 并让所有 `published_only` 读路径做值覆盖，改动面大得多。若治理合规要求"已发布内容非经审批不得变更"，再走 B 案。

**直接消灭：B6。**

### 3.5 工作区把已有能力接上线

| 断点 | 改动 | 成本 |
| --- | --- | --- |
| B7 | 生成完成后自动弹「本次变更报告」抽屉（接已有 `api.getMergeReport`）：新增/更新/保留/冲突/上游消失，每项可跳转实体 | 小 |
| B8 | 生成下拉补齐「仅业务对象」「仅业务关系」（`handleGenerate` 已实现，只缺菜单项） | 极小 |
| B9 | 统一 evidence cache 清理语义：三个范围的**全新生成**一律清检查点+证据缓存（只有重试/自动续跑复用） | 小 |
| B5 | 实体详情页每个可合并字段旁给 pin 图标（接已有 `api.setFieldPin`）；列表页标「已钉住 N 字段」 | 中 |
| B10 | `latest_ontology_id` 更名 `working_ontology_id`；域卡片改显示「已发布 vX · 未发布变更 N · 待复核 M」 | 小 |
| §1.3 死锁 | 新增 `POST /domains/{id}/discard-unpublished`（二次确认）：把工作本体回滚到最近一次发布快照 | 中 |

### 3.6 发布前置面板（Preflight）

发布确认框从一段静态文案改成实算清单：

```
将发布：业务对象 128 / 属性 1,204 / 业务关系 96
将跳过：待复核对象 41、端点未发布的关系 12、未解决字段冲突 7
版本：v3 → v4
```

这样「点了发布但本体浏览页还是空的」在**点之前**就能看见原因，而不是事后靠猜。

---

## 4. 实施结果

按依赖顺序落地（**字段权威分级必须先于一域一本体**——两行并一行后没有它，再生成会直接静默改写正对外服务的内容）：

| # | 内容 | 落点 |
| --- | --- | --- |
| 1 | 字段权威分级 + 发布播种 | `ontology_merge.DESCRIPTIVE_FIELDS` / `seed_published_authority`、`publish.py` |
| 2 | 一域一本体：写侧统一取行 | 新增 `services/ontology_workspace.py`；`draft_task_service`（三个范围）、`manual_creation` 改调 |
| 3 | 同域第二个 published 兜底拦截 | `publish.py` sibling 校验 |
| 4 | `UNIQUE(domain_context_id)` + 存量收敛 | 迁移 `4aa435f23621`；删 `ontology_revision.py`、`/create-revision`、前端「创建修订草稿」 |
| 5 | `needs_review` 真列 | 迁移 `5ec47c2fd4c3`；分类器→证据→草稿→合并→发布→查询全链路改判据 |
| 6 | A 案：人工编辑已发布实体立即生效 | `edit._mark_edited`；迁移 `341f29e30b22` 加 `has_unpublished_change` |
| 7 | 放开遗留 `role_reason` 钉住 | 迁移 `bacbc3c392ad`（确认复核的副作用产物，非用户本意） |
| 8 | 发布前置面板 | `publish.select_publishable` / `preflight` + `GET /ontologies/{id}/publish-preflight`，与发布共用判定 |
| 9 | 域指标 + 待固化提示条 | `workspace_service._publish_metrics`；`DomainContextDetail` 加四个计数 |
| 10 | 生成下拉补齐 + 丢弃未发布 | `DomainDetailPage`；`POST /domains/{id}/discard-unpublished` |
| 11 | 合并报告抽屉 | 生成完成即弹（接已有 `api.getMergeReport`，此前零调用） |
| 12 | 人工权威字段面板 | 新增 `FieldAuthorityPanel`（接已有 `api.setFieldPin`，此前零调用） |
| 13 | 证据缓存新鲜度统一 | `draft_task_service._reset_evidence_for_fresh_run` |

`latest_ontology_id` → `working_ontology_id` 全链路更名；`report_duplicate_drafts` 与 `/draft-duplicates` 随之删除（一域一本体后结构上不可能有重复草稿）。

## 5. 验证

**测试**：`1541 passed, 3 skipped`（基线 1531，新增 12 条）。新增 `tests/test_ontology_lifecycle_loop.py` 钉住闭环本身：

- 发布后再生成复用同一行本体，域内恒 1 行
- 发布把结构性字段钉住、描述性字段不钉
- 已发布对象角色被机器改判 → 值不变 + 记冲突
- 描述性字段发布后仍被机器刷新，不进冲突面板
- 发布不推进基线 → 机器不改口时不重复打扰
- 复核状态与 `role_reason` 冲突解决互不干扰
- 再生成不回写复核状态
- 人工建模落在同一行工作本体
- 库层拒绝同域第二行本体
- 发布前置面板与实际发布口径一致
- 已发布实体被编辑后仍 published 且计入待固化
- 丢弃未发布内容不动已发布部分

**真实实例**：dev 库（erpnext 1033 对象 / odoo 483 对象）迁移前就已带着 B2 的病灶——`erpnext` 域有**两个 published 本体、都是 v1**（1033 对象各一份，已发布对象 154 vs 58），本体浏览页因此双计。迁移收敛到原始血脉那行，无孤儿残留。

UI 实测：域卡片显示「154 已发布 / 1033 对象」；odoo 页头提示条「251 项待提升」；发布前置面板给出「将发布：业务对象 78 · 属性 1375 · 业务关系 173；将跳过：非业务对象 405 · 端点未发布的关系 688」——这正是此前只显示一句「发布成功」的那个场景；「人工权威字段」面板放开一个遗留 `role_reason` 钉住，`overridden_fields` 落库同步清空。

## 6. 已定决策

| 决策 | 取值 |
| --- | --- |
| 人工编辑已发布实体 | **A 案**：立即生效、状态不退回，改动计入 `has_unpublished_change` 与页头「待固化」提示条 |
| `display_name` 归类 | **结构性**——业务名是人工最主要的资产，发布后机器只提冲突 |
| 发布是否推进 `machine_baseline` | **否**（原方案有误）：基线停在机器上次输出，避免每次重跑重报同一条冲突 |
| 生成的元数据新鲜度 | 三个范围的全新生成一律重新抓取；只有重试/自动续跑复用检查点与证据缓存 |
