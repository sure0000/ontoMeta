# 血缘补录 V2：智能关系补充（Intelligent Relation Supplement）

> 状态：✅ **P0–P4 全部落地并通过真机验收（2026-09-07）**。
> 日期：2026-09-07
> 主战场：`backend/app/services/lineage_package.py`、`lineage_inventory.py`、`sql_lineage_extractor.py`，
> 新增 `key_family.py` / `relation_inference.py`；前端 `pages/LineageSupplementPage.tsx`、`components/lineage/*`
> 承接：现有「代码包扫描 + 画布补录」两条轨道（`docs/` 无前序方案，本文是该模块第一份设计文档）

---

## 0. 一句话结论

补录模块现在只有两种拿到关系的办法：**从 SQL 里读**（客户得给代码包）和**让人手连**（一条一条画）。
真实的 jwsp 域是 **138 张表、138 张孤岛（100%）、0 个声明式主键、0 个声明式外键**——没有代码包，
手连 138 张表不现实。

但同一个域里 **4832 个字段 100% 带样例值与 distinct 计数**。关系不是没有证据，是证据在**值**里，
没人去读：`sfz_hm` 里躺着 `440106196508233538`，`dh_hm` 里躺着 `17859641175`，52 种不同列名
（`bl_bh` / `zbr_bh` / `chujingr_bh`…）里躺着同一套 `RY00000183`。

V2 只做一件事：**把值当证据用**。机械信号把 4960 个字段收敛成 15 个「键族」，LLM 判定每个族是不是
真实体键并命名，**基数由 distinct/rows 算出来而不问模型**，人在画布上确认后才写。

---

## 1. 真实数据勘察（jwsp 域实测，2026-09-07）

以下数字全部来自对生产 DataHub（`http://100.93.8.29:8080`）的只读探针，不是估算。

### 1.1 家底

| 指标 | jwsp | odoo |
| --- | --- | --- |
| 表数 | 138 | 483 |
| 孤岛（上下游皆空） | **138（100%）** | 未测 |
| 声明式主键 | **0** | 0 |
| 声明式外键 | **0** | 有（`currency_id`/`company_id` 等 `fk=True`） |
| 字段总数 | 4832 | — |
| 带样例值 + distinct 的字段 | **4832（100%）** | 100% |

两个域是两种难度：**odoo 有声明式外键，现有管线够用**；**jwsp 什么结构信号都没有，只有值**。
V2 是为 jwsp 这类源设计的。

> ⚠️ 这也修正了一条陈旧假设：`ontology_projection.py:115` 的注释写「真实源零 PK 声明、也没开
> profiling（unique_count/row_count 全空）」。**profiling 现在是开的且全覆盖**，这条注释与依赖它的
> 保守判断都该复查。

### 1.2 值里有什么（键族聚类实测结果）

4960 个字段经机械闸门后剩 475 列，聚成 **15 个键族**：

| 值形状 | 列数 / 表数 / 列名种类 | 值样例 | 人读出来是什么 |
| --- | --- | --- | --- |
| `N<18>` | 21 / 19 / 2 | `440106196508233538` | **身份证号**（`sfz_hm`、`zj_hm`） |
| `N<11>` | 35 / 34 / 4 | `17859641175` | **手机号**（`dh_hm`、`ck_dhhm`、`sj_dhhm`、`syr_dh`） |
| `A<2>N<8>` | 90 / 40 / **52** | `RY00000183` | **人员编号**（52 种列名，命名匹配全数漏掉） |
| `N<16>` | 16 / 16 / 1 | `3101042026457051` | **案件编号**（`aj_bh`） |
| `N<15>` | 64 / 32 / 2 | `862973014507117` | **IMEI/IMSI 设备号** |
| `N<6>` | 116 / 73 / 34 | `310115` | **行政区划代码**（维度码，不是实体键） |
| `居民身份证` | 17 / 17 / 2 | `居民身份证` | ❌ 枚举值，噪声 |
| `中国` / `汉族` | 14 / 9 | `中国` | ❌ 枚举值，噪声 |

**这就是用户要的东西**：`A<2>N<8>` 族横跨 40 张表、52 种列名——`bl_bh`（笔录编号侧）、`zbr_bh`
（主办人编号）、`chujingr_bh`（出警人编号）指的是同一个「人员」。**没有任何命名规则能把这 52 个
名字连起来，值形状一眼就能。**

### 1.3 三个负面结论（决定了架构）

**(a) 列名相同 ≠ 有关系。** 跨表复现的列名有 698 个，最高频的是 `row_id`（137/137 张表）、
`logdate`（135）、`sj_ly`、`gengx_sj`、`rk_sj`、`etl_sj`——**全是 ETL/审计列**。
「同名列即关系」这条朴素规则在这个域上产出的几乎全是垃圾。

**(b) 5 个样例值做集合交集，噪声压倒信号。** 68667 组跨表列对中 980 组有 ≥2 个共同样例，
抽查全是假阳性：`aj_bl_zl.row_id ∩ aj_bldwry_zl.ba_dw_dm = ['3','5']`——两个低基数小整数撞上了。
**交集命中是强证据、但只在高基数值空间里成立**；低基数列上的交集毫无意义。

