# MCP 工具设计

## 设计原则

1. **工具即能力边界** - MCP 工具定义了系统能力的边界，不依赖 agent 提示词
2. **约束在工具内** - 业务规则、权限检查、接地验证都在工具内部实现
3. **复用现有引擎** - MCP 工具是薄包装层，核心逻辑复用现有的纯函数引擎
4. **结构化返回** - 所有工具返回结构化数据（JSON），包含错误处理和修复建议
5. **审计自动化** - 所有写操作自动记入决策账本

## 工具分类与映射

### 1. 本体建模工具集（Ontology Modeling Tools）

#### 1.1 `infer_ontology_from_datahub`
**功能**：从 DataHub 域推断本体草稿
**复用引擎**：`draft_generator.py::OntologyDraftGenerator.generate()`
**参数**：
```json
{
  "domain_id": "string (required) - DataHub 域 ID",
  "datasource_id": "string (required) - 目标数据源 ID",
  "options": {
    "enable_fk_inference": "boolean (default: true)",
    "enable_relationship_inference": "boolean (default: true)",
    "confidence_threshold": "number (default: 0.5)"
  }
}
```
**返回**：
```json
{
  "ontology_id": "string - 生成的本体 ID",
  "status": "draft|generating|failed",
  "summary": {
    "objects_count": 123,
    "relationships_count": 45,
    "needs_review_count": 12
  },
  "next_steps": ["review_business_objects", "classify_pending"]
}
```
**约束**：
- 需要 DataHub 连接配置存在且可用
- 需要目标数据源存在
- 自动写入决策账本

#### 1.2 `classify_business_objects`
**功能**：批量分类或重新分类业务对象
**复用引擎**：`draft_generator.py::classify_object_role()`
**参数**：
```json
{
  "ontology_id": "string (required)",
  "object_ids": ["array of object IDs (optional) - 留空表示处理所有待审对象"],
  "force_reclassify": "boolean (default: false) - 是否重新分类已确认对象"
}
```
**返回**：
```json
{
  "classified_count": 45,
  "business_objects": 12,
  "data_tables": 20,
  "bridges": 8,
  "technical_tables": 5,
  "review_required": ["obj_123", "obj_456"]
}
```

#### 1.3 `infer_relationships`
**功能**：推断或重新推断对象间关系
**复用引擎**：`draft_generator.py` 关系推断逻辑
**参数**：
```json
{
  "ontology_id": "string (required)",
  "source_object_id": "string (optional) - 限定源对象",
  "target_object_id": "string (optional) - 限定目标对象"
}
```

#### 1.4 `validate_ontology`
**功能**：按治理规约校验本体
**复用引擎**：`governance/lint.py::lint_spec()`
**参数**：
```json
{
  "ontology_id": "string (required)",
  "standard_version": "string (optional) - 默认使用当前激活规约"
}
```
**返回**：
```json
{
  "valid": false,
  "violations": [
    {
      "level": "error|warning",
      "rule": "rule_id",
      "message": "描述",
      "location": "object_123.field_456",
      "fix": "修复建议"
    }
  ],
  "blocking_count": 3,
  "can_publish": false
}
```

### 2. 数据任务工具集（Data Task Tools）

#### 2.1 `propose_sync_task`
**功能**：提议同步任务（从源数据库到 ODS）
**复用引擎**：
- `agents/drafters/sync_drafter.py` - 生成 spec
- `governance/lint.py` - 自动校验
**参数**：
```json
{
  "name": "string (required)",
  "source_datasource_id": "string (required)",
  "target_datasource_id": "string (required)",
  "source_tables": ["array of table names (required)"],
  "strategy": "full|incremental|cdc",
  "schedule": "cron expression (optional)",
  "context": "string (optional) - 业务背景，写入决策账本"
}
```
**返回**：
```json
{
  "proposal_id": "string - 提案 ID，用于后续确认",
  "artifact_id": "string - 制品 ID",
  "spec": { /* 完整 spec */ },
  "validation": {
    "valid": true,
    "violations": []
  },
  "confirmation_url": "/tasks/sync/{artifact_id}/confirm",
  "next_steps": ["review_spec", "confirm_execution"]
}
```
**内置约束**：
- 自动调用 `lint_spec()` 校验
- 不合规直接拒绝，返回 violations
- 自动写入决策账本（intent + ontology + data + plan 环）
- **只提案，不执行** - 需要前端确认后才执行

#### 2.2 `propose_transform_task`
**功能**：提议清洗加工任务
**复用引擎**：
- `agents/drafters/transform_drafter.py`
- `flink_sql_generator.py::generate_flink_sql()` - 预生成 SQL 预览
**参数**：
```json
{
  "name": "string (required)",
  "ontology_id": "string (required)",
  "source_datasets": ["array of dataset references"],
  "target_datasource_id": "string (required)",
  "target_database": "string (required)",
  "target_table": "string (required)",
  "transformation_rules": { /* JSON 规则定义 */ },
  "schedule": "cron expression (optional)",
  "context": "string (optional)"
}
```

