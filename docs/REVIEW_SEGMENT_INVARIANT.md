# 审核收口：已判可回看 + 角色决定板块

> 2026-09-03 修订：取消「待归类业务对象」板块与它的确认门禁。原门禁方案见本文
> §附录·被取代的方案。

## 问题

审核台存在两个致命漏洞，都在同一件事上：**判定不可逆、判错了找不回来**。

1. **判完的板块消失了**
   一个板块判完之后从队列里彻底消失。判错了只能靠记忆去翻卡片墙——那等于没有复查机制。

2. **中间地带吞掉了一半的表**
   判成业务对象、却在关系图上连不成簇的表会落进「待归类业务对象」板块（实测 erpnext
   547 个）。这个板块既不是业务模块（进不了业务地图），也不是明确的非业务（不知道该
   拿它怎么办）。第一版方案用**门禁**堵住出口：不归位就不许确认。结果是人看到一个被
   禁掉的按钮，而那个板块仍然不会变空——**门禁堵的是症状，中间地带本身才是病**。

## 解决方案

### 1. 已判可回看

队列不是单向的——服务端在「待判 + 已判」的完整人口上分组，两个视图因此是**同一批组、
同一套 key**，换的只是成员的那一半。

- `get_review_queue` 的 `status` 参数：`"pending"`（默认）/ `"reviewed"`
- 分组逻辑不变，过滤条件相反；两个视图都给 `pending_total` / `reviewed_total`
- 判完的板块点进去自动切到 `status=reviewed`，空状态交叉引导
- 已判视图里主动作（`A` 键）换成**退回复核**
- 撤销记录板块归属，还原时一起挪回去

### 2. 角色决定板块，没有第三态

**划分只有三类**（[`services/segment_kinds.py`](../backend/app/services/segment_kinds.py)）：

| kind | 是什么 |
| --- | --- |
| `business` | 聚类得出的业务模块 |
| `shared` | 公共主数据（枢纽对象，处处被引用，刻意不并入单个模块） |
| `system` | 不是业务对象/关系表的一切：框架管道表、数据库自带 schema，以及归不进任何业务模块的表 |

规矩一句话：**是业务对象或业务关系表的，一定落在某个业务板块下；其余的落系统表。**

#### 落位是级联的（[`services/segment_placement.py`](../backend/app/services/segment_placement.py)）

先命中先归属：

1. 数据库自带 schema → 系统表（压根不该进本体）
2. 非业务角色（数据表 / 技术表）→ 系统表
3. 枢纽对象 → 公共主数据
4. **邻居投票**：关系对端已经在某个业务板块里，就跟着去（最强信号，图上真的连着）
5. **命名族亲和**：同族的表已经在某个业务板块里，就跟着去
   （`tabSales Invoice Item` 跟着 `tabSales Invoice` 走）
6. 都兜不住 → 系统表，由人在审核台上移出来

第 4、5 步就是取代「待归类」的东西：机器不再把推不动的对象挂起，而是尽量给它一个真实
的归属。实测 erpnext（回填脚本）：584 个原待归类对象里 **240 个被亲和收编进业务模块**，
其余落系统表。

草稿侧（`segment_generator.build_fallback_segments`）与库内侧
（`segment_placement.place_unsegmented`）跑的是同一套级联，生成、创建、回填三条路
落位一致。

#### 第 6 步不是又一个垃圾桶

业务角色却落在系统表里的对象由 `stranded_in_system` 数出来：

- 队列组带 `ReviewGroup.stranded_in_system`（服务端算，前端不重算）
- `ReviewModeStats.stranded_total` / `stranded_reviewed`
- 批量判定返回 `ObjectTypeBatchUpdateResult.stranded_in_system`

**判定不因此被拒**。确认照常成立，只是回执如实说「其中 N 个还在系统表里待移出」，
审核台把「移动到板块」那一行高亮成主动作。这是与第一版方案最大的区别：
门禁换成了可见性。

#### 角色变了，板块跟着变（`resettle_by_role`）

判定时（不要求角色**变了**）重新落位：

- 改判数据表 / 技术表 → 移到系统表，**业务板块里也不例外**（只往里补不往外清，
  业务地图会慢慢混进一堆管道表）
- 改判业务对象 / 关系表 → 走亲和归位去业务板块；已经在业务模块里的不动
  （聚类/人工的归属不该被一次改判推翻）
- 枢纽的归宿恒是公共主数据，不参与亲和投票（它被处处引用，投票几乎总能给出一个板块，
  进去就把大半张图粘成一块）
- **人工钉过板块的对象一律不动**（`overridden_fields` 含 `segment_id`）——手动
  「移动到板块」是人的判断

### 3. 分错了能移出来

「移动到」是**常驻动作**，不再只在某一组出现：分错板块的对象哪一组里都可能有。

- 目的地是**所有**板块，系统表也在内（业务板块按成员数倒序在前，系统表垫底）。
  只给业务板块的话，误判成业务对象、被人移进销售的技术表就再也退不回系统表。
- 「移动并确认」= `segment_id` + `needs_review=false` 一次请求，后端先挪后判
- 归错地方的那一组（`stranded_in_system`）把这一行高亮，并给说明条

### 4. 待判用红色上标

「还剩多少要判」是审核台上唯一持续变化的数字，混在灰色的 `12/40` 里读不出来。
提成红色上标（`.review-sup`）之后，扫一眼就知道哪几块还没判完；判完不留占位。