**(c) 两两配对是错的算法。** 第一版漏斗（形状一致 + 一端近唯一 + 列名共词元）产出
**26178 对候选**，其中绝大多数是时间戳互相配对——每个时间戳都唯一，`ratio=1.0` 让它们全部通过
「像主键」的闸门，而所有时间列共享同一个形状。
**必须先按语义把不能当 JOIN 键的列剔掉，再聚族，而不是先配对。**

修正后：4960 字段 → 475 候选列 → **15 个族**。展开成对是 15244 对，按族送 LLM 是 **15 条**。
这个量级差异（1000×）就是本方案的核心工程判断。

---

## 2. 现状：两条轨道与它们的边界

| 轨道 | 入口 | 关系从哪来 | 能力边界 |
| --- | --- | --- | --- |
| 代码包扫描 | 上传 `.zip`/`.sql` | `sql_lineage_extractor.extract()`：①有落点的语句（`INSERT`/`CTAS`/`CREATE VIEW`）→ 表级边；②语句里所有**两端属于不同表的等值谓词** → 关联键 | 客户没有代码包就完全失效；纯 DDL 包产出为 0 |
| 画布补录 | 手工连线 | 人直接给 `source_table`/`target_table`/`join_keys` | 零推断；138 张表手连不可行 |

两条轨道的产物都落 `LineagePackageEdge`（`models/lineage.py:73`），上报都走
`add_lineage_edge()` → DataHub `updateLineage`。

**V2 是第三条轨道**，入口在画布（用户要求），产物同样落 `LineagePackageEdge` 家族，
共用既有的 URN 对齐（`lineage_inventory.resolve`）、preview/apply 分离、逐条失败留档。

---

## 3. 关键判断：血缘 ≠ 关联关系

这是整个方案最重要的一条，先说清楚再谈实现。

仓库自己的口径写在 `relation_structure.py:1-8`：

- `foreign_key` / `bridge_table` / `fact_table` = **业务关联关系**（真实业务事实，ObjectProperty）
- `derivation` = **溯源/派生**（数据血缘，对齐 PROV-O `wasDerivedFrom`），**本身不是业务事实**

「`aj_bl_zl.bl_bh` 与 `aj_cyry_ql.ry_bh` 指同一个人员」是**关联关系**，不是数据流动。
把它写成 DataHub lineage 边，等于声明「案件笔录表的数据加工成了案件从业人员表」——这是假的。

而且这个错误会一路传导，链条可追：

1. 写成 lineage 边 → `evidence_builder.py:338` 读进来，描述形如「血缘：A 加工至 B」
2. → `infer_relation_structure_type()`（`relation_structure.py:23`）看见「血缘」→ 判 `derivation`
3. → `_LLM_RELATION_SYSTEM_PROMPT`（`draft_generator.py:255`）按溯源范畴命名 → 产出「汇总为」「派生出」
4. 而正确答案是「涉及」「记录」「属于」

**所以：智能关系补充的产物必须以「关联关系」身份落地，不能混进血缘图。** 这一条决定了第 5.6 节的落点设计。

> 用户的目标是「解决 DataHub 缺血缘」。诚实的说法是：**孤岛问题会被解决，但解决它的正确姿势
> 不是把关联关系伪装成血缘**。第 5.6 节给出三个落点方案和取舍。

---

## 4. 现有管线里的两个空洞（V2 顺带补上）

### 4.1 基数从来没有被计算过

`evidence_builder.py` 里四处关系构造，基数**全是硬编码常量**：

| 位置 | 值 | 场景 |
| --- | --- | --- |
| `evidence_builder.py:293` | `"many_to_one"` | 声明式外键 |
| `evidence_builder.py:327` | `"many_to_one"` | 源画像推断外键 |
| `evidence_builder.py:356` | `"one_to_many"` | 血缘边 |
| `evidence_builder.py:629` | `"many_to_many"` | 桥表 |

`unique_count` 全库只在 `object_classifier.py:305` 用过一次（确认主键唯一度），**从未用于基数**。
用户这次点名要基数，正好把这个洞补上——而且是**算出来的**，不是猜的（见 5.4）。

### 4.2 关联键采集了，但没有任何消费者

`lineage_package.py:9` 的模块注释写：「关联键留在本地库里——它是给关系推断用的证据」。

**查遍全仓，`LineagePackageEdge.join_key` 的读者只有 API 出参和 MCP 工具，
`evidence_builder` / `draft_generator` / `object_classifier` 一个都没读过。**

也就是说：扫描器辛苦从真实 SQL 里解析出的关联键——**全系统置信度最高的关联证据，
因为它是被真实执行过的 JOIN**——采集完就烂在库里。

V2 建立的消费路径必须同时接上它（见 6.1）。

---

## 5. 方案

### 5.1 管线总览

