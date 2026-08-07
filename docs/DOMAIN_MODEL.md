# ontoMeta 领域模型文档

## 1. 领域目标

ontoMeta 的领域模型用于统一描述系统内的核心业务实体、实体关系和状态流转，确保产品、后端和前端围绕同一套语义对象建设。

---

## 2. 核心领域对象

### 2.1 DomainContext

表示一个建模上下文，对应 DataHub 中的数据域。

关键属性：

- id
- datahubDomainId
- name
- description
- owner
- status

### 2.2 Ontology

表示某一数据域下的一组本体成果，是聚合根。

关键属性：

- id
- domainContextId
- version
- status
- generatedAt
- publishedAt
- generatedBy
- approvedBy

### 2.3 ObjectType

表示业务对象。

关键属性：

- id
- ontologyId
- name
- displayName
- description
- canonicalTermId
- sourceConfidence
- status

### 2.4 Property

表示 ObjectType 的属性。

关键属性：

- id
- objectTypeId
- name
- displayName
- description
- dataType
- sourceFieldRef
- semanticType
- required
- status

### 2.5 RelationType
n
表示两个或多个 ObjectType 之间的关系。

关键属性：

- id
- ontologyId
- name
- displayName
- description
- sourceObjectTypeId
- targetObjectTypeId
- cardinality
- sourceEvidence
- status

### 2.6 BusinessLogic

表示指标、标签、规则等业务逻辑。

关键属性：

- id
- ontologyId
- name
- displayName
- logicType
- description
- expressionSummary
- sourceType
- sourceRef
- status

#### 2.6.1 BusinessLogicObjectBinding

表示 BusinessLogic 与 ObjectType（表/对象）之间的显式绑定，类似于数仓中指标/标签绑定到表。一条 BusinessLogic 可绑定多个 ObjectType；同一对（logic, object）可按 `role` 区分多种角色。

关键属性：

- id
- businessLogicId
- objectTypeId
- role（subject=主对象 / dimension=维度对象 / output=产出对象）
- source（inferred=LLM 或规则推断 / manual=人工绑定）
- confidence
- createdAt

#### 2.6.2 BusinessLogicPropertyBinding

表示 BusinessLogic 与 Property（字段）之间的显式绑定，类似于数仓中指标/标签绑定到字段。一条 BusinessLogic 可绑定多个 Property；同一对（logic, property）可按 `role` 区分多种角色。

关键属性：

- id
- businessLogicId
- propertyId
- role（input=口径输入 / output=结果输出 / filter=过滤条件 / group=分组维度）
- source（inferred / manual）
- confidence
- createdAt

### 2.7 DraftEvidence

表示 LLM 生成草稿时使用的证据集合。

关键属性：

- id
- ontologyId
- evidenceType
- sourceSystem
- sourceRef
- payloadSummary
- confidence

### 2.8 ChangeConfirmation

表示一次重要操作的确认记录。

关键属性：

- id
- ontologyId
- targetType
- targetId
- actionType
- confirmationStatus
- confirmedAt

### 2.9 VersionRecord

表示对象级或本体级版本记录。

关键属性：

- id
- entityType
- entityId
- version
- diffSummary
- operator
- createdAt

### 2.10 字段级溯源（Provenance）

为支持"预生成本体反复运行、增量演进、不丢人工修正"，ObjectType / Property /
RelationType / BusinessLogic 均带一组字段级溯源元数据（由 `ProvenanceMixin`
与 `services/ontology_merge.py` 的三路合并实现）：

- origin：machine（机器生成）/ manual（人工新建）/ machine_edited（机器生成后被人工修正）
- overridden_fields：被人工修改并"钉住"的字段列表（再生成时受保护）
- machine_baseline：上一次机器生成的可合并字段值（三方合并的 base）
- user_created：人工新建，再生成永不覆盖/删除
- deleted_by_user：人工删除墓碑，再生成不复活
- upstream_removed：上游已消失但因含人工价值而保留（置 deprecated）
- last_generation_id：最近一次"见到"该实体的生成运行 id
- conflict_json：未解决的字段级冲突 {field:{base,ours,theirs}}

RelationType 额外带 source_signature（urn 对 + structure_type）作为稳定身份键，
合并匹配时不依赖可变的 name。Ontology 带 draft_revision 追踪草稿演进次数。

### 2.11 MaterializationContract（物化契约）

挂在本体对象 / 关系 / 业务逻辑上的物化配置，是「本体 → 物理数仓」正向生成的落地参数：
目标层（dim/dwd/dws/ads）、目标引擎、增量策略（full/incremental/cdc）、分区键、SCD 类型、
刷新频率。带 ProvenanceMixin 并参与三路合并（人工钉住的字段机器不覆盖）。

### 2.12 GovernanceArtifact（治理制品）

写侧智能体产出的声明式规格及其生命周期：kind（cluster/sync/transform/metric）、spec_json、
status（drafted→validated→confirmed→executing→succeeded|failed）、校验报告与执行回执。
LLM 只产规格不产命令，未经确认不得执行。详见 [DW_IMPLEMENTATION.md](./DW_IMPLEMENTATION.md)。

---

## 3. 领域关系

### 3.1 聚合关系

- 一个 `DomainContext` 下有多个 `Ontology`
- 一个 `Ontology` 下有多个 `ObjectType`
- 一个 `ObjectType` 下有多个 `Property`
- 一个 `Ontology` 下有多个 `RelationType`
- 一个 `Ontology` 下有多个 `BusinessLogic`
- `Ontology` 的对象 / 关系 / 业务逻辑各自可挂一个 `MaterializationContract`
- `GovernanceArtifact`（写侧治理制品）可绑定到一个 `Ontology`

