# 本体可读性重排：业务板块的生成、组织与人工修正

> 状态：**核心代码已落地，存量数据回填待执行** · 2026-09-01
> 核验：P0/P1 核心链路、L1 地图、审核模式、增量维护和关系复核已接通；ERP 存量本体的板块命名/回填使用 `backend/scripts/backfill_ontology_segments.py --apply`，需可用的 LLM 运行环境。
> 范围：板块生成（后端）+ 本体浏览/审核信息架构（前端）+ 人工增量修正
> 数据依据：ERP 本体 `c9820a62-66cb-4cc0-a2cc-36ff0bde5c77`（1035 对象 / 1332 关系），`backend/ontometa.db` 2026-08-31 快照
> 配套：[方案页](https://claude.ai/code/artifact/995b93ef-1ff4-47b3-a162-7e1190411331) · [可点击原型](https://claude.ai/code/artifact/55189017-8ae8-495b-86b4-8bb9541a1077)（真实数据）

---

## 0. 结论先行

**诊断：** 本体浏览页把本体呈现成一面按字典序排列的卡墙，对象在一个 Tab、关系在另一个 Tab——屏幕上永远凑不齐「对象—关系—对象」这个最小可读单元。后端其实早就会做社区检测，能聚出业务板块，但**前端没有任何入口**，而且**聚出来的板块本身还不能用**：48 个板块里 26 个的名字是机械兜底，前 8 个里 3 个是纯 MySQL 监控表。

**主张：** 以**业务板块**为支点重排。板块不是布局的一个装饰，是整套可读性的地基——地图、板块页、审核进度全挂在它上面。因此顺序是：

```
先把板块算准（后端）→ 再接三级下钻的信息架构（前端）→ 人工修正必须增量生效（两侧）
```

三条硬主张：

1. **板块在生成本体时产出，不在请求时现算**——生成时已经算过一遍了，只是把结果扔了。
2. **聚类跑两遍**，第二遍剔除技术表。实测 48 → 12 个板块，机械命名 26 → 5，噪声板块归零。
3. **人工修正一律增量生效，全量重划退成显式动作**。AI 生成是提效，不是求一次到位；改一处就重跑整轮，审核就退化成批处理。

---

## 1. 体检：现在为什么读不出结构

以下均为 ERP 本体真实数据。

### F1 屏幕上凑不齐一个三元组

对象 Tab 与关系 Tab 互斥（[OntologyWorkspaceView.tsx:502](../frontend/src/components/OntologyWorkspaceView.tsx:502)）；卡片脚注只给「4 关系」这个计数，不给「连到谁」（[:707](../frontend/src/components/OntologyWorkspaceView.tsx:707)）。计数是统计量，不是语义。

### F2 排序编码的是拼音，不是业务

卡片按 `display_name` 字典序排列（[OntologyWorkspaceView.tsx:219](../frontend/src/components/OntologyWorkspaceView.tsx:219)）。已发布业务对象 138 个 = 7 页；工作区草稿全量 1035 个 = 52 页。翻页顺序与业务重要性完全无关。

### F3 已发布视图里最显眼的是字典表

已发布 154 个对象中 **66%（102 个）一条关系都没有**；度数最高的是 计量单位(5)、国家(2)、币种(2)。真正的骨干——公司(136 度)、客户(61 度)、商品(63 度)——还压在草稿里。

### F4 关系名被两个空动词吃掉

1332 条关系只有 79 个不同名字，其中「属于」453 +「引用」440 = **67%**。而这 453 条「属于」有 **440 种不同的端点组合**——语义在端点上，不在动词上；现在按动词分组恰好丢掉唯一有信息的一维。

目标端集中度：公司 108 / 定时事件 45 / 项目 27 / 客户 26。

### F5 已经算好的业务板块没有入口

[`grouped-graph`](../backend/app/api/ontology.py:249) 已经会做标签传播聚类 + 枢纽摘除 + 簇命名（[ontology_query.py:800](../backend/app/services/ontology_query.py:800)），实跑得到 48 个板块 + 28 个枢纽。但：

- 前端**没有任何页面挂载 overview 模式**（只有 [ObjectTypeDetailPage](../frontend/src/pages/ObjectTypeDetailPage.tsx) 与 [RelationTypeDetailPage](../frontend/src/pages/RelationTypeDetailPage.tsx) 用它画单对象邻域图）；
- [ClusterMatrixView.tsx](../frontend/src/components/graph/ClusterMatrixView.tsx) 至今 **0 引用**，是死代码；
- `getOntologyGroupedGraph` 已在 [api.ts:534](../frontend/src/api.ts:534)，无人调用。

### F6 详情页也是切片的

对象详情把 基本信息／属性／关系列表／关系图谱／版本记录 拆成 5 个 Tab（[ObjectTypeDetailPage.tsx:864](../frontend/src/pages/ObjectTypeDetailPage.tsx:864)）。要回答「这个对象是什么、关键字段有哪些、连着谁」得切三次。

---

## 2. 三条原则

| | 原则 | 含义 |
| --- | --- | --- |
| 一 | **最小可读单元是三元组** | 任何一屏，只要声称在展示本体，就必须同时出现「对象 / 关系 / 对象」。只出现对象的屏叫表清单。 |
| 二 | **先看形状，再看名字** | 入口从「1035 张卡」换成「N 个板块 + M 根骨架」。卡墙退居「我已经知道名字」的检索场景。 |
| 三 | **顺序即语义** | 排序、分组、默认过滤都要编码业务重要性。字典序不是中立的，它在主张「这些东西同等重要」，而这句话是假的。 |

---

## 3. 板块：从哪来，怎么才算得准

### 3.1 现状：算了，只留下一个数字

[evidence_builder.py:659](../backend/app/services/evidence_builder.py:659) 在**生成阶段**就跑了 `label_propagation_clusters`，图是 DataHub 的**真外键 + 推断外键 + 血缘**。但它只取 `segment_size`（该表所属聚类有几个成员），**成员集合当场丢弃**。

这个数字喂给 [object_classifier.py:83](../backend/app/services/object_classifier.py:83)（`_MIN_SEGMENT_SIZE = 3`）：不隶属任何业务环节的表会从 `business_object` 降级。

⇒ **板块已经是角色判定的必要条件**，只是没有身份、没有名字、没落库。

顺带一提，生成时的图比请求时的好：

| | 生成时（evidence_builder） | 请求时（ontology_query） |
| --- | --- | --- |
| 边来源 | DataHub 真外键 + 推断外键 + 血缘 | 已落库的 `RelationType` |
| 是否被角色剥过 | 否 | 是（rule1：两端必须都是 business_object） |
| 命名素材 | 原始英文表名 | 已有中文业务名 |

**划分该用生成时的图，命名该用生成后的中文名**——所以要拆成两步，插在管线的两个不同位置。

### 3.2 硬伤：喂进去的是全部表

`_build_graph_signals` 对 `{ds.name for ds in bundle.datasets}` 聚类——`performance_schema` 的监控表、Frappe 的打印模板与定时任务，全都参与业务板块的形成，**污染划分本身**。

最大那个 55 成员的板块之所以叫「信头抬头」，就是因为一张打印模板表被工单、会计分录、对账单处理围在中间。

### 3.3 两遍法：第二遍剔除技术表（实测对照）

先有鸡还是先有蛋是真的：角色判定要 `segment_size`，干净的板块要先有角色。解法是**跑两遍**——标签传播很便宜，第二遍几乎不花钱。

| | 现在：全部表一起聚 | 第二遍：剔除 technical 重聚 |
| --- | --- | --- |
| 参与聚类的对象 | 1035 | 430 |
| 聚出的板块 | **48** | **12** |
| 名字是机械兜底的 | **26** | **5** |
| 枢纽 | 28，含 文档类型定义 / 定时事件 | 11，全是 公司·客户·商品·会计科目·项目·仓库·供应商… |
| 前 8 个板块里的噪声 | **3 个是纯 MySQL 监控表** | **0 个** |
| 最大板块 | 「信头抬头」55 个<br>`信头抬头·序列号·工单·会计分录·对账单处理` | 「成本中心」47 个<br>`成本中心·报价单·固定资产·商机·供应商报价` |

第二遍出来的 12 个板块每一个都一眼能读出业务：

| 板块（机械名） | 成员样例 | 实际业务含义 |
| --- | --- | --- |
| 成本中心 | 成本中心·报价单·固定资产·商机·供应商报价 | 财务与商机 |
| 派工单 | 派工单·库存录入单·序列号·物料清单·工单 | 生产与库存 |
| 联系人 | 联系人·员工·客户分组·定价规则·销售区域 | 客户与销售 |
| 收付款单 | 收付款单·付款请求·银行账户·银行保函 | 资金收付 |
| 税务规则 | 税务规则·税务类别·采购税费模板 | 税务 |
| 支付方式 | 支付方式·POS开班明细·收银结款明细 | POS 收银 |
| 任务 | 任务·依赖任务·项目模板任务·资产维保任务 | 项目任务 |
| 营销活动 | 营销活动·营销活动商品·邮件活动 | 营销 |

名字仍是机械的（取自度数最高成员），但**素材终于对了**，LLM 命名才有得可命。

复现脚本思路：读 `backend/ontometa.db` 建无向邻接 → `identify_hub_nodes(adj, min(40, max(5, len(clustered)//20)))` → 摘枢纽后 `label_propagation_clusters` → `split_dominant_clusters` → `name_cluster`；两遍的差别只是参与节点集合是否含 `table_role='technical'`。

### 3.4 生成管线的两个插点

```
DataHub 证据 ──▶ 聚类①（全图，已存在）──▶ 生成本体信息 ──▶ 聚类②（新增）──▶ LLM 板块命名 ──▶ 落库
   表·外键          只产 segment_size        对象·关系·角色     只在业务对象子图      成员中文名+动词    ontology_segments
   推断FK·血缘             │                 LLM 中文命名          → 12 板块+11 枢纽     无 LLM 则报错       + 锚点成员
                          │                 + 角色否决                  ▲
                          └── 回喂角色判定 ──▶│                          │
                                              └── 角色定了才剔得掉技术表 ─┘
```

**聚类①（保持现状不动）**
位置：[evidence_builder.py:659](../backend/app/services/evidence_builder.py:659)。产物是**一个信号**（`segment_size`），不是板块。必须先于角色判定。

**聚类②（新增）**
位置：`draft_generator._build_object_types_from_evidence` 之后。这个时点很关键——LLM 的角色否决（`role_overrides`，复用对象命名那一次调用，零额外 LLM 成本）刚好在这之前生效，`performance_schema` 那批已被正确打成 technical。此时中文业务名也已经有了。
只在 `business_object + bridge` 子图上重跑 `label_propagation_clusters` + `identify_hub_nodes`。

**LLM 板块命名**
输入：每个板块的成员中文名（按度数取 top 15）+ 板块内高频动词 + 该板块连的枢纽。
沿用对象命名同一条法则：**没有 LLM 就报错，不接受机械名降级**（参见 [draft_generator.py](../backend/app/services/draft_generator.py) 的 `MissingBusinessNameError` 与 `_LLM_SYSTEM_PROMPT`）。
复用 `draft_checkpoint` 分块续跑，复用 `object_naming` 去碰撞——「税务规则」和「税务类别」这类很容易撞名。

### 3.5 落库结构

```
ontology_segments
  id / ontology_id / name / display_name
  anchor_refs        -- 度数最高的 K 个成员的 source_ref，重算时的对齐键（见 6.3）
  member_count
  origin / machine_baseline / overridden_fields / conflict_json / needs_review   -- ProvenanceMixin 同款

object_types
  + segment_id       -- FK → ontology_segments
  + is_hub           -- 枢纽不属于任何板块，现在靠约定推出来
```

**枢纽必须一起落库。** [`identify_hub_nodes`](../backend/app/services/community_detection.py:101) 的门槛是 `max(15, 平均度×3)`，均值一变枢纽集合就变，板块划分跟着全变——这是划分不稳定最大的单一来源，不落库连「为什么这次和上次不一样」都答不了。

### 3.6 纠正：高频外键列不是噪声

一个容易做错的动作是「把高频外键列拉黑」。实测外键列名分布：

| 列名 | 出现次数 | 指向 | 判定 |
| --- | --- | --- | --- |
| `company` | 99 | 公司 | ✅ 真实多租户结构 |
| `project` | 28 | 项目 | ✅ 真实 |
| `cost_center` | 23 | 成本中心 | ✅ 真实 |
| `customer` | 22 | 客户 | ✅ 真实 |
| `EVENT_NAME` / `event_name` | 42 | 定时事件 | ❌ 框架管道 |
| `reference_doctype` | 25 | 文档类型定义 | ❌ 框架管道 |
| `letter_head` | 15 | 信头抬头 | ❌ 框架管道 |

拉黑高频列会毁掉真结构。真正的噪声**按角色剔除就够了**，不需要列黑名单。

---

## 4. 信息架构：地图 → 板块 → 档案

三级下钻，每一级都遵守原则一（屏幕上一定有关系）。L1 用现成的 `grouped-graph`，L2 用现成的 `clusters/{id}`，**都不需要新建后端能力**。

### 4.1 L1 业务地图（新首屏）

> **2026-09-02 修订**：初版把 L1 做成了「全域宏观图」——版块气泡 + 枢纽骨架 + 按权重加粗的
> 聚合连线。实测直接失败：erpnext 已发布本体（154 对象 / 35 关系）在屏幕上只剩 16 个无标签
> 色块和 5 条细线，草稿态（1035 对象）也只是 17 个色块 + 14 个枢纽。**一个对象名都读不到，
> 一条关系动词都读不到**，违背原则一。宏观图能回答的只有「有几块」，而那是目录该干的活。
>
> 现在 L1 **默认就是「某一个业务模块内部的关系图」**，全域宏观图退成目录里的一个可选项。

两栏：

- **左栏 · 模块目录**：按**模块内关系条数**排序（不是成员数）——能读出业务的模块排在最前，
  「N 对象 · M 关系」+ 关系量条给量感。底部两个伪条目：「全域概览」（旧宏观图）与
  「未接入模块的对象」。
- **右栏 · 模块关系图**：真节点、真关系、真动词。默认选中关系最丰富的模块，打开就有东西可读。
  - 「关系图 / 关系清单」切换：图给形状，句子给精确（句子直接复用 `relation_sentences`）。
  - 「含跨模块邻居」开关：跨模块关系普遍多于模块内关系（销售与服务 140 vs 51），
    整条泼出来会退回毛线球，所以按外部对象聚合成 `neighbors`、按连接条数取前 12 个，
    虚线灰卡 + 「N 条 · 所属模块」徽标。默认关。

**可读性优先于一屏看全**（这是本次的核心取舍）：

- 详情图按形状选布局——> 12 个节点走力导向（`d3-force`，固定随机种子保证同一份数据每次同样落位），
  否则走 dagre LR。原因：模块图是「一堆表指向少数被引用对象」的星形，dagre 会把二十几个节点
  塞进同一个 rank 摞成两千多像素高的柱子，`fitView` 一缩就全糊。
- 节点换**紧凑卡片**（140×38，只画名字），省下的像素换成可读的缩放。
- 适配后把缩放**顶回 0.8 下限**（13px 字剩约 10px），塞不下的靠拖拽。
  注意 G6 的 `autoFit: "view"` 会在渲染后再自适应一次覆盖掉这个下限，详情图必须关掉它自己适配；
  力导向是异步收敛的，还要在 `afterlayout` 再适配一次，否则量到的是布局没落定时的包围盒。
- 边多于 24 条时默认藏起动词标签并把线压淡（ERP 里 67% 的动词是「属于/引用」这类空动词），
  **悬浮某个对象即只看它的关系**——邻居与相关边亮起并显出动词/基数，其余压到近乎透明。
  这是稠密图里唯一真正管用的读法。

视图切换保留清单：地图管「我不知道要找什么」，清单管「我知道名字」。

### 4.1.1 板块划分是全覆盖分区（2026-09-02）

初版把聚不进业务模块的对象一律留在 `segment_id IS NULL`，前端叫「未接入对象」。
实测下来这个桶吞掉了 **890 / 1035 个对象（86%）**——它不是一种情况，是四种：

| 原因 | 数量 | 处置路径 |
| --- | --- | --- |
| 来自数据库自带 schema（sys 102 / information_schema 85 / performance_schema 81 / mysql 31） | **299** | 收窄摄取范围 |
| 判为 `technical` 的框架管道表（`segment_generator` 只在 business_object + bridge 子图上聚类） | **287** | 人工复核角色（585 个 technical 里 491 个仍 `needs_review`） |
| 可入池但在聚类子图上零边 —— 主因是 308 个桥表里 252 个零关系，其中 **230 个带 ERPNext 的 `parent`/`parenttype`/`parentfield` 泛型父引用**，`parent` 是不指向任何表的 varchar，FK 推断推不出来 | **268** | 补 parent 端关系推断 |
| 有边但只成单点簇（`clusters = [c for c in clusters if len(c) > 1]`，刻意丢弃） | **36** | 同上 |
| 枢纽（原本 `segment_id = None`，设计如此） | 11 | — |

混在一起就什么也做不了。现在改成**全覆盖分区：每个对象恰好属于一个板块**。

- `ontology_segments.kind`（迁移 `8c8a5b82add9`）：`business` / `shared` / `pending` / `technical` / `system`，
  定义与各自的收敛路径见 [`services/segment_kinds.py`](../backend/app/services/segment_kinds.py)。
- 只有 `business` 走 LLM 命名；其余四类名字固定（`__system_tables__` 这类双下划线标识名
  同时是重算时的**对齐键**，所以既不进 `dedupe_segment_names` 也不进 `_allocate_name`，
  否则每跑一次就多出一个空板块）。
- 枢纽从「不属于任何板块」改成归入 `shared`（公共主数据）。`is_hub` 与 `segment_id` 是两个
  正交的事实，之前用前者把后者置 None 是把判定和归属混为一谈。
- **grouped-graph 里要用两张映射**：板块的关系账按 `segment_of` 算，宏观边按 `cluster_of` 聚合
  （枢纽在宏观图上是独立节点）。混用会让「公共主数据」显示成 0 关系——而它恰恰是全图连接最密的一块
  （实测 内部 43 / 跨块 429）。
- 概览图只画 `business` 板块 + 枢纽：兜底板块动辄两三百个成员，画进去会把真业务模块挤成小点；
  布局也只对它们跑，免得业务模块之间留下大片空洞。
- 存量本体用 [`scripts/backfill_segment_partition.py`](../backend/scripts/backfill_segment_partition.py)
  回填，不必重跑生成（业务板块原样不动，只补兜底归属）。

前端左栏因此分两段：业务模块（按模块内关系数排序、给关系量条）在上，
「未进业务模块」四类在下（给种类图标、不给量条——成员数不代表业务体量）。
每一类在标题行写清「为什么在这」，这一栏的价值就是让人知道下一步该动哪里。

### 4.2 L2 板块视图

面包屑 + 板块摘要条（成员数 / 板块内关系 / 跨板块关系 / 待复核）；左侧骨架图（核心 + 卫星）与板块内关系句子，右侧成员表按度数排序。

稠密板块（成员 > 40）自动切**邻接矩阵**——[ClusterMatrixView.tsx](../frontend/src/components/graph/ClusterMatrixView.tsx) 已经写好并按结构类型着色，接上路由就能用，正好避开节点连线图在稠密簇里糊成毛线球。

> **2026-09-02 修订**：`get_segment_detail` 原本只在 `len(members) > 40` 时才返回 `edges`，
> 而最大的板块只有 32 个成员——**板块关系图从来没渲染过一次**，尽管它有 51 条内部关系。
> 现已改为恒返回，并一并带上 `neighbors` / `cross_edges`（跨板块邻居，按连接条数降序、
> 上限 `_SEGMENT_NEIGHBOR_CAP`）。`relation_sentences` 上限从 20 提到 200，覆盖整块。
> `grouped-graph` 的每个 cluster 也补了 `internal_relation_count` / `cross_relation_count`，
> 供 L1 目录排序。

### 4.3 L3 对象档案（合并 5 个 Tab 为一页）

- **关系写成句子**，不是表格行：`供应商 ──▶ 隶属于 供应商分组 · N:1 · fk supplier_group`。外键列名从 `source_evidence` 里读回（格式：`X 通过引用字段 F 关联 Y`）。
- **出向 / 入向分开**。「我依赖谁」和「谁依赖我」是两个不同的问题。
- **邻域图常驻右栏**，不是第四个 Tab。句子给精确，图给形状。
- **字段按语义角色分组**（主键 / 外键 / 普通属性），外键行直接写出指向哪个对象。

### 4.4 两处微观改动，收益最大

**对象卡**：把「31 关系」这个计数换成**邻居名 + 所属板块 + 落点**——同样的面积，读者拿到的是位置感而不是统计量。

**关系去重列表**：现在点进「属于」是 453 行三元组。改为按**目标端**二级分组：

```
属于  453 条 · 4 类归属
  ├ ⋯ 隶属于 公司        108
  ├ ⋯ 调度自 定时事件      45
  ├ ⋯ 归口   项目         27
  └ ⋯ 面向   客户         26
```

同一个「属于」，按目标端拆开就变回人话。

---

## 5. 浏览与审核：一套结构，两套动作

本体既要给人读，也要给人判。这两件事今天挤在同一个组件里，靠 `showRoleClassification` 一个布尔开关区分——结果两边都不好用。但它们**不该拆成两套界面**：判完立刻要切回读者视角确认「改完之后读起来对不对」。

### 5.1 这已经是两个不同的任务

| | 浏览模式 | 审核模式 |
| --- | --- | --- |
| 面对的数据 | 已发布 154 对象 / 33 关系 | 待复核 **866** 对象（84%）+ **1288** 条 suggested 关系 |
| 用户在问 | 这个域在干什么、X 连着谁 | 下一个判什么、判据是什么、能不能一次判一批 |
| 做完的标志 | 看懂了 | 队列清空了 |
| 有效排序键 | **度数**（谁是骨干） | **相似度**（同板块 + 同命名模式，谁能一起判） |
| L1 地图 | 认地形 | **进度地形**：哪块判完了、哪块还全红 |
| L2 板块 | 板块内谁是核心 | **一屏判一批**：同板块的表通常同类 |
| L3 档案 | 这是什么、连着谁 | **判据并排**：role_reason + 置信度 + 源表证据 + 同板块已判结果 |

置信度救不了场：`role_confidence` 中位数 **0.5**，1033 个里 **898 个挤在 0.5–0.7**；关系更极端——**1332 条的 `source_confidence` 全部在 0.5–0.7**。审核减负只能靠**成批裁决**。

### 5.2 三件今天解决不了的事

1. **队列取代分页**。866 个待复核，当前「仅看待复核 + 批量改」的批量**只作用于当前页**（[OntologyWorkspaceView.tsx:283](../frontend/src/components/OntologyWorkspaceView.tsx:283)，代码注释自陈原因是服务端分页跨页选择不保证一致）→ 866 ÷ 20 = **44 页手工翻**。
2. **排序键不同**。浏览按度数，审核按「同板块 + 同命名模式」聚堆。两个排序键塞不进同一个开关——这是必须分模式的硬证据。
3. 🔴 **关系根本没有复核态**。`relation_types` **没有 `needs_review` 列**（只有 `object_types` 有）。而按 rule1「业务关系两端必须都是 business_object」，**对象判错会连带删掉关系**——审核的因果链是「对象角色 → 关系存活」，工具却只覆盖了因、没覆盖果。

### 5.3 刻意不拆开的部分

- 不建两套组件树、两套数据源——L1/L2/L3 只建一次，加一个 `mode: browse | review` 参数。
- 不为审核单开一个应用。
- 已发布浏览页保持只读，编辑入口仍然跳工作区（现状即如此，保留）。

---

## 6. 人工修正：改一处，不该触发整片重划

AI 生成是提效，不是求一次到位——所以永远会有错，人工修正是常态而不是例外。既然如此，**每一次修正都必须当场生效**。如果改一个对象的角色就要重跑整轮聚类，地图每次都变样、人的空间记忆全废，审核退回批处理，进度条也失去意义。

### 6.1 增量矩阵

| 人工改什么 | 立刻怎么生效 | 要重跑吗 |
| --- | --- | --- |
| 板块名 / 描述 | 直接改，写进 `overridden_fields` | 不用 |
| 对象归属（挪到别的板块） | 直接挪，`segment_id` 进 `overridden_fields` | 不用 |
| 对象判为技术表 | 从当前板块摘掉，成员数 −1 | 不用 |
| 对象提为业务对象 | 按邻居投票落到某个板块；没邻居进「未接入」 | 不用 |
| 加 / 删一条关系 | 只重算这两个对象的归属 | 不用 |
| 重新生成草稿 · 显式点「重算板块」 | 全量重划（先给预览再落） | 是 |

**修板块最有效的手段是修角色和修关系**，不是拖拽对象——图对了板块自己就对，把一批技术表判对能一次修好一片。所以界面上「调整板块」的主入口应该是角色与关系，这跟审核模式按命名模式聚堆批量改角色正好是同一个动作。

### 6.2 为什么增量在算法上是自然的

标签传播本身就是**邻居投票**（[community_detection.py:19](../backend/app/services/community_detection.py:19)）。给一个对象重新定归属 = 只对这一个节点跑一轮投票，看它的邻居里哪个板块的成员最多。这不是另写一套逻辑，**就是同一个算法只跑一个节点**，复杂度 O(邻居数)。

「增量」和「全量」共用同一份代码，区别只是跑几个节点。增量是默认，全量退成显式动作：只在重新生成草稿、或人主动想看「按现在的判定重新分会是什么样」时才发生。

### 6.3 🔴 前提：板块得有一个扛得住重算的身份

对象靠 `source_ref` 对齐，关系靠 `relation_signature(source_ref, target_ref, structure_type)` 对齐（[ontology_merge.py:57](../backend/app/services/ontology_merge.py:57)）。**板块是派生物，没有天然的上游标识**：

- ❌ **序号**（`cluster-<idx>`，[ontology_query.py:854](../backend/app/services/ontology_query.py:854)）——按大小排序生成，数据一变就指向别的板块。[api/ontology.py:267](../backend/app/api/ontology.py:267) 已经在返回「聚类不存在或已随数据变化失效，请刷新概览图」，等于承认了这点。
- ❌ **成员集合 hash**——加一个成员就换身份。
- ✅ **锚点**：把度数最高的 K 个成员的 `source_ref` 存成 `anchor_refs`，新旧板块按 anchor 的 Jaccard 重叠匹配；超过阈值判为同一板块，人工改的名和钉住的归属跟着走；低于阈值算新板块，旧板块进 MergeReport 的 `removed`。

这跟 `ontology_merge` 处理关系的方式同构，能直接接进现有的 added/updated/kept/conflict/removed。**这条不做，人工修改每次重跑都会丢**——所以它是地基，不是收尾。

### 6.4 人工的东西任何时候都不丢

钉过的归属和改过的板块名进 `overridden_fields`，**全量重划也不覆盖**——[`_merge_entity_fields`](../backend/app/services/ontology_merge.py:165) 里人工值优先、双方都改进 `conflict` 交人工裁决。这套机制项目里已经有了，板块接上就行。

---

## 7. 关系语义

板块治形状，谓词治含义。

### S2 空动词按端点 + 外键列细化

「属于／引用」的语义躺在**外键列名**里：`supplier` → 下给，`company` → 隶属于，`parent_*` → 上级。先跑规则，剩下的批量送 LLM 重命名，结果**进待复核队列**而不是直接改。在改完之前，界面一律显示**三元组短语**而非裸动词——这一半今天就能做。

### S4 发布集要能自圆其说

已发布的 154 个对象只连着 33 条关系，公司/客户/商品这些枢纽都还在草稿里。发布门禁应当提示：**选中对象的一跳邻居若未发布，发布后它就是个孤点**。

---

## 8. 影响范围

### 8.1 前端

| 模块 | 影响 | 风险点 |
| --- | --- | --- |
| [OntologyWorkspaceView.tsx](../frontend/src/components/OntologyWorkspaceView.tsx) | 排序、卡片、Tab 结构 | ⚠️ **被两个页面共用**：[OntologyPage.tsx:167](../frontend/src/pages/OntologyPage.tsx:167)（已发布浏览）和 [DomainDetailPage.tsx:839](../frontend/src/pages/DomainDetailPage.tsx:839)（工作区草稿态，带批量改角色）。改排序/折叠孤点会同时改掉工作区的批量选择语义 |
| [graph/OntologyGraphView.tsx](../frontend/src/components/graph/OntologyGraphView.tsx) | 启用 overview 分支 | 这条分支**从没被真实调用过**，等于新代码，不是复用 |
| [graph/ClusterMatrixView.tsx](../frontend/src/components/graph/ClusterMatrixView.tsx) | 0 引用的死代码复活 | 同上，没跑过 |
| [ObjectTypeDetailPage.tsx](../frontend/src/pages/ObjectTypeDetailPage.tsx) | 合并 5 Tab | 独立，风险最低 |
| 路由 + [AppBreadcrumb.tsx](../frontend/src/components/AppBreadcrumb.tsx) | 新增「板块」层级 | — |

`ObjectTypeSummary`（[types.ts:283](../frontend/src/types.ts:283)）加字段是加法，安全，但有 **13 个文件**在用（`ExpressionRichEditor`、`ManualCreateModal`、`DatasetEditor`、`UnclaimedTablesModal`、`endpointSuggest.ts`…），所以**只能加不能改语义**。

### 8.2 后端

| 模块 | 影响 |
| --- | --- |
| [evidence_builder.py](../backend/app/services/evidence_builder.py) | 保留聚类①的成员集合（现在丢弃） |
| [draft_generator.py](../backend/app/services/draft_generator.py) | 新增聚类② + LLM 板块命名 |
| [community_detection.py](../backend/app/services/community_detection.py) | 增量单节点投票入口；命名改 LLM |
| [ontology_query.py](../backend/app/services/ontology_query.py) | `grouped-graph` 补 `published_only`；改读落库的板块 |
| [models/ontology.py](../backend/app/models/ontology.py) | 新表 + 两列 + 迁移 |

### 8.3 ⚠️ 两个容易忽略的外溢

1. **Data Agent 的域语义卡会跟着变**：[domain_semantic_card.py:195](../backend/app/services/domain_semantic_card.py:195) 直接调 `_compute_cluster_partition`，把板块名写进 Agent 的检索提示词。板块算法一改，Agent 行为跟着改——而且是**往好里改**，12 个真板块比 48 个含噪声的更值得写进 prompt。
2. **确定性会被打破**：`community_detection` 现在是纯计算模块，[test_community_detection.py:44](../backend/tests/test_community_detection.py:44) 专门断言多次运行结果一致。LLM 板块命名必须把结果**落库缓存**，而不是每次请求现调。

### 8.4 数据模型改动（一共两处）

1. 新表 `ontology_segments` + `object_types` 加 `segment_id` / `is_hub`
2. `relation_types` 加 `needs_review`

> ⚠️ 本仓 Alembic 修订号是手写顺序 hex，**极易撞号**，撞号症状是整套 pytest 炸几百个 error 且看不出跟迁移有关。加迁移前先脚本取号。

---

## 9. 落地顺序

### P0 · 板块算准并落库（后端 · S–M）

- [ ] **加聚类②**：角色判定之后，只在业务对象子图上重跑一次。实测 48 → 12 个板块、机械命名 26 → 5、噪声板块归零。复用现有 `community_detection` 函数，不写新算法。
- [ ] **板块与枢纽落库**：新表 `ontology_segments`，`object_types` 加 `segment_id` / `is_hub`。
- [ ] **锚点身份**：`anchor_refs` + Jaccard 匹配。**必须和落库同批做**，否则人工修正会丢。
- [ ] **`relation_types` 补 `needs_review`**：1288 条关系今天没有任何复核入口，而「修边」是修板块的主入口。

### P1 · 接线 + 人工修正（前端为主 · M）

- [ ] **挂 L1 地图**：`getOntologyGroupedGraph` 与 overview 模式都已存在，缺一个页面把它们接起来；同时给 `grouped-graph` 补 `published_only`（它现在[全量查草稿](../backend/app/services/ontology_query.py:898)，挂到只读浏览页会与下面的清单对不上）。
- [ ] **L2 板块页**，稠密板块切 `ClusterMatrixView`。
- [ ] **增量修正闭环**：改角色／改归属／改名当场生效，邻居投票只跑单节点；全量重划退成显式按钮 + 预览。人工值一律进 `overridden_fields`。
- [ ] **L3 档案页合并 Tab**，关系写成三元组句子。

### P2 · 命名与语义（数据侧 · M–L）

- [ ] **LLM 板块命名**：沿用对象命名的「无降级」约定，复用 checkpoint 分块与去碰撞。
- [ ] **审核模式**：跨页选择集、按命名模式聚堆、判据右栏、板块级进度。
- [ ] **空动词细化**（S2）与**发布门禁提示孤点**（S4）。
- [ ] **对象摘要补 `top_neighbors` 与 `segment_id/segment_name`**，卡片才显示得出「→ 采购订单 · 采购发票」。

---

## 10. 刻意不做的事

- **不引入新图库**——`@antv/g6` v5 已是依赖，矩阵视图是纯内联 SVG。
- **不建两套界面**——浏览与审核共用 L1/L2/L3，只加一个 `mode` 参数分动作层。
- **不让人工修正触发全量重跑**——增量是默认，重划是显式动作。
- **不拉黑高频外键列**——`company`/`customer` 那些是真结构，噪声按角色剔就够。
- **不删卡墙**——它对「我知道名字」的检索仍然最快，只是不再当首屏。
- **不做全图铺开**——1035 节点、307 节点的环状纠缠（见 `erp-lineage-is-cyclic-tangle`），铺开只会得到毛线球；聚类是唯一可读的降维方式。
- **不改发布流程**，也不改任何既有字段的语义。
