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

> 管理端路径均挂在 `/api` 前缀下，需 `ONTOMETA_ADMIN_TOKEN`。完整契约以运行中的 OpenAPI（`/docs`）为准。

### 6.1 工作区相关

- `GET /api/domains`、`GET /api/domains/{id}`
- `POST /api/domains/{id}/generate-draft`、`GET /api/domains/{id}/progress`
- `GET /api/domains/{id}/tasks`、任务日志 / stop / retry
- `GET /api/domains/{id}/draft-duplicates`

### 6.2 本体相关

- `GET /api/ontologies`、`GET /api/ontologies/{id}`
- `GET /api/ontologies/{id}/object-types`、`.../relation-types`（分页 `limit`/`offset`/`q`）
- `GET /api/ontologies/{id}/graph`（邻域：`center_id`/`depth`/`max_nodes`/`full`）
- `GET /api/ontologies/{id}/versions`、`.../versions/{v}/diff|snapshot`
- `POST /api/ontologies/{id}/validate`（发布前一致性校验）
- `GET|PATCH /api/object-types/{id}`、`GET|PATCH /api/relation-types/{id}`

### 6.3 业务逻辑相关

- `GET /api/business-logics`（分页）、`GET /api/business-logics/{id}`
- 分类 CRUD、表达式格式化、绑定、发布确认

### 6.4 重要操作确认相关

- `POST /api/confirmations`
- `GET /api/confirmations/{id}`
- `POST /api/confirmations/{id}/confirm`、`.../cancel`

### 6.5 Chat BI

- 会话 / 分类 CRUD：`/api/chat-bi/conversations*`、`/api/chat-bi/categories*`
- `POST /api/chat-bi/ask`、`GET /api/chat-bi/suggestions`

### 6.6 设置与外部应用管理

- LLM / DataHub：`/api/settings/*`、`GET /api/config`
- 外部应用 CRUD、Key 轮换、调用日志、目录：`/api/external-apps*`、`/api/external-api/*`

### 6.7 对外只读（App API Key）

- REST：`GET /api/v1/domains|object-types|relation-types|business-logics`（及详情）
- MCP：`GET|POST /api/mcp`、`GET /api/mcp/tools`、`POST /api/mcp/tools/call`
- Scope：`domains:read` / `objects:read` / `relations:read` / `logics:read`；限流超限 `429`
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

- 所有编辑与发布动作需要审计日志（变更日志 / 确认记录）
- 所有 DataHub 引用保留原始来源 ID
- 发布版本不可被无痕覆盖；支持版本 diff 与快照只读查看
- **权限现状（阶段性）**：
  - 管理面：共享 `ONTOMETA_ADMIN_TOKEN`（非完整 RBAC）
  - 对外面：外部 App API Key（哈希存储）+ 应用级 scope + 进程内限流
  - 产品目标中的「读取 / 编辑 / 审核 / 发布」四层角色尚未落地，见 [OPTIMIZATION_PLAN.md](./OPTIMIZATION_PLAN.md) 非目标说明
- 生产（`DEBUG=false`）500 响应脱敏；CORS 使用显式 origins
---

## 10. 非功能要求

### 10.1 性能

- 数据域详情页应支持大域分页加载（已实现 `PageResult` + limit/offset）
- 图谱展示应支持局部展开，避免一次渲染全图（已实现邻域参数）
- LLM 生成应支持异步任务化（已实现进程内队列；多实例队列延期）

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

---

## 12. Schema 迁移

- 使用 **Alembic**（`backend/alembic/`）；应用启动时执行 `upgrade head`
- 开发可用 SQLite；生产 / Docker Compose 使用 PostgreSQL（`DATABASE_URL`）
- 遗留库（有表无 `alembic_version`）启动时 stamp，或运行 `scripts/alembic_stamp_legacy.py`
- 详见 [backend/alembic/README.md](./backend/alembic/README.md)

---

## 13. 异步任务与队列

- 草稿生成状态机：`queued → running → succeeded | failed | cancelled`（兼容旧 `completed`）
- **进程内** Semaphore + DB 队列位次；配置 `MAX_CONCURRENT_DRAFT_GENERATIONS`
- 进程重启：`queued`/`running` → `failed`（fail-on-restart，不自动 resume）；失败任务可 `retry`
- Chat BI / 表达式格式化中的同步 LLM 调用经 `asyncio.to_thread`，避免阻塞事件循环
- 多实例 / Redis·Celery 级队列为延期项（B5.1），Compose 中 Redis 为可选注释服务

---

## 14. 部署

- 本地：`Makefile`（`install` / `backend` / `frontend` / `migrate` / `test`）
- 容器：根目录 `docker compose up --build` → `postgres` + `api` + `frontend`（Nginx 反代 `/api`）
- 环境变量清单：`backend/.env.example`