#### 2.3 `propose_materialize_task`
**功能**：提议物化任务（本体对象 → 物理表）
**复用引擎**：
- `agents/drafters/materialize_drafter.py`
- `agents/executors/materialize_executor.py::preflight()` - 预检
**参数**：
```json
{
  "name": "string (required)",
  "ontology_id": "string (required)",
  "object_ids": ["array of object IDs to materialize"],
  "target_datasource_id": "string (required)",
  "load_strategy": "full|incremental|snapshot",
  "schedule": "cron expression (optional)",
  "context": "string (optional)"
}
```
**内置预检**：
- M13 preflight 检查（13 项预检）
- 不通过直接拒绝，返回 blocking issues

#### 2.4 `propose_metric_task`
**功能**：提议指标计算任务
**复用引擎**：
- `agents/drafters/metric_drafter.py`
- `metric_compiler.py::compile_metric()` - 编译预览

#### 2.5 `get_task_status`
**功能**：查询任务执行状态
**复用引擎**：`services/ops_records.py::REGISTRY["task_run"]`
**参数**：
```json
{
  "task_id": "string (optional) - 任务 ID",
  "artifact_id": "string (optional) - 制品 ID",
  "scope": "session|ontology|all (default: ontology)"
}
```
**返回**：
```json
{
  "artifact_id": "string",
  "name": "string",
  "status": "draft|confirmed|running|succeeded|failed",
  "last_run": {
    "started_at": "ISO8601",
    "completed_at": "ISO8601",
    "status": "running|succeeded|failed",
    "error": "string (if failed)",
    "records_processed": 12345
  },
  "next_run_at": "ISO8601 (if scheduled)",
  "spec": { /* 关键字段投影 */ },
  "as_of": "ISO8601",
  "source": "GovernanceArtifact.status"
}
```

#### 2.6 `list_task_runs`
**功能**：列出任务的历史运行记录
**复用引擎**：`services/ops_records.py::REGISTRY["pipeline"]`

### 3. 查询工具集（Query Tools）

#### 3.1 `query_ontology`
**功能**：查询本体结构
**复用引擎**：`services/ontology_query.py`
**参数**：
```json
{
  "ontology_id": "string (optional) - 留空查所有",
  "include_unpublished": "boolean (default: false)",
  "filters": {
    "object_role": "business_object|data_table|...",
    "needs_review": "boolean"
  }
}
```
**返回**：
```json
{
  "ontologies": [
    {
      "id": "string",
      "domain_name": "string",
      "version": "string",
      "objects_count": 123,
      "relationships_count": 45,
      "published_at": "ISO8601"
    }
  ]
}
```

#### 3.2 `query_business_objects`
**功能**：查询业务对象详情
**复用引擎**：`services/ontology_query.py`
**参数**：
```json
{
  "ontology_id": "string (required)",
  "object_id": "string (optional) - 留空查所有",
  "keyword": "string (optional) - 名称或描述模糊搜索",
  "include_fields": "boolean (default: true)",
  "include_landing": "boolean (default: false) - 是否包含物理落点信息"
}
```
**返回**：
```json
{
  "objects": [
    {
      "id": "string",
      "name": "业务对象名",
      "object_type": "business_object",
      "description": "string",
      "fields": [
        {
          "name": "字段名",
          "semantic_type": "attribute|category|...",
          "data_type": "string",
          "description": "string"
        }
      ],
      "landing": {
        "ods_table": "ods.ods_erp_customer",
        "serving_table": "dwd.dwd_customer",
        "materialized": true,
        "last_sync_at": "ISO8601"
      }
    }
  ]
}
```

#### 3.3 `query_relationships`
**功能**：查询对象间关系
**复用引擎**：`services/ontology_query.py`
**参数**：
```json
{
  "ontology_id": "string (required)",
  "source_object_id": "string (optional)",
  "target_object_id": "string (optional)",
  "relationship_type": "references|contains|transforms (optional)"
}
```

#### 3.4 `execute_sql`
**功能**：在数据仓库执行只读 SQL
**复用引擎**：
- `services/query_gateway.py::run_sql()`
- `services/answer_verifier.py` - 自动接地验证
**参数**：
```json
{
  "sql": "string (required) - SELECT 语句",
  "datasource_id": "string (optional) - 默认使用 warehouse",
  "limit": "number (default: 100, max: 1000)"
}
```
**返回**：
```json
{
  "columns": ["col1", "col2"],
  "rows": [ [val1, val2], ... ],
  "row_count": 42,
  "truncated": false,
  "execution_time_ms": 123,
  "grounding": {
    "entities_verified": ["customer", "order"],
    "numbers_verified": [42, 1234.56],
    "hallucination_risk": "low|medium|high"
  }
}
```
**内置约束**：
- SQL 白名单校验（只允许 SELECT）
- 自动 LIMIT
- F4 接地验证
- 禁止访问系统表