```
DataHub 域元数据（字段 + 样例值 + distinct + rowCount）
        │  ← 分钟级抓取，必须落盘缓存（复用 draft_evidence_cache 模式）
        ▼
[门 1-6] 机械过滤：4960 字段 → 475 候选键列
        ▼
[聚族]   按值形状签名聚类，跨 ≥2 表 → 15 个键族
        ▼
[算基数] distinct/rows 算每个成员的角色与两两基数（不问 LLM）
        ▼
[LLM]    每族一问：是不是实体键？叫什么？谓词是什么？（不问基数）
        ▼
[候选]   落 relation_candidates 表，state=proposed
        ▼
[画布]   建议层渲染成虚线连边，人确认/改/删
        ▼
[apply]  确认后的边 → 关联关系落点（5.6）
```

**分工原则**：机械信号负责**召回与降噪**，LLM 负责**判定与命名**，人负责**确认**。
基数属于机械层——它是算术，不是判断。

### 5.2 机械闸门（门 1-6，含实测剔除量）

| 门 | 规则 | jwsp 实测剔除 |
| --- | --- | --- |
| 1 | **技术/审计列**：同名列出现在 >25% 的表 → 剔 | 635 |
| 2 | **无画像**：无样例值或无 rowCount → 剔 | — |
| 3 | **形状不稳定**：5 个样例的形状签名不唯一 → 剔 | 3298 |
| 4 | **时间类**：物理类型含 DATE/TIME/TIMESTAMP，或形状匹配日期模板，或值落在 `(19\|20)\d{6}` → 剔 | 319 |
| 5 | **度量/坐标类**：形状为 `N<x>.N<y>`，或列名以 `_cnt/_amt/_je/_sl/_jd/_wd/lnglat` 收尾 → 剔 | 155 |
| 6 | **常量/枚举**：形状签名里**不含任何 `A<>`/`N<>` 占位符**（即样例值全是同一个字面量，如 `汉族`），或 distinct < 5，或形状为 `N<1>`/`N<2>` 的低基数码值 → 剔 | 78 + 常量族 |

> 门 6 的「形状不含占位符」是实测发现的干净判据：`居民身份证`/`中国`/`汉族` 三个噪声族正是这样被
> 一条规则清掉的，不需要词典。

**为什么不复用 `evidence_builder._infer_semantic_type()`（`evidence_builder.py:900`）做闸门**：
它是**命名口径**（`name.endswith("_id")`、`"date" in name`），在拼音列名上整体失效——
`blzz_sj`（笔录制作时间）不含 `time`/`date`/`_at`，会被判成 `attribute`。
本方案的闸门必须自带**值判据**。两套口径的关系应在 7 阶段收敛（见 8「明确不做」）。

### 5.3 键族聚类

```python
@dataclass(frozen=True)
class KeyFamily:
    id: str                      # f_<shape hash>
    value_shape: str             # "A<2>N<8>"
    sample_values: list[str]     # 去重后的代表值（≤10）
    members: list[KeyColumn]     # 跨 ≥2 张表

@dataclass(frozen=True)
class KeyColumn:
    table: str
    column: str
    distinct: int
    rows: int
    ratio: float                 # distinct / rows
    samples: list[str]
```

形状签名：字母段 → `A<n>`、数字段 → `N<n>`，其余字符原样，截断 32 字符。
**同族要求跨 ≥2 张表**（单表内复现不是关系）。

### 5.4 基数由数据算，不由模型猜

对族内任意两个成员 `a`、`b`，用 distinct/rows 判角色：

| 条件 | 角色 | 两两基数 |
| --- | --- | --- |
| `a.ratio ≥ 0.98` 且 `b.ratio < 0.98` | a 是主端 | **1:N**（a → b） |
| 两端 `ratio ≥ 0.98` | 皆唯一 | **1:1** |
| 两端 `ratio < 0.98` | 皆引用端 | **N:M**（经由共享实体） |

输出直接喂 `normalize_cardinality()`（`ontology_types.py:131`）归一到既有枚举，
不新增词汇表。

**jwsp 实测的关键发现：15 个族里「近唯一端」几乎全是 0**——这个域**根本没有人员主表、
没有案件主表**。所以正确的产出不是「A.x → B.y 外键」，而是：

> 「这 40 张表的 52 个列都引用同一个『人员』实体，而该实体在本域没有主表。」

这正好落在仓库既有的建模口径上——`fact-tables-are-verbs` / `task-output-tables-are-landings`：
表是事实/明细，真正的对象在它引用的键上。所以 N:M 族应产出：

1. 一个**共享实体候选**（人员 / 案件 / 设备）
2. 成员表之间的 `bridge_table` 关系（两两，基数 N:M）
3. 一条「**缺主数据表**」发现，供后续接建数任务（本期只提示，不自动建）

### 5.5 LLM 的四件事（不含基数）

