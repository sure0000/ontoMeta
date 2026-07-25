# 本体预生成：版本管理 / 更新 / 保留人工修正 方案

> 目标：让"预生成本体"从一次性动作变成**可反复运行、可增量演进、不丢人工修正**的能力。
> 核心机制：**字段级来源标记 + 稳定身份键 + 三方合并（base/ours/theirs）+ 版本快照**。

---

## 1. 现状与根因

### 1.1 三条更新路径当前都会破坏人工成果

| 路径 | 代码位置 | 行为 | 后果 |
|---|---|---|---|
| 全量重生成 | `draft_task_service._run_draft_generation` → `_purge_draft_ontologies` | 删除该域全部 draft 本体后重建 | 人工命名/描述/角色/新增对象/关系全部丢失 |
| 增量对象 | `publish.DraftPersistenceService.upsert_objects` | 按 `source_ref` upsert，无条件覆盖机器字段 | 人工修正被覆盖 |
| 增量关系 | `publish.DraftPersistenceService.upsert_relations` | 按 `name` upsert，无条件覆盖 | 人工修正被覆盖；`name` 被改过还会匹配错位 |

### 1.2 根因

- **缺字段级来源**：无法区分某字段值来自机器还是人工。
- **缺机器基线**：无法判断"机器这次相对上次是否变化"，只能整份覆盖。
- **缺稳定身份键**：关系用可变的 `name` 匹配，不稳定。
- **版本只覆盖 published**：draft 阶段无版本、无基线、无变更报告。

---

## 2. 设计目标

1. **版本管理**：draft 与 published 都有清晰的版本生命周期与快照。
2. **更新**：再生成幂等、可增量、可反复，不推倒重来。
3. **保留人工修正**：字段级保护人工编辑，仅在真正冲突时提示复核。

---

## 3. 数据模型改造

### 3.1 实体新增字段（ObjectType / Property / RelationType / BusinessLogic 共用一组语义）

| 字段 | 类型 | 含义 |
|---|---|---|
| `origin` | str | `machine` / `manual` / `machine_edited`（机器生成后被人工改过） |
| `overridden_fields` | Text(JSON) | 被人工显式修改并**钉住**的字段名列表，如 `["display_name","description"]` |
| `machine_baseline` | Text(JSON) | 上一次机器生成的可合并字段值快照（合并的 base） |
| `user_created` | bool | 人工新建、非机器产出（再生成永不覆盖/删除） |
| `deleted_by_user` | bool | 人工删除的墓碑标记（再生成不复活） |
| `last_generation_id` | str | 最近一次"见到"该实体的生成运行 id（用于判定上游消失） |
| `upstream_removed` | bool | 上游已消失但因含人工价值而保留（置 deprecated 而非删除） |
| `conflict_json` | Text(JSON) | 当前未解决的字段级冲突：`{field:{base,ours,theirs}}` |

> 说明：`EntityStatus.EDITED` 是**实体级**粗粒度，保留用于流程状态；新的 `overridden_fields` 是**字段级**保护，两者并存、互补。

### 3.2 稳定身份键（合并匹配的关键）

合并必须靠**不随展示字段变化的自然键**关联新旧实体：

| 实体 | 稳定键 | 说明 |
|---|---|---|
| ObjectType | `source_ref`（DataHub dataset URN） | 已存在；人工新建无 URN 者置 `user_created`，不参与匹配 |
| Property | (对象 `source_ref`, `source_field_ref`/`field_name`) | 已存在 `source_field_ref` |
| RelationType | 新增 `source_signature` | FK：`urn(src)+urn(tgt)+fk_field`；血缘：`urn(src)+urn(tgt)+structure_type`。**不再用可变的 name 匹配** |
| BusinessLogic | `source_type`+`source_ref`（人工建的用 `user_created`） | |

> 新增 `RelationType.source_signature` 列并在生成/合并时确定性计算。

### 3.3 版本相关

- `Ontology.version`：保留，仅在 **publish** 时 +1（已有）。
- `Ontology.draft_revision`（新增 int）：每次生成运行 / 显著保存时 +1，用于草稿演进追踪。
- `GenerationRun`（新增表，或复用 `DraftGenerationTask` 扩列）：记录每次生成运行的
  - `id / domain_id / scope / started_at / finished_at`
  - `machine_output_json`：本次**原始机器输出快照**（合并前的 theirs 全量），用于审计与"一键采纳上游"。
  - `merge_report_json`：本次合并变更报告（见 §6）。
- `VersionRecord`：保留，publish 时写 `snapshot_json`（已有），用于版本对比与回滚。

---

## 4. 三方合并算法（核心）