#### 3.5 `get_lineage`
**功能**：查询血缘关系
**复用引擎**：`services/pipeline_lineage.py`
**参数**：
```json
{
  "entity_type": "object|table|task",
  "entity_id": "string (required)",
  "direction": "upstream|downstream|both",
  "depth": "number (default: 2, max: 5)"
}
```

#### 3.6 `get_landing`
**功能**：查询对象的物理落点
**复用引擎**：`services/object_landing.py::bulk_object_landings()`
**参数**：
```json
{
  "target_kind": "object|logic",
  "target_id": "string (optional)",
  "keyword": "string (optional)"
}
```
**返回**：
```json
{
  "landings": [
    {
      "object_id": "string",
      "object_name": "客户",
      "ods_table": "ods.ods_erp_customer",
      "ods_status": "synced|syncing|failed",
      "ods_last_sync_at": "ISO8601",
      "serving_table": "dwd.dwd_customer",
      "serving_status": "materialized|...",
      "serving_last_update_at": "ISO8601",
      "as_of": "ISO8601",
      "source": "IngestionContract + WarehouseObjectProjection"
    }
  ]
}
```

### 4. 治理工具集（Governance Tools）

#### 4.1 `validate_against_policy`
**功能**：按规约校验 spec 或本体
**复用引擎**：`governance/lint.py::lint_spec()`
**参数**：
```json
{
  "target_type": "ontology|task_spec",
  "target_id": "string (required)",
  "standard_version": "string (optional)"
}
```
**返回**：同 `validate_ontology`

#### 4.2 `lint_task_spec`
**功能**：检查任务 spec 的合规性
**复用引擎**：`governance/lint.py`

#### 4.3 `get_active_governance_standard`
**功能**：获取当前激活的治理规约
**复用引擎**：`governance/standard.py`
**返回**：
```json
{
  "version": "v1.2.0",
  "activated_at": "ISO8601",
  "policy_count": 42,
  "summary": {
    "naming_conventions": "...",
    "required_fields": ["..."],
    "prohibited_patterns": ["..."]
  }
}
```

### 5. 运维记录工具集（Operational Record Tools）

#### 5.1 `get_ops_record`
**功能**：查询各类运维记录
**复用引擎**：`services/ops_records.py::REGISTRY`
**参数**：
```json
{
  "family": "task_run|pipeline|datasource|ontology_version|draft_run|merge_report|conflict|decision|standard|data_app|component|migration",
  "subject_id": "string (optional)",
  "keyword": "string (optional)",
  "scope": "session|ontology|all",
  "limit": "number (default: 20, max: 100)"
}
```
**返回**：
```json
{
  "family": "task_run",
  "subject": "同步 ERP 客户表",
  "facts": [
    {"label": "状态", "value": "succeeded"},
    {"label": "处理行数", "value": 12345},
    {"label": "耗时", "value": "2m 34s"}
  ],
  "items": [ /* 列表型记录 */ ],
  "as_of": "ISO8601",
  "observed_at": "ISO8601",
  "source": "GovernanceArtifact.status",
  "truncated": false
}
```

## 工具统计

| 工具集 | 工具数量 | 只读/写 | 复用现有引擎 |
|--------|---------|---------|-------------|
| 本体建模 | 4 | 写 | ✅ draft_generator, lint |
| 数据任务 | 6 | 写（提案） | ✅ drafters, executors, lint |
| 查询 | 6 | 只读 | ✅ ontology_query, query_gateway |
| 治理 | 3 | 只读 | ✅ governance/lint, standard |
| 运维记录 | 1 | 只读 | ✅ ops_records |
| **合计** | **20** | 10 写 + 10 读 | **100% 复用** |

## 与现有 Data Agent 工具对比

当前 Data Agent 有 **30+ 个工具**（`chat_bi_tool_schemas.py`），其中：
- 很多是内部辅助工具（`search_objects`, `get_object`, `select_skill`）
- 有些是低频工具（`get_modeling_case`, `get_preference`）
- 有些是前端 UI 职责（`propose_form`, `confirm_action`）

MCP 工具集精简到 **20 个核心能力工具**，通过：
1. 合并低频工具（12 个运维记录族 → 1 个 `get_ops_record`）
2. 移除纯 UI 工具（确认流程归前端）
3. 移除内部辅助工具（通用 agent 自己有搜索能力）

## 下一步

1. 定义 MCP 服务端框架（Python MCP SDK）
2. 实现工具注册和分发机制
3. 为每个工具编写包装器（thin wrapper）
4. 实现统一的错误处理和审计
5. 编写工具测试用例