**一族一问**，15 个族 = 15 条候选，一次请求装得下（对照：按对是 15244 条）。
沿用 `draft_generator` 的既有姿势：`AsyncOpenAI` + `response_format={"type":"json_object"}` +
`_coerce_llm_response()` 兜围栏（GLM 会加 ```json）。

**入参**（每族）：值形状、代表值、成员列表（表名/列名/distinct/rows/ratio）、表的中文名或注释（若有）。

**出参**：

```json
{"families": [{
  "family_id": "f_a2n8",
  "verdict": "entity_key | dimension_code | not_a_key",
  "entity_name": "人员",
  "key_name": "人员编号",
  "predicate": "涉及",
  "confidence": 0.86,
  "reason": "值为 RY 前缀 + 8 位序号；成员列名 bl_bh/zbr_bh/chujingr_bh 均含「编号」词元；跨 40 张业务表复用"
}]}
```

四件事，都是机械层做不了的：

1. **判真伪**：`N<6>`/`310115` 是行政区划码（`dimension_code`），`A<2>N<8>` 是实体键（`entity_key`），
   漏网的枚举判 `not_a_key`。
2. **命名实体**：从值形态 + 拼音列名读出「人员」——遵循既有铁律
   `object-naming-no-fallback`：**没有 LLM 或输出不合格就报错，不降级、不用 `f_a2n8` 当名字**。
3. **给谓词**：供后续关系命名，口径对齐 `_LLM_RELATION_SYSTEM_PROMPT`（三元组谓词，
   不许「关联」「生成」这类无信息量词）。
4. **给理由**：`reason` 原样进候选行，人在画布上审的就是这句话。

**明确不问 LLM 的**：基数（算术）、URN 对齐（`inventory.resolve`）、成员去留（闸门）。

### 5.6 落点：三个方案与取舍

| 方案 | 写到哪 | 语义 | 代价 |
| --- | --- | --- | --- |
| **(a) DataHub lineage 边** | `updateLineage` | ❌ 错（关联伪装成加工，见第 3 节） | 零新增能力；能立刻让表脱离孤岛 |
| **(b) DataHub `schemaMetadata.foreignKeys`** | REST `ingestProposal` | ✅ 对；读侧 `_parse_schema_fields:213` 已解析，写侧现已读取完整 aspect 后合并回写 | 目标 DataHub 版本仍需非生产实例验证 |
| **(c) ontoMeta 本地关联关系表** | 新表 + 接 `evidence_builder` 第三个 FK 来源 | ✅ 对；留得住来源与理由（DataHub 存不下） | 需改 evidence_builder；DataHub 里仍是孤岛 |

**当前落点：(c) 为本体证据主路径，(b) 已提供显式写回 API；(a) 仍不启用。**

- (c) 是正确落点：关系证据接进 `evidence_builder.py:277-334` 那个已有的 FK 证据缝（声明式 FK / 源画像推断 FK 之后的第三路），带 `confidence` 与 `reason`，本体侧免新增判定口径。
- (a) 单独给一个开关，文案必须直说：**「同时写入 DataHub 血缘图（可让表脱离孤岛，但会把关联关系记成加工关系）」**——不能默认勾选，不能藏。
- (b) 通过 DataHub REST `ingestProposal` 读取完整 `schemaMetadata` 后合并写回，目标实例不支持或缺少 schema aspect 时 fail-closed。
- (a) 仍不启用：它会把关联关系伪装成血缘加工关系。

### 5.7 数据模型

复用 `LineagePackage`（`kind` 增加 `"inference"`）留档一次推断批次，新增候选表：

```python
class RelationCandidate(Base):        # relation_candidates
    id / package_id                    # 归属推断批次
    domain_context_id
    family_id / value_shape            # 键族溯源
    entity_name / key_name             # LLM 命名
    source_table / source_column
    target_table / target_column
    source_urn / target_urn            # inventory.resolve 对齐
    cardinality                        # 算出来的，normalize_cardinality 归一
    structure_type                     # foreign_key | bridge_table
    verdict / confidence / reason      # LLM 判定 + 理由（人审的就是这句）
    state                              # proposed | confirmed | rejected | applied
    decided_by / decided_at            # 人工表态留痕