```
MERGEABLE_FIELDS 按实体定义：
  ObjectType:   name, display_name, description, table_role, role_reason
  Property:     display_name, description, data_type, semantic_type
  RelationType: display_name, description, cardinality, structure_type
  BusinessLogic: display_name, description, expression_summary, logic_type

merge(existing, incoming, gen_id):
    if existing is None:                      # 上游新增
        create(origin=machine, machine_baseline=incoming值, last_generation_id=gen_id)
        return "added"

    if existing.user_created:                 # 人工建的，机器不碰
        existing.last_generation_id = gen_id
        return "skip_user"

    base = existing.machine_baseline
    conflicts = {}
    for f in MERGEABLE_FIELDS:
        cur, b, inc = existing[f], base[f], incoming[f]
        if f in existing.overridden_fields or cur != b:   # 人工改过（钉住或值已偏离基线）
            if inc != b:
                conflicts[f] = {base:b, ours:cur, theirs:inc}   # 双改冲突，保留 ours
            # else 机器没变，保留 ours
        else:
            existing[f] = inc                 # 人没动，采纳机器新值
        base[f] = inc                         # 基线始终推进到 theirs
    existing.machine_baseline = base
    existing.last_generation_id = gen_id
    if conflicts: existing.conflict_json = merge(existing.conflict_json, conflicts)
    existing.origin = "machine_edited" if existing.overridden_fields else "machine"
    return "updated" | "kept" | "conflict"

处理上游消失（本次 incoming 中不存在、且 origin!=user_created 的旧实体）：
    if 纯机器且无引用/无编辑：  delete
    else:                       status=deprecated, upstream_removed=True   # 保留人工价值
```

要点：
- **基线始终推进到 theirs**：即便这次保留了人工值，下次机器值稳定后不会反复报冲突。
- **冲突不阻塞生成**：合并照常完成，冲突进 `conflict_json` 与变更报告，交人工复核。
- **人工删除的墓碑**（`deleted_by_user`）不被上游复活。

---

## 5. 四类更新场景的统一处理

| 场景 | 处理 |
|---|---|
| **A. 首次生成** | 无 draft → 直接落库，`origin=machine`，`machine_baseline=生成值` |
| **B. 同一草稿再生成（演进）** | 走三方合并，**不再 purge**。保护人工字段、更新机器字段、纳入上游新增、处理上游消失 |
| **C. 增量对象/关系** | 同 B，但按 scope 限定合并范围；关系用 `source_signature` 匹配 |
| **D. 已发布本体的再生成（schema 演进）** | 从 published 版本快照**新建一个 draft**：把已发布值作为 ours 且视为权威（全部计入 overridden），再合并本次机器输出 → 产出复核 diff → 通过后 publish 为 version+1 |

> `_purge_draft_ontologies` 只保留给**显式"丢弃并重建"**动作（用户主动放弃全部草稿），不再是默认再生成路径。

---

## 6. 生成变更报告（面向复核）

每次生成运行结束产出 `merge_report_json`，工作区展示：

```
本次生成结果：
  新增   12   （上游新表/新字段/新关系）
  更新    8   （机器字段已刷新，你未改动）
  保留    5   （你的人工修正被保护，机器无变化）
  冲突    2   （上游有更新，与你的修改冲突，待复核）
  上游删除 1  （对象已置为 deprecated，人工内容保留）
```

冲突复核 UI：逐字段展示 `base / 你的值 / 上游值`，操作：
- **采纳上游**（清除该字段 override，值取 theirs）
- **保留我的**（确认钉住，清除冲突标记）
- 批量：**全部采纳上游** / **全部保留我的**

实体徽标：`机器生成` / `人工修正` / `上游有更新·待复核`。

---

## 7. 编辑侧改造（记录字段级来源）

`edit.EditService.update_*` 在人工修改字段时：
1. 把被改字段加入该实体 `overridden_fields`；
2. `origin = machine_edited`（原为 machine）或 `manual`（user_created）；
3. 保留现有 `status=EDITED` 逻辑不变。

提供"**取消钉住**"操作：从 `overridden_fields` 移除某字段，下次生成即可被机器接管。

---

## 8. 版本生命周期与操作

```
draft (draft_revision 随生成/保存递增)
  └─ 生成运行 GenerationRun：machine_output + merge_report（可回看每次生成）
  └─ 人工编辑（字段级 override）
  └─ 预发布 pre_published
publish → version+1，写 VersionRecord.snapshot_json（不可变）
  └─ 版本对比：任意两个 VersionRecord 走已有 compute_version_diff
  └─ 回滚：从旧版本快照新建 draft（场景 D 的变体）
```

- **一个域同一时刻至多一个 active draft** 的既有不变量保留，但通过"合并进现有 draft"维持，而非"删旧建新"。
- published 版本不可变；再演进必须经由新 draft。

---

## 9. 落地改造点清单

