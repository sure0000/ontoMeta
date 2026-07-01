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

---

## 3. 领域关系

### 3.1 聚合关系

- 一个 `DomainContext` 下有多个 `Ontology`
- 一个 `Ontology` 下有多个 `ObjectType`
- 一个 `ObjectType` 下有多个 `Property`
- 一个 `Ontology` 下有多个 `RelationType`
- 一个 `Ontology` 下有多个 `BusinessLogic`

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