### 3.2 关联关系

- `RelationType` 关联 `ObjectType`
- `BusinessLogic` 通过 `BusinessLogicObjectBinding` 显式绑定一个或多个 `ObjectType`（含 role: subject/dimension/output）
- `BusinessLogic` 通过 `BusinessLogicPropertyBinding` 显式引用多个 `Property`（含 role: input/output/filter/group）
- `DraftEvidence` 为 `ObjectType`、`Property`、`RelationType`、`BusinessLogic` 提供证据
- `ChangeConfirmation` 针对重要操作进行确认记录
- `VersionRecord` 记录任意领域对象的历史版本

> 绑定是显式的一等关系，而不是依赖命名文本模糊命中。
> 草稿生成阶段由 LLM/规则推断产出 `source=inferred` 的候选绑定；
> 工作区编辑阶段可由人工新增、调整或解除绑定，人工绑定的 `source=manual`。
> 历史数据可通过一次性迁移脚本把命名命中固化为 inferred 绑定。

---

## 4. 状态模型

### 4.1 Ontology 状态

- draft
- in_review
- published
- archived

### 4.2 ObjectType / Property / RelationType / BusinessLogic 状态

- suggested
- edited
- approved
- published
- deprecated

### 4.3 ChangeConfirmation 状态

- pending
- confirmed
- cancelled

---

## 5. 领域规则

### 5.1 生成规则

- 本体必须先有草稿，后有发布
- 所有发布内容必须可追溯到至少一组 DataHub 证据或人工说明
- LLM 输出只能生成 suggested 状态对象，不能直接发布

### 5.2 确认规则

- 删除、发布、批量修改、覆盖生成等重要操作必须先确认
- 未确认的操作不能真正执行
- 确认记录必须保留

### 5.3 版本规则

- 每次发布都要创建版本记录
- 已发布对象被修改时，必须进入新的编辑态而不是直接覆盖历史版本

### 5.4 再生成与人工修正保护规则

- 预生成/再生成采用三方合并（base=机器基线 / ours=当前值 / theirs=新机器输出）：
  人未改动的字段接受机器更新；人工修正的字段被保护；双方都改则记为冲突待复核。
- 再生成不得删库重建，也不得无条件覆盖；人工新建与人工删除的意图必须被尊重。
- 已发布本体的持续演进通过"从已发布版本派生修订草稿"进行：已发布值作为人工
  权威基线，再生成产出复核冲突，经确认后发布为新版本。

### 5.5 两种本体生成方式（历史治理 vs 新业务先行）

系统提供两条互补的本体建模入口，分别服务于「存量」与「增量」两类数据资产。
二者产出统一进入同一份草稿本体（同样走 suggested/edited → 确认 → 发布 的治理闭环），
区别在于**方向**与**与物理数据的先后关系**：

| 维度 | 从 DataHub 预生成 | 人工生成 |
| --- | --- | --- |
| 面向数据 | **历史/存量数据**（已有物理表、字段、外键、血缘） | **新业务**（尚无物理表或从零规划） |
| 方向 | **逆向/事后**：从既有元数据抽取本体 | **正向/先行**：先定义本体，再落地物理表 |
| 数据来源 | DataHub 元数据（表、字段、外键、血缘、术语） | 业务人员手工输入 |
| 主要用途 | 对既有数据资产做**本体治理**（对齐语义、识别对象/关系、区分技术表） | 让新业务从第一天起**以本体思想管理数据** |
| 物理表 | 已存在，本体是其语义映射 | 由本体**派生建表 DDL**，在选定数据源上创建 |
| 触发入口 | 「生成」▸「生成本体草稿」 | 「生成」▸「人工生成」 |

- **从 DataHub 预生成**（`generate-draft`）：把已有元数据逆向抽取为业务对象与关系，
  是对**历史数据**的本体化治理手段。对象角色（业务对象/数据表/桥接/事实表/技术表）
  由结构、内容、拓扑、语义信号判定，事实表/桥接表体现为业务关系而非业务对象。
- **人工生成**（`manual/object-types`）：业务人员先手工定义业务对象及其属性（也可
  新增业务关系），系统据此按所选**数据源方言**生成建表 DDL；对象/属性以
  `origin=user`、`user_created=true` 写入草稿本体。物理表的实际执行依赖数据源连接
  配置，作为受控的下一步（当前产出 DDL 供评审/执行）。这是「本体先行 / ontology-first」
  的新业务建模路径：本体是数据结构的**权威定义**，物理表是其投影。

> 一句话区别：**预生成是对历史数据做本体治理（事后、逆向）；人工生成是为新业务
> 用本体思想管理数据（先行、正向），并据本体在数据源上创建物理表。**

---

## 6. 与 DataHub 的映射

### 6.1 外部输入对象

- Domain -> DomainContext
- Dataset -> ObjectType 候选 / BusinessLogic 候选
- SchemaField -> Property 候选
- ForeignKey / Lineage -> RelationType 候选
- SQL / Query / Datajob -> BusinessLogic 候选
- GlossaryTerm -> 标准命名依据
- Usage / Profiling / Assertions -> 证据补强

### 6.2 不直接复制的对象

ontoMeta 不要求把 DataHub 的所有实体原样复制为本地领域对象，而是只保留对本体生成和管理有意义的抽象结果与追溯引用。

---

## 7. 设计原则

- 本体是聚合中心
- 对象、属性、关系、逻辑都是一等公民
- 证据与审核记录必须可追溯
- DataHub 是事实输入源，ontoMeta 是语义表达层