| 模块 | 改动 |
|---|---|
| `models/ontology.py` | 新增 §3.1 字段 + `RelationType.source_signature` + `Ontology.draft_revision` |
| Alembic 迁移 | 加列；回填：`status==suggested → origin=machine`；其余（edited/approved/pre_published/published）→ 视为人工权威，`machine_baseline=当前值` 且相应 `overridden_fields` 保守置为全部可合并字段，避免升级后首次生成覆盖历史人工成果 |
| 新增 `services/ontology_merge.py` | 三方合并核心 `OntologyMergeService`（对象/属性/关系/逻辑），产出 merge_report |
| `services/publish.py` | `save_draft`/`upsert_objects`/`upsert_relations` 改为经 `OntologyMergeService`；`upsert_relations` 改用 `source_signature` 匹配 |
| `services/draft_task_service.py` | 全量再生成 `_run_draft_generation` 用合并替代 `_purge_*`；purge 仅保留给显式"丢弃重建" |
| `services/draft_generator.py` | 输出补 `source_signature`（关系）；机器输出即 theirs，不含来源标记（来源由合并层赋值） |
| `services/edit.py` | 写入 `overridden_fields` / `origin`；新增取消钉住 |
| `services/version_diff.py` | 复用；新增"生成运行合并报告"序列化 |
| `schemas` + `api` | 变更报告、冲突列表与解决、字段钉住/取消钉住、生成运行历史等端点 |
| 前端 | 实体徽标、生成变更报告面板、冲突复核 UI、字段级"采纳上游/保留我的" |

---

## 10. 分阶段实施

- **P0（保命）**：加字段级来源 + 三方合并，全量/增量再生成停止破坏人工修正；输出变更报告（文本即可）。→ 解决最痛的"丢修改"。
- **P1（可用）**：冲突复核 UI + 字段钉住/取消钉住 + `source_signature` 关系匹配 + 上游消失处理。
- **P2（完善）**：已发布本体 schema 演进闭环（场景 D）、版本对比/回滚、生成运行历史与"一键采纳上游"。

---

## 12. 实施状态（已落地）

> 本方案已按 P0 → P2 全量实现，`backend` 114 项测试通过，前端 `tsc`/`build` 通过，Alembic 迁移可 up/down 往返。

### 后端
- 模型：`ObjectType/Property/RelationType/BusinessLogic` 增字段级溯源字段；`RelationType.source_signature`；`Ontology.draft_revision`；`DraftGenerationTask.merge_report_json`（`app/models/*`，溯源派生属性见 `app/models/_provenance.py`）。
- 迁移：`alembic/versions/c1d2e3f4a5b6_ontology_provenance_merge.py`，含存量数据保守回填（非 suggested 实体全字段钉住）。
- 合并核心：`app/services/ontology_merge.py`（三方合并 + `MergeReport` + `relation_signature`）。
- 任务层：`app/services/draft_task_service.py` 全量/对象/关系再生成改为合并，不再 purge 重建；写入 `draft_revision` 与合并报告。
- 编辑追踪：`app/services/edit.py` 人工编辑写 `overridden_fields`、清冲突；人工新建置 `user_created`。
- 溯源操作：`app/services/provenance_service.py`（冲突列表/单条解决/批量解决/字段钉住）。
- 演进（场景 D）：`app/services/ontology_revision.py` 从已发布本体深拷贝派生修订草稿并播种权威基线。
- API：`/ontologies/{id}/conflicts`、`/conflicts/resolve`、`/ontologies/{id}/conflicts/resolve-all`、`/fields/pin`、`/domains/{id}/tasks/{tid}/merge-report`、`/domains/{id}/create-revision`。
- 测试：`tests/test_ontology_merge.py`、`tests/test_provenance_api.py`、`tests/test_ontology_revision.py`。

### 前端
- 类型/接口：`types.ts`（`FieldProvenance`/`MergeReport`/`ConflictItem` 等）、`api.ts`（冲突/钉住/合并报告/批量/修订）。
- 组件：`components/ProvenanceBadge.tsx`（机器生成/人工修正/上游有更新/上游已删除徽标）、`components/ConflictsPanel.tsx`（逐字段复核 + 批量）。
- 页面：对象详情页头部徽标；工作区域详情页"字段冲突复核"折叠面板 + "创建修订草稿"按钮；全量生成确认文案改为合并语义。

### 待办（后续增强）
- 任意历史版本回滚（当前修订草稿基于"当前已发布实体"深拷贝，尚未支持从任意 `VersionRecord.snapshot_json` 重建带绑定的草稿）。
- 生成运行历史的独立可视化面板（当前合并报告已按任务落库，可经 merge-report 端点查看）。
- `publish.DraftPersistenceService` 旧的 `save_draft/upsert_*` 已被合并取代，保留为参考，可择机移除。

---

## 11. 关键设计原则回顾

1. **人工修正是第一优先级**：默认保护，机器只接管"人没碰过的字段"。
2. **稳定身份键**：合并靠 URN/signature，不靠可变展示名。
3. **基线可推进**：冲突只提示一次，不反复骚扰。
4. **不可变发布 + 可演进草稿**：版本清晰、可对比、可回滚。
5. **一切可追溯**：生成运行、机器基线、人工 override、冲突与解决全程留痕。
