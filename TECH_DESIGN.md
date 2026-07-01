# ontoMeta 技术设计文档

## 1. 技术目标

技术设计的目标，是支撑一个建立在 DataHub 之上的本体建模系统，实现“读取元数据 -> 生成草稿 -> 工作区编辑确认 -> 发布本体 -> 持续演进”的完整闭环。

---

## 2. 总体架构

建议采用分层架构：

1. **接入层**：对接 DataHub GraphQL / OpenAPI
2. **语义生成层**：整理输入证据并调用 LLM 生成草稿
3. **本体管理层**：管理对象、属性、关系、业务逻辑、确认和版本
4. **应用层**：工作区、本体、业务逻辑相关接口
5. **展示层**：前端页面与图谱视图

---

## 3. 核心模块

### 3.1 DataHub Connector

职责：

- 读取数据域、数据集、字段、术语、血缘、任务、查询、图表等信息
- 屏蔽 DataHub API 差异
- 生成统一的内部输入模型

输入：

- DataHub API

输出：

- DomainInput
- DatasetInput
- FieldInput
- LineageInput
- LogicEvidenceInput

### 3.2 Evidence Builder

职责：

- 将 DataHub 原始输入整理为适合 LLM 的证据包
- 按数据域、对象候选、逻辑候选分组
- 做截断、聚合、排序和去噪

输出：

- ObjectTypeEvidencePack
- PropertyEvidencePack
- RelationEvidencePack
- LogicEvidencePack

### 3.3 Ontology Draft Generator

职责：

- 调用 LLM 生成本体草稿
- 分步生成对象、属性、关系和业务逻辑
- 返回结构化 JSON 结果

建议策略：

- 分阶段生成，避免一次性输出过大
- 先对象，再属性，再关系，再逻辑
- 每一步都附带 evidence 引用和 confidence

### 3.4 Confirmation Service

职责：

- 对删除、发布、批量修改、覆盖生成等重要操作提供二次确认能力
- 记录确认动作与操作者
- 驱动重要操作的最终执行

### 3.5 Publish Service

职责：

- 将编辑确认后的草稿发布为正式版本
- 冻结发布快照
- 生成版本记录和差异记录

### 3.6 Ontology Query Service

职责：

- 提供前端查询接口
- 支持对象详情、图谱、业务逻辑详情、版本记录
- 仅提供只读查询，不承载编辑入口

---

## 4. 数据流

### 4.1 草稿生成数据流

1. 用户在工作区选择数据域并点击预处理
2. 后台自动调用 DataHub Connector 拉取对应元数据
3. Evidence Builder 组装证据包
4. Draft Generator 调用 LLM 输出草稿
5. 本地持久化草稿与证据引用
6. 返回可编辑的草稿结果

### 4.2 编辑与发布数据流

1. 用户打开草稿结果
2. 编辑对象、属性、关系和业务逻辑
3. 对删除、发布、批量修改、覆盖生成等重要操作进行二次确认
4. Publish Service 发布版本
5. Query Service 对外提供已发布语义结果

---

## 5. 存储设计

建议采用关系型数据库承载核心业务数据。

### 5.1 核心表

- domain_contexts
- ontologies
- object_types
- properties
- relation_types
- business_logics
- draft_evidences
- change_confirmations
- version_records
- entity_change_logs

### 5.2 关键字段原则

- 所有核心实体都要有 `status`
- 所有发布实体都要有 `version`
- 所有生成结果都要有 `confidence`
- 所有外部来源都要有 `source_ref`

---

## 6. API 设计

### 6.1 工作区相关

- `GET /domains`
- `GET /domains/:id`
- `POST /domains/:id/generate-draft`
- `GET /domains/:id/progress`

### 6.2 本体相关

- `GET /ontologies`
- `GET /ontologies/:id`
- `GET /object-types/:id`
- `GET /ontologies/:id/versions`

### 6.3 业务逻辑相关

- `GET /business-logics`
- `GET /business-logics/:id`

### 6.4 重要操作确认相关

- `POST /confirmations`
- `GET /confirmations/:id`
- `POST /confirmations/:id/confirm`
- `POST /confirmations/:id/cancel`

---

## 7. LLM 设计

### 7.1 生成原则

- 不直接生成最终发布版本
- 所有输出必须结构化
- 每个结果应附带证据引用
- 每个结果应附带置信度和生成说明

### 7.2 推荐拆分方式

1. 识别 Object Type
2. 为每个 Object Type 提取 Property
3. 在对象之间生成 Relation Type
4. 独立识别 Business Logic
5. 对结果做一致性校验

### 7.3 输出结构建议

```json
{
  "objectTypes": [],
  "properties": [],
  "relationTypes": [],
  "businessLogics": [],
  "evidenceRefs": []
}
```

---

## 8. 前端设计原则

- 工作区强调任务与范围
- 本体页强调结构理解与语义编辑
- 业务逻辑页强调规则追溯与依赖解释
- 图谱只作为辅助表达，不替代详情页

---

## 9. 安全与审计

- 所有编辑与发布动作需要审计日志
- 所有 DataHub 引用保留原始来源 ID
- 发布版本不可被无痕覆盖
- 权限分为读取、编辑、审核、发布四层

---

## 10. 非功能要求

### 10.1 性能

- 数据域详情页应支持大域分页加载
- 图谱展示应支持局部展开，避免一次渲染全图
- LLM 生成应支持异步任务化

### 10.2 可维护性

- DataHub 接入层与本体业务层解耦
- LLM Prompt 与生成流程可配置
- 证据组装器独立，便于后续新增数据源

### 10.3 可追溯性

- 每个发布结果都要追溯到生成证据和审核记录
- 每次编辑都要保留变更历史

---

## 11. 分阶段实施建议

### 第一阶段

- 打通 DataHub 接入
- 完成对象、属性、关系、逻辑草稿生成
- 支持人工审核与发布

### 第二阶段

- 增强证据质量
- 支持图谱视图
- 支持版本差异与回滚查看

### 第三阶段

- 增强跨域语义建模
- 增强业务逻辑解析能力
- 增强数字孪生表达能力