出现位置：顶层 tab（对象 / 关系）、次级 tab（外键 / 关系表）、待判/已判切换器、
侧栏每个板块、全部板块、未接入板块。

### 5. 术语：三元组 → 外键

界面上「关系三元组」一律改叫「外键关系」——关系本来就是从外键推出来的，
「三元组」是建模行话。改的是全站用户可见文案（审核台 tab 与列名、关系详情页、
对象档案、关系组列表、动词建议抽屉）。

组件标识名 `RelationTriples` 保留（它渲染的形状确实是主谓宾），文件头注明术语映射。
`冲突三元组`（base/ours/theirs）与 F3 的`拒绝三元组`是无关概念，不动。

## 存量迁移

两个已废弃的板块 kind（`pending` / `technical`）在 `segment_kinds.LEGACY_KINDS` 里
保留常量，只为迁移与兼容读；判定链路一律不认。

**自动**：重新生成本体时 `ontology_merge` 调 `dissolve_legacy_segments`，
拆掉旧板块并由兜底落位重新归位。

**手动回填**（存量本体）：

```bash
python -m scripts.backfill_segment_partition --dry-run   # 预览
python -m scripts.backfill_segment_partition             # 执行
```

三步：拆旧板块 → 补齐未归属（级联落位）→ 修正落位漂移（角色与板块不符的）。
dry-run 与真跑走同一段判定代码（`resettle_by_role(apply=False)`），预演的数就是真跑的数。

实测（erpnext + odoo 两个本体，1069 个对象）：

```
拆掉已废弃板块，共腾出 1069 个对象
补齐合计： system=829  business=240
挪位合计： business→system=1  system→shared=7
校验：仍未归属 0 个对象（应为 0）
```

## 测试覆盖

`tests/test_review_classification_gate.py`（13 个用例）：

**已判可回看**：同一批组同一套 key、判完的板块仍能打开、已判可退回复核

**落位不变量**：
- 归不进业务模块的业务对象可以被确认，但回执报出 `stranded_in_system`
- 「移动到销售 + 确认」是一次调用，且钉住 `overridden_fields`
- 改判数据表 → 落系统表
- 技术表改判业务对象 → 命名族亲和直接落进对应业务模块（不钉 overridden）
- 无亲和信号的 → 留系统表，回执报 1
- 非业务角色从业务板块被清出去
- 人工移过板块的对象不被改判推翻

**统计口径**：`stranded_total` / `stranded_reviewed`、板块 kind、组的
`stranded_in_system`（技术表待在系统表里不算归错地方）

`tests/test_segment_partition.py`（13 个用例）覆盖草稿侧级联：邻居收编、命名族收编、
非业务角色永不进业务板块、全覆盖不重叠。

## 关键文件

### 后端
- `app/services/segment_kinds.py` — 三类 kind、`is_business_role`、废弃常量
- `app/services/segment_placement.py` — 级联落位、`AffinityIndex`、`resettle_by_role`、
  `stranded_in_system`、`dissolve_legacy_segments`
- `app/services/segment_generator.py` — 草稿侧同款级联（`build_fallback_segments`）
- `app/services/edit.py` — 判定时重新落位、批量回执
- `app/services/ontology_query.py` — 队列 `status`、组的 `stranded_in_system`、统计
- `app/services/ontology_merge.py` — 合并时拆旧板块
- `app/schemas/ontology.py` — 返回值与队列组字段

### 前端
- `frontend/src/pages/ReviewWorkbenchPage.tsx` — 切换器、常驻移动行、红色上标、外键改名
- `frontend/src/components/OntologyOverviewPanel.tsx` — 板块目录只剩两类兜底
- `frontend/src/types.ts` — `SegmentKind`、`ReviewGroup.stranded_in_system`
- `frontend/src/styles/layout.css` — `.review-sup`、`.review-actions-row--urgent`

### 脚本
- `backend/scripts/backfill_segment_partition.py` — 拆旧 + 补齐 + 自愈

## 影响范围

**破坏性变更**：

- `SegmentKind` 去掉 `pending` / `technical` 两个取值（前端类型同步收窄）
- `ReviewGroup.requires_classification` → `stranded_in_system`（语义反转：
  前者是「不许确认」，后者是「归错了地方」）
- `ObjectTypeBatchUpdateResult.pending_classification` → `stranded_in_system`
- `ReviewModeStats.unclassified_*` → `stranded_*`
- 确认接口不再返回 400（原待归类门禁）
- 改判非业务角色会把对象移出业务板块（此前只动兜底板块）

**存量数据**：需跑一次回填脚本，或重新生成本体自动完成。

## 附录 · 被取代的方案（2026-08-30 ~ 09-03）

第一版用门禁堵出口：`_assert_classified_or_rollback` 在 `needs_review=False` 时拒绝
所有仍在 `pending` 板块里的对象，前端把确认按钮禁掉并给说明条，顶栏挂「待归类未归位」
芯片捞存量。

它解决的问题是真的（这批对象确实没判完），但代价是把一个机器解决不了的问题推给了人：
547 个对象要人一个个挑板块。现在机器先用邻居 + 命名族收编掉大部分（实测 41%），
剩下的以「归错地方」的形式可见但不阻塞——**能自动的自动，剩下的说清楚，不拿门禁堵人**。