```

**为什么不塞进 `LineagePackageEdge`**：那张表的语义是「一条血缘边」，字段是
`source_file`/`join_key`/`applied_at`。硬塞会让「边」这个词同时指血缘和关联——正是第 3 节要避免的。

### 5.8 API

```
POST /lineage/domains/{id}/infer-relations     # 跑推断，只落候选不写任何外部系统
GET  /lineage/domains/{id}/relation-candidates # 按族分组返回
POST /lineage/relation-candidates/{id}/decide  # confirm / reject（人工表态）
POST /lineage/domains/{id}/relations/apply     # 已确认键族写回 schemaMetadata.foreignKeys
```

沿用既有 preview/apply 分离与逐条失败留档（`ApplyReceipt`）。

### 5.9 前端：画布上的「建议层」

按用户要求，入口在**画布补录**，不新开第三个 tab（`LineageSupplementPage.tsx:493` 的
`scan | canvas` 模式不变）：

- 画布工具栏加「🔍 智能补充关系」按钮 → 跑推断 → 结果以**虚线 + 建议色**画在画布上，
  与人工实线边视觉分离
- 右侧抽屉按**键族**列出：族名（人员/案件/身份证号）、值样例、成员表、基数、LLM 理由
- 操作：整族接受 / 整族拒绝；确认后作为本体关联证据，可显式点「写回外键」同步到 DataHub
- 族里若「近唯一端 = 0」，抽屉提示「本域缺少该实体的主表」，并给一个跳建数任务的入口（只跳转，不自动建）

复用 `CanvasEdge`（`LineageCanvas.tsx:44`）结构，加 `suggested?: boolean` 与 `familyId?: string`。

---

## 6. 现有血缘补录的优化空间（一并纳入）

按「值 / 代价」排序。前三项与 V2 共享同一条消费路径，建议同期做。

### 6.1 【高】把关联键接上消费者（承 4.2）

扫描器解析出的 `join_key` 是**观察到的真实 JOIN**，置信度高于任何推断，现在无人消费。
V2 的候选表建好后，把 `join_key` 按同一条路径接进 `evidence_builder`，`confidence` 给到最高档
（高于 LLM 推断的族）。**这项不需要 LLM、不需要 DataHub，纯接线。**

### 6.2 【高】DDL 外键约束不解析 —— ⚠️ 实施时发现比这里写的更严重

`_join_keys()`（`sql_lineage_extractor.py:149`）只扫 `exp.EQ`。实测确认：

```sql
CREATE TABLE orders (id INT, customer_id INT, FOREIGN KEY (customer_id) REFERENCES customers(id));
```

sqlglot 会解析出 `exp.ForeignKey` / `exp.Reference` 节点，但 `find_all(exp.EQ)` 返回**空**。

> **本节原先的结论是错的**（写方案时只验了 `exp.EQ` 为空，`sources` 那半句是推断的，
> 没实测）。P4 实施时跑出来的真相是：`_statement_lineage` 用 `find_all(exp.Table)` 取上游，
> 会把 `REFERENCES customers(id)` 里的 `customers` **当成来源表**，于是这条纯建表语句
> 推出一条「customers 加工至 orders」的边。
>
> **所以不是「产出为 0」，是「产出错的」**——一条把外键说成数据流动的假血缘，
> 而且它 `state=ok`、会被上报进 DataHub，然后被 `infer_relation_structure_type` 判成
> `derivation`、被命名成「派生出」。这正是第 3 节那条错误传导链，只不过源头在扫描器自己。

补法两件：上游改成**只从查询体取**（`statement.args["expression"]`），假血缘即消失；
再新增 DDL 外键提取，产出 `kind=relation` 的关联边（**是关联不是血缘**，不上报）。

### 6.3 【中】上报未批量，N 条边 = N 次 GraphQL

`_write_edges()`（`lineage_package.py:434`）按 `(source_urn, target_urn)` 分组后**逐组发一次**
`add_lineage_edge()`。而 `UpdateLineageInput.edgesToAdd` 本身是**数组**
（`datahub.py:1066`）。300 条边 = 300 次串行往返。

补法：`add_lineage_edges(connector, edges: list[tuple[str, str]])` 批量提交（分批 ≤50），
回执仍需按边归因失败——批量失败要能落到具体边上，否则回执会退化成「整批失败」。

### 6.4 【中】blocked 边没有补救路径

`_classify()` 把对不上 URN 的边判 `blocked`，理由是「落点表未在 DataHub 找到」。今天**只能重扫**，
而重扫用的是同一套 `inventory.resolve`，结果一样。真实原因往往是库名前缀差异或表名大小写。

补法：给 blocked 边一个**手工映射**入口（SQL 表名 → URN），映射存在域级别复用，重扫时优先套用。

### 6.5 【中】字段级元数据抓取是分钟级瓶颈，且没有缓存

`lineage_inventory` 特意走轻查询（`fetch_domain_dataset_index`，实测 ~6s），因为
`fetch_domain_bundle` 对 ERP 域要跑 ~7 分钟（`draft_evidence_cache.py:1-8` 记着这条）。
**V2 恰恰需要全域字段 + 样例值**——必须落盘缓存，否则每次点「智能补充」都等几分钟。

补法：抽出与 `draft_evidence_cache` 同构的 `datahub_bundle_cache`（TTL + `datahub_domain_id`
fingerprint 双失效，任何异常降级为 miss），两个特性共用。**这是 V2 的前置项，不是可选优化。**

### 6.6 【低】重扫会删掉未上报的边

`rescan()`（`lineage_package.py:249-251`）删除所有 `applied_at is None` 的边再重建。
今天这些边没有人工编辑能力，所以无损。但 V2 引入「人工确认/修改候选」后，
同样的语义会**抹掉人的表态**——候选表必须自带保护（`state != proposed` 的行不参与重算）。

### 6.7 【低】孤岛口径与「有血缘」口径过窄

`InventoryTable.isolated`（`lineage_inventory.py:49`）= 上下游皆空。V2 落地后，
一张表可能**关联关系很丰富但血缘仍为空**——它在补录页仍显示为孤岛，在本体侧却已经有关系了。
补法：概览页把「孤岛」拆成「无血缘」与「无任何关系（血缘 + 关联皆空）」两个数。**已实现**：
`DomainOverview.no_lineage` 与 `no_any_relation`，关系证据按域内唯一裸表名归一。

---

## 7. 阶段与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P0** ✅ | 6.5 元数据落盘缓存 + 6.1 关联键接线 | 二次点击秒回；扫描出的 join_key 能在本体证据里看到 |
| **P1** ✅ | 闸门 + 聚族 + 基数算术（纯机械，无 LLM） | jwsp 域跑出 15 ± 3 个族；身份证/手机号/人员编号/案件编号四族必须在列；无一个常量族 |
| **P2** ✅ | LLM 判定 + 候选落库 + API | 15 个族一次请求完成；`居民身份证`/`中国`/`汉族` 全判 `not_a_key`；`N<6>` 判 `dimension_code` |
| **P3** ✅ | 画布建议层 + 人工确认 + apply 到 (c) | 端到端：点按钮 → 看到虚线 → 确认 → 本体侧关系证据出现 |
| **P4** ✅ | 6.2 DDL 外键 + 6.3 批量上报 + 6.4 手工映射 | 纯 DDL 包产出 > 0；300 条边上报往返数 < 10 |

**回归底线**：现有 `tests/test_lineage_package.py`、`test_lineage_inventory.py`、
`test_sql_lineage_extractor.py`、`test_mcp_lineage.py` 全绿；新增闸门与基数算术必须有
**用真实 jwsp 形态数据**构造的用例（常量族、时间戳族、低基数码值族各一条反例）。

### P0 / P1 落地记录（2026-09-07）

**新增**：`services/key_family.py`（闸门+聚族+基数，纯函数）、`services/observed_joins.py`
（关联键读取与解析）、`services/datahub_bundle_cache.py`（原始 bundle 落盘缓存）、
`GET /lineage/domains/{id}/key-families`（只读，让 P1 的产出在产品里看得见）。
**改动**：`evidence_builder`（`observed_joins` 入参 + `_merge_observed_joins` + `_fk_cardinality`）、
`source_profile.InferredFk`（加 `target_column`/`origin`/`confidence`）、
`draft_task_service` 与 `unmodeled_tables`（传关联键 + 换 fingerprint）。

真机验收（jwsp，138 表 / 4968 字段）：

| 项 | 结果 |
| --- | --- |
| P0 缓存 | 冷启 **210.8s** → 二次 **0.04s**（HTTP 端到端 0.14s），4771× |
| P0 关联键 | 真实包 `gflow.zip` 解析出 223 条，落地 **5 条**关系（该域此前关系数为 **0**）；其余因两端表不在本域被丢弃 |
| P1 族数 | **12**（验收线 15±3） |
| P1 四族 | 身份证 `N<18>` 21 列 / 手机号 `N<11>` 35 列 / 人员编号 `A<2>N<8>` 90 列 52 种列名 / 案件编号 `N<16>` 16 列 |
| P1 常量族 | 0（`汉族`/`中国`/`居民身份证` 被「形状无占位符」清掉） |
| 基数 | 算出来的：`sfz_hm = sfz_hm` → `many_to_many`，`xs_id = xs_id` → `one_to_one` |
| 回归 | 1965 passed, 1 skipped；ruff 全绿 |

### P2 落地记录（2026-09-07）

**新增**：`services/key_family_verdict.py`（LLM 判定，一次请求判完整批族）、
`services/relation_inference.py`（编排 + 落候选 + 展开两两关系）、
`models/relation_candidate.py` + 迁移 `8d4d847200c9`、
`services/llm_json.py`（从 `draft_generator` 抽出的 JSON 解析口径，两处共用）。
**API**：`POST /lineage/domains/{id}/infer-relations`、
`GET /lineage/domains/{id}/relation-candidates`、
`POST /lineage/relation-candidates/{id}/decide`。

真机验收（jwsp + 自建 `glm-5.2-fp8`，12 个族一次请求判完）：

| 族 | 判定 | 命名 | 判据（模型原文摘要） |
| --- | --- | --- | --- |
| `N<18>` | entity_key | 自然人 / 身份证号 | 18 位数字前 6 位为地区代码 |
| `A<2>N<8>` | entity_key | 人员 / 人员编号 | RY 前缀 + 8 位序号，列名多以 bh(编号) 结尾 |
| `N<16>` | entity_key | 案件 / 案件编号 | 16 位数字，前缀含行政区划代码 |
| `N<11>` | entity_key | 手机号 / 手机号码 | 11 位以 1 开头 |
| `N<15>` | entity_key | 移动设备 / IMEI | 15 位以 86 开头 |
| `N<20>` | entity_key | SIM卡 / ICCID | 20 位以 8986 开头 |
| `A<2>N<16>` | entity_key | 警情 / 警情编号 | JQ 前缀 |
| `N<10>` | entity_key | 网络账号 / QQ号 | 列名 qqid |
| `N<6>` | **dimension_code** | 行政区划 | 「连接的是码表而非具体实体」 |
| `A<6>` | **not_a_key** | — | IDCARD 等证件类型英文枚举常量 |
| `A<7>` | **not_a_key** | — | 「疑似脱敏数据或随机串」 |
| `N<3>` | **not_a_key** | — | 「混聚了证件类型码与国家代码等不同体系」 |

验收全 PASS。API 端到端：`GET` 0.40s 返回 8 条 entity_key 候选，`decide` 记下表态并展开
208 对（含算出来的基数与 `bridge_table`/`foreign_key` 结构类型）。

**两条真机才暴露的事，直接影响 P3**：

1. **LLM 这一步 ~430s**（bundle 已命中缓存，全部耗时在模型侧；自建 glm-5.2-fp8）。
   而 `llm_timeout_seconds` 是 300s——**中途超时后整轮重来**，叠加 `max_retries=5`
   最坏能烧十几分钟。已给这一步单独的 `key_family_verdict_timeout_seconds`（默认 900s）
   并把耗时打进日志。但结论不变：**P3 的画布按钮不能同步等**，必须走既有的
   `DraftGenerationTask` + 队列 + 进度轮询那套异步模式。
2. **模型指出了机械层的一个真实缺陷**：`N<3>` 被判 not_a_key，理由是
   「混聚了证件类型码(zj_lx_dm)与国家代码(gj_dm)等不同体系」——即**同形状不等于同值域**。
   短数字形状尤其容易撞。可考虑在 P4 给短形状加一道「列名词元需一致」的细分闸门。

### P3 落地记录（2026-09-07）

**新增**：`models/relation_inference_task.py` + 迁移 `d7b5276879d6`（异步任务）、
`components/lineage/RelationSuggestionDrawer.tsx`（建议抽屉）。
**API**：`POST .../infer-relations` 改为**异步**（返回任务）、`GET /lineage/inference-tasks/{id}`、
`GET .../inference-task`（该域最近一次）。
**改动**：`observed_joins` 增加 `origin`/`confidence` 与 `load_relation_evidence`（两来源合流）、
`evidence_builder` 按 origin 分开渲染描述、`LineageCanvas` 支持 `suggested` 虚线边与只读检视、
`database.init_db` 增加推断任务的陈旧回收。

**落地方式与方案 §5.8 不同（简化）**：没有单独的 `relations/apply` 端点。落点 (c) 是
「本地候选接进 evidence_builder」，而 P0 已经为代码包关联键铺好了那条路——所以
**「确认」本身就是落地**，人一点确认，下次生成草稿就会带上这批关系。`applied` 状态
留给写回 DataHub（P4）。

真机端到端（jwsp，确认「案件编号」一个族）：

| 环节 | 结果 |
| --- | --- |
| 抽屉 | 12 个族按判定分三组渲染，判据原文可读，`not_a_key` 不给确认按钮 |
| 确认 | 「已确认「案件编号」，120 条关系将参与本体生成」 |
| 画布 | 3 张表上画布后出现 **3 条绿色虚线**（`stroke-dasharray="3 3"`），检视面板为只读 |
| 本体证据 | 关系数 **0 → 111**（其中 105 条来自已确认键族），描述为「智能关系补充推断，已人工确认：…案件编号」 |
| 分类影响 | business_object **0 → 21**——该域此前 138 张表全是孤岛、全判 data_table |

**一个真机才暴露的数据形态坑**：画布节点名来自 `lineage_inventory`（``库.表``，
如 `jwsp.aj_bl_zl`），候选成员名来自 `fetch_domain_bundle`（裸名 `aj_bl_zl`）——
**同一个 DataHub 的两个查询给的表名形态不一样**。第一版按全名匹配，虚线一条都画不出来。
已改为按裸名匹配、且只在画布上裸名唯一时才认（与 `lineage_inventory.resolve` 同口径）。
后端的证据合流路径本来就做了这层归一（`_merge_observed_joins`），所以只有前端漏了。

**已知取舍**：推断跑在 API 进程里的 `asyncio` 任务，**开发热重载会打断它**（由启动时的
陈旧任务回收兜底，用户重点一次即可）。要做到 reload 免疫得像草稿生成那样抬进子进程。

### P4 落地记录（2026-09-07）

**新增**：`models/lineage_table_mapping.py` + 迁移 `db3055d82d31`（人工表名映射）、
`components/lineage/TableMappingModal.tsx`；`LineagePackageEdge.kind` 列 + 迁移 `c086109d5cb1`。
**改动**：`sql_lineage_extractor` 提取 DDL 外键并修正上游取法、`datahub.add_lineage_edges`
批量提交、`_write_edges` 批发 + 失败退回逐条定位、`_classify` 吃人工映射、
`ScanReport` 给 blocked 边加「指定对应表」入口。

**后续补齐**：关系感知的概览指标、宽族降置信、无主表告警与建数入口；已确认键族通过
DataHub REST `ingestProposal` 合并写回 `schemaMetadata.foreignKeys`，并新增
`POST /lineage/domains/{id}/relations/apply`。

| 验收项 | 结果 |
| --- | --- |
| 纯 DDL 包产出 | 3 条语句 → **4 条外键、0 条血缘**（三种写法：表级约束 / ALTER ADD / 列级内联） |
| 假血缘 | 旧口径上游 = `['customers']`（假边），新口径 = `[]` ✅ |
| 上报往返 | 300 条边 → **6 次** GraphQL（批大小 50），验收线 < 10 |
| 人工映射 | 真机 jwsp：映射 `d0` → `jwsp.cjdxx_zl`，**当场修复 1 条** blocked 边 |

**这一阶段最有价值的产出不是三个优化，是发现 6.2 的结论写错了**：方案里说纯 DDL 包
「产出为 0」，实际是**产出了一条假血缘**（把 `REFERENCES` 的被引用表当成了上游）。
写方案时只实测了 `exp.EQ` 为空，`sources` 那半句是推断的——推断进了文档，就成了「事实」。
§6.2 已就地更正并标注。

**实施中修的第二个细节**：两端都对不上的边，映射了其中一端后仍是 blocked，但理由必须
从「落点表未找到」换成「上游表未找到」。不换的话人映射完看见的还是原来那句，
会以为映射根本没生效。

**顺手修的阻塞性 bug（非本方案范围）**：`recover_stale_draft_tasks` 拿 Postgres 的
`func.now()`（**带时区**）与 naive 的 `updated_at` 比较抛 `TypeError`。它挂在 `init_db()`
启动钩子上，异常直接让**服务起不来**（实测 uvicorn `Application startup failed`），
触发条件正是它自己要处理的场景——库里留着一条 running 任务时热重载。本地 SQLite 的
`now()` 是 naive，所以一直没暴露。已修并补上会失败的回归用例。

---

## 8. 明确不做

- **不做实时源库采样**。样例值全部来自 DataHub profiling（实测 100% 覆盖），不新增源库连接。
- **不做值集合真交集判定**。5 个样例的交集在低基数列上全是噪声（实测 980 组假阳性），
  只用形状 + 基数；交集**仅在高基数族内**作为加分项，且不作为唯一判据。
- **不自动建主数据表**。「缺主表」只提示，建表走既有建数任务，由人发起。
- **不合并两套语义类型口径**。`_infer_semantic_type()` 是命名口径、本方案是值口径，
  两者服务不同阶段；强行合并会把建模期的判定提前到补录期。收敛留给后续专项。
- **不撤销已写入 DataHub 的边**。与现有 `delete()` 口径一致（`lineage_package.py:299-305`）。
- **不让 LLM 给基数**。基数是算术。

---

## 9. 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **形状签名过粗** | `N<6>` 同时命中行政区划码与任何 6 位数字编号 | LLM 的 `verdict` 就是为这层设计的；族内成员表跨度过大（>50% 表）时自动降置信并保留人工确认 |
| **desensitized 数据** | 实测 `zbr_sfz_hm` 样例是 `0E1PXW73`（脱敏），格式正则会失效 | 方案不依赖格式正则，依赖**形状一致性**——脱敏值形状同样一致，会自成一族，不会误并入真身份证族 |
| **LLM 命名不合格** | 拼音列名可能让模型编造实体名 | 遵循 `object-naming-no-fallback`：不合格即报错，不降级；`reason` 必须给出判据，人审时能识破 |
| **一次全域推断成本** | 族数虽小，但前置的全域字段抓取是分钟级 | 6.5 落盘缓存为前置项；推断按域触发，不做自动定时 |
| **写入 (a) 污染血缘语义** | 见第 3 节 | 默认关闭 + 文案直说 + (b) 就绪后废弃 |
| **候选表膨胀** | N:M 族两两展开会产生大量行（15 族 = 15244 对） | **候选表按族存，不按对存**；两两关系在 apply 时按需展开，且展开前必须有人确认整族 |

---

## 10. 调查锚点（本方案的事实来源）

| 结论 | 锚点 |
| --- | --- |
| 孤岛 100%、0 PK/FK、100% profiled | 生产 DataHub `100.93.8.29:8080` jwsp 域只读探针（2026-09-07） |
| 15 个键族及其成员 | 同上，闸门 + 聚族实测 |
| 两两配对产出 26178 对 | 同上，第一版漏斗实测（已废弃） |
| 血缘 ≠ 关联的语义分叉 | `relation_structure.py:1-8`、`:23-42` |
| 错误传导链 | `evidence_builder.py:338` → `relation_structure.py:23` → `draft_generator.py:255` |
| 基数四处硬编码 | `evidence_builder.py:293,327,356,629` |
| `unique_count` 仅一处消费 | `object_classifier.py:305` |
| `join_key` 无消费者 | 全仓 grep：仅 `api/lineage.py`、`mcp/tools/lineage.py` 出参 |
| DDL 外键不解析 | `sql_lineage_extractor.py:149,206` + sqlglot 实测（`exp.EQ` 空、`exp.ForeignKey` 有） |
| 上报未批量 | `lineage_package.py:434` vs `datahub.py:1066`（`edgesToAdd` 是数组） |
| 抓取分钟级 / 缓存先例 | `lineage_inventory.py:1-13`、`draft_evidence_cache.py:1-8` |
| FK 证据缝位置 | `evidence_builder.py:277-334` |
| 读侧已解析 foreignKeys | `datahub.py:213-224`（`_parse_schema_fields`） |
| 命名口径在拼音列上失效 | `evidence_builder.py:900-935`（`_guess_semantic_type` 只看英文命名） |
| 画布边结构 | `LineageCanvas.tsx:44`、页面模式 `LineageSupplementPage.tsx:493` |
