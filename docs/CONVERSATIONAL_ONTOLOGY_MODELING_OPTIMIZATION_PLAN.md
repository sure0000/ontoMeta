# 对话式本体驱动建模优化方案与执行计划

> 状态：**待评审 / 可执行方案**  
> 文档类型：目标架构 + 产品流程 + 文件级实施计划  
> 适用范围：ontoMeta 当前主干工作树  
> 目标场景：**基于本体知识，通过对话式 Agent 确认需求、确认本体与数据、生成执行计划，完成维度建模、指标逻辑、标签逻辑与数据应用交付。**

---

## 0. 结论先行

ontoMeta 已具备本场景的大部分底层能力：本体生成与治理、Data Agent、多轮澄清、动态表单、指标/标签表达式编译、任务提案、任务链、物化、数据应用、人工确认、RBAC、执行回执和决策留痕。当前主要问题不是“缺少更多零散工具”，而是这些能力尚未围绕一个稳定的建模主线收敛。

本方案只新增两个核心抽象：

1. **建模工单 `ModelingCase`**：承载从需求到结果的权威状态、版本、确认与失效关系；
2. **维度模型 `DimensionalModel`**：把业务过程、事实粒度、维度、度量、SCD、桥接关系等升级为一级设计制品。

其它能力原则上复用现有实现：

- 对话与提案：`chat_bi.py` / `chat_bi_skills.py` / `chat_bi_tool_schemas.py`
- 本体治理：工作区、三方合并、发布与形式化校验
- 口径编译：`expression_candidate.py` / `metric_compiler.py`
- 物理生成：`materialization_contract.py` / `warehouse_generator.py`
- 执行治理：`agent_pipeline.py` / `task_pipeline.py` / `pipeline_compiler.py`
- 数据应用：`data_app.py` / Data App 前端
- 审计留痕：`chat_bi_ledger.py`

### 0.1 目标完成后的用户流程

```text
用户提出业务目标
  → Agent 澄清并生成《需求规格》
  → 用户确认需求
  → Agent 给出《本体与数据上下文》及缺口
  → 用户确认采用的本体版本、对象、关系、数据源
  → Agent 生成《维度模型》
  → 确定性校验器检查粒度、基数、键、SCD、扇出和引擎能力
  → 用户确认维度模型
  → Agent 批量生成指标/标签/规则提案
  → 现有编译器逐条编译与自证
  → Agent 生成执行 DAG 与数据应用提案
  → 用户确认执行方案
  → 现有治理流水线逐制品校验、dry-run、确认、执行
  → 结果验收并发布数据应用
```

### 0.2 范围判断

本方案完成后，系统应达到：

- **P0–P3**：可做完整、可控、可审计的场景演示；
- **P4–P5**：可进入单客户真实环境试点；
- **P6**：具备对外宣称生产级价值的证据。

---

## 1. 现状基线

### 1.1 已有能力与复用点

| 场景环节 | 已有能力 | 主要代码锚点 | 本方案动作 |
|---|---|---|---|
| 需求澄清 | `ask_clarification`、`request_form`、多轮历史 | `services/chat_bi.py`、`schemas/chat_bi.py` | 固化为版本化需求规格 |
| 决策留痕 | 需求/本体/数据/方案/执行/结果六环账本 | `models/chat_bi_ledger.py`、`services/chat_bi_ledger.py` | 保留为观察层，不作为流程权威 |
| 本体确认 | 查询对象/关系/逻辑；本体草稿、合并、发布 | 工作区、本体 API 与服务 | 增加本次建模采用的版本化快照 |
| 数据确认 | 数据源目录、连接测试、库表内省 | `services/data_app.py`、`api/data_app.py` | 固化数据源引用与映射快照，不保存凭据 |
| 指标/标签 | 结构化表达式提案、编译 SQL、语义证明 | `expression_candidate.py`、`metric_compiler.py` | 扩为批量逻辑包 |
| 执行计划 | `update_plan` 与持久化 `TaskPipeline` | `task_pipeline.py`、`pipeline_compiler.py` | 统一为建模工单下的交付计划 |
| 物理生成 | DIM/DWD/ADS 分层、DDL/ETL/DAG/映射 | `materialization_contract.py`、`warehouse_generator.py` | 让维度模型编译为现有契约与任务 |
| 执行门控 | draft→validate→confirm→execute→receipt | `agent_pipeline.py` | 原样复用，不允许绕过 |
| 数据应用 | 面板/看板生成、预览、发布、版本、分享 | `services/data_app.py`、`api/data_app.py` | 绑定工单与模型版本 |

### 1.2 当前关键断点

#### G1 · 没有端到端权威聚合根

会话、决策账本、本体、口径、任务链、数据应用彼此有关联，但没有一个对象回答：

- 本次需求确认到了哪一版；
- 本次采用哪个本体版本与哪些数据源；
- 哪个维度模型是用户确认的；
- 哪组指标/标签属于这次需求；
- 哪条任务链和哪个看板是本次交付结果；
- 上游变化后哪些确认已失效。

#### G2 · “分层投影”不等于“维度建模”

当前规则可以把 `business_object` 放到 DIM、把事实/桥接关系放到 DWD、把业务逻辑放到 ADS，但没有一级制品表达：

- 业务过程；
- 事实表粒度；
- 事务事实、周期快照、累积快照；
- 一致性维度与角色扮演维度；
- 自然键、代理键、退化维度；
- SCD1/SCD2 与迟到维度策略；
- 度量可加性；
- 多对多桥接和扇出风险。

#### G3 · 两种“计划”没有统一

- `update_plan` 是对话内分析路线，主要用于展示；
- `GovernanceTaskPipeline` 是持久化执行编排。

缺少一份连接“需求设计步骤”与“可执行任务 DAG”的交付计划。

#### G4 · 单条提案能力强，方案级批量交付弱

当前可以逐条提指标、标签、任务、面板，但一个真实需求通常需要：

- 一张事实表 + 多个维度；
- 多个指标与标签；
- 一组有依赖的任务；
- 一个包含多个面板的看板。

系统尚不能把它们作为同一版本的设计包统一校验、确认和追踪。

#### G5 · 工程事实与文档有漂移

实施前必须以实际注册表和代码为准。当前实际写侧类型为：

```text
materialize / sync / transform / metric
```

部分旧文档仍引用已移除的 cluster/Bigtop Manager 路径，任务链文档部分段落也仍保留“线性链”旧描述，而代码已经支持 `depends_on` 的 DAG 形态。P0 必须先修正文档与验收基线，避免按过期设计实施。

---

## 2. 设计原则与硬约束

### R1 · 本体仍是业务语义的一级权威

`DimensionalModel` 是面向分析交付的**投影设计**，不是第二套本体。模型中的事实、维度、属性和关系必须引用本体实体 ID；需要新增或修订本体时，只生成变更提案，仍走现有工作区治理与发布流程。

### R2 · 建模工单是流程权威，决策账本是审计观察层

- `ModelingCase` 决定当前阶段、当前有效版本和是否可进入下一阶段；
- `ChatBiDecisionRecord` 继续记录谁接受、修改或拒绝了什么；
- 账本写失败不能让主链失败，也绝不能越权改变工单状态。

### R3 · LLM 只产声明式 Spec，不产直接执行命令

- LLM 可提出 RequirementSpec、DimensionalModelSpec、LogicBundleSpec、DeliveryPlanSpec；
- 确定性代码负责解析、校验、编译、diff 和执行计划生成；
- LLM 不直接生成可绕过校验落库的 DDL/DML；
- SQL 必须经现有编译器、方言适配器与形式化校验。

### R4 · Agent 只提案，确认必须是用户动作

对话工具不得偷偷确认阶段、发布本体、执行任务或发布应用。所有写操作必须由前端确认动作触发，并遵循 RBAC。

### R5 · 确认对象必须版本化且可失效

任何已确认规格都必须有：

- `revision`
- `content_hash`
- `confirmed_by`
- `confirmed_at`
- `based_on` 上游版本/哈希

上游变更后，下游确认应标记 `stale`，而不是静默沿用。

### R6 · 凭据不得进入会话、Spec、快照和决策账本

只保存 `data_source_id`、catalog、数据库名、映射版本、连接状态摘要；连接串、密码、Token 继续由现有设置/数据源服务管理。

### R7 · 不另建执行引擎和调度器

设计计划最终编译为现有：

- `GovernanceArtifact`
- `GovernanceTaskPipeline`
- Airflow DAG
- Flink SQL / 目标引擎 DDL

不再创建第二套任务状态机或第二个调度器。

### R8 · 配置遵守 Web/DB 单一来源

新增可配置项必须遵守 `docs/DEVELOPMENT_PRINCIPLES.md`：运行期配置从数据库读取；除 bootstrap 项外不得新增环境变量配置。

---

## 3. 目标领域模型

## 3.1 建模工单 `ModelingCase`

建议新增：`backend/app/models/modeling.py`。

```text
ModelingCase
├── id
├── title
├── conversation_id?             # 对话入口，可空（允许从专属页面创建）
├── primary_domain_id?
├── domain_ids_json
├── stage                         # 权威流程阶段
├── current_revision
├── owner_subject_id?
├── blocked_reason?
├── created_at / updated_at
└── specs[] / links[]
```

推荐阶段：

```text
collecting_requirement
requirement_confirmed
context_confirmed
model_confirmed
plan_confirmed
executing
verifying
completed
blocked
cancelled
```

`stage` 是工单流程事实，因此可以落库；这与任务链“由制品聚合推导状态”不冲突。任务链状态仍不重复保存。

## 3.2 版本化规格 `ModelingCaseSpec`

不要把所有 JSON 永久塞在 `ModelingCase` 一行中。新增通用版本表：

```text
ModelingCaseSpec
├── id
├── case_id
├── kind                          # requirement/context/dimensional_model/logic_bundle/delivery/acceptance
├── revision
├── status                        # draft/confirmed/stale/superseded/rejected
├── payload_json
├── content_hash
├── based_on_json                 # 上游 kind/revision/hash 列表
├── validation_report_json
├── proposed_by
├── confirmed_by / confirmed_at
├── created_at / updated_at
└── UNIQUE(case_id, kind, revision)
```

为什么使用“通用版本表 + 强类型 Pydantic schema”：

- DB 不因 Spec 字段增加而频繁迁移；
- 业务层仍由 Pydantic 严格校验，不接受任意 JSON；
- 可统一做版本、确认、失效、diff 和审计；
- 与项目现有 JSON Text 存储习惯一致，兼容 SQLite/PostgreSQL。

## 3.3 工单引用 `ModelingCaseLink`

用于追踪最终交付物，不复制其权威状态：

```text
ModelingCaseLink
├── case_id
├── ref_kind                      # ontology/data_source/business_logic/artifact/pipeline/data_app
├── ref_id
├── role                          # input/output/evidence/plan/result
├── spec_revision?
├── metadata_json?
└── UNIQUE(case_id, ref_kind, ref_id, role)
```

## 3.4 需求规格 `RequirementSpec`

第一版至少包含：

```json
{
  "business_goal": "降低销售订单交付延期",
  "business_processes": ["销售订单履约"],
  "subjects": ["订单", "客户", "商品"],
  "questions": ["延期率是多少", "哪些客户风险高"],
  "time_scope": {"type": "rolling", "value": 90, "unit": "day"},
  "grain_expectation": "订单行/天",
  "metrics": ["订单量", "延期率", "销售额"],
  "tags": ["高延期风险客户"],
  "refresh_sla": "0 2 * * *",
  "delivery": ["dashboard"],
  "acceptance_criteria": [
    "延期率与金标准 SQL 偏差为 0",
    "任务失败时不得显示成功"
  ],
  "open_questions": []
}
```

进入 `requirement_confirmed` 前，以下字段必须明确：

- 业务目标；
- 至少一个业务过程或主题；
- 预期交付物；
- 时间范围或明确“不限”；
- 验收标准；
- 所有阻断型 `open_questions` 已清空。

## 3.5 上下文规格 `ModelingContextSpec`

用于确认“这次究竟基于什么本体和数据”：

```json
{
  "ontologies": [
    {"ontology_id": "...", "version": 7, "domain_id": "...", "content_hash": "..."}
  ],
  "selected_objects": ["object-id"],
  "selected_relations": ["relation-id"],
  "selected_logics": ["logic-id"],
  "data_sources": [
    {
      "data_source_id": "...",
      "catalog_name": "erp",
      "database": "sales",
      "mapping_hash": "...",
      "connection_status": "ok"
    }
  ],
  "evidence_gaps": [],
  "assumptions": []
}
```

只保存引用、版本与哈希，不保存 DSN 或凭据。

---

## 4. 维度模型一级制品

## 4.1 `DimensionalModelSpec`

建议放在新包：

```text
backend/app/modeling/
├── __init__.py
├── specs.py
├── validator.py
├── compiler.py
└── diff.py
```

第一版 Spec：

```json
{
  "name": "sales_fulfillment_model",
  "display_name": "销售履约分析模型",
  "business_process": "销售订单履约",
  "ontology_refs": [{"ontology_id": "...", "version": 7}],
  "facts": [
    {
      "name": "fact_sales_order_line",
      "display_name": "销售订单行事实",
      "fact_type": "transaction",
      "grain": {
        "statement": "每个销售订单行一行",
        "keys": ["订单行.id"]
      },
      "source_object_id": "...",
      "event_time_property_id": "...",
      "measures": [
        {
          "property_id": "...",
          "aggregation": "sum",
          "additivity": "additive"
        }
      ],
      "dimensions": [
        {"dimension_ref": "dim_customer", "relation_id": "...", "role": "customer"}
      ],
      "degenerate_dimensions": ["订单行.订单号"]
    }
  ],
  "dimensions": [
    {
      "name": "dim_customer",
      "display_name": "客户维度",
      "source_object_id": "...",
      "natural_key_property_ids": ["..."],
      "surrogate_key": {"enabled": true, "name": "customer_sk"},
      "attribute_property_ids": ["..."],
      "scd_type": "scd2",
      "effective_from_name": "effective_from",
      "effective_to_name": "effective_to",
      "current_flag_name": "is_current",
      "conformed_key": "customer"
    }
  ],
  "bridges": [],
  "role_playing_dimensions": [],
  "target": {
    "data_source_id": "...",
    "engine": "iceberg",
    "database_prefix": "sales"
  }
}
```

## 4.2 必须显式表达的语义

### 事实

- `fact_type`：`transaction | periodic_snapshot | accumulating_snapshot | factless`
- `grain.statement`：人可读且必填；
- `grain.keys`：必须能落到本体对象/属性；
- 事件时间或快照周期；
- 原子度量及 `additivity`：`additive | semi_additive | non_additive`；
- 维度引用与关联关系；
- 退化维度；
- 源对象或关系实现表。

### 维度

- 来源本体对象；
- 自然键；
- 代理键；
- 属性集合；
- SCD 类型；
- 一致性维度标识；
- 角色扮演关系；
- 未知成员策略；
- 迟到维度策略。

### 桥接

任何 N:N 分析路径不得静默直连，必须：

- 引用已有 `bridge_table` 关系；或
- 生成桥接模型提案；或
- 以阻断问题返回用户。

## 4.3 确定性校验器

新增 `backend/app/modeling/validator.py`，至少包含以下规则：

| code | 严重度 | 判据 |
|---|---|---|
| `grain_missing` | error | 每个事实必须声明粒度与键 |
| `grain_key_unresolved` | error | 粒度键必须引用已确认本体属性 |
| `fact_source_unresolved` | error | 事实必须有本体来源或已确认人工设计来源 |
| `dimension_natural_key_missing` | error | 维度必须有自然键；无证据时要求人工确认，不得猜 |
| `join_path_missing` | error | 事实到维度必须存在可证明关联路径 |
| `join_key_unresolved` | error | 路径存在但 JOIN 键无法落到属性 |
| `fanout_risk` | error/warning | 聚合跨一对多、多对多时可能重复计量 |
| `bridge_required` | error | 多对多未配置桥接 |
| `measure_semantic_mismatch` | error | SUM/AVG 等只能用于合适的 measure |
| `additivity_mismatch` | warning/error | 库存余额等不得默认跨时间求和 |
| `scd_capability_gap` | error | 目标引擎无法表达选定 SCD 策略 |
| `conformed_dimension_conflict` | error | 同一一致性维度的键/属性语义冲突 |
| `role_playing_target_missing` | error | 角色扮演维度无基础维度 |
| `ontology_snapshot_stale` | error | 模型基于的本体版本已变化且未 rebase |
| `data_mapping_stale` | error | 数据映射哈希变化且未重新确认 |

复用而不是重写：

- 基数与 JOIN 规则：`ontology_projection.py`
- 路径搜索：`semantic_navigator.py`
- SQL 扇出证明：`sql_soundness.py`
- 语义类型归一：`ontology_types.py`
- 引擎能力：`warehouse/registry.py` 与 Adapter capabilities
- 治理规约：`active_standard(db)`

## 4.4 编译器输出

`backend/app/modeling/compiler.py` 不直接执行，输出 `DimensionalModelCompilation`：

```json
{
  "ontology_changes": [],
  "materialization_contract_patches": [],
  "logical_tables": [],
  "logic_candidates": [],
  "pipeline_steps": [],
  "data_app_blueprint": {},
  "warnings": [],
  "unsupported": []
}
```

输出的去向：

| 编译结果 | 复用路径 |
|---|---|
| 本体缺失对象/关系 | 工作区草稿 + 三方合并，不直接改发布本体 |
| DIM/DWD/ADS 落层与 SCD | `MaterializationContract` patch，人工钉住 |
| 逻辑表 | `WarehouseGenerator` / Dialect Adapter |
| 指标/标签候选 | `expression_candidate` + `metric_compiler` |
| 执行步骤 | `GovernanceTaskPipeline` |
| 面板/看板 | Data App 现有生成路径 |

## 4.5 模型状态与失效

维度模型作为 `ModelingCaseSpec(kind=dimensional_model)` 管理版本，不另造一套无关联状态机：

```text
draft → confirmed → stale/superseded
```

- requirement/context revision 变化 → 已确认模型变 `stale`；
- 模型 revision 变化 → logic_bundle、delivery、acceptance 变 `stale`；
- 目标引擎或数据映射变化 → 模型必须重新 validate；
- 已执行结果不删除，只标记其依据的模型 revision。

---

## 5. 批量指标、标签与规则

## 5.1 `LogicBundleSpec`

```json
{
  "dimensional_model_revision": 3,
  "items": [
    {
      "kind": "metric",
      "name": "sales_amount",
      "display_name": "销售额",
      "fields": [],
      "body": {},
      "target_fact": "fact_sales_order_line",
      "dimensions": ["dim_customer", "dim_product"]
    },
    {
      "kind": "tag",
      "name": "high_delay_risk_customer",
      "display_name": "高延期风险客户",
      "subject_dimension": "dim_customer",
      "fields": [],
      "body": {}
    }
  ]
}
```

## 5.2 编译规则

1. 先用 `search_logics`/服务查询做语义查重；
2. 每一项调用现有 `compile_expression()`；
3. 每一项调用现有 `compile_candidate()` 或等价语义证明；
4. 校验粒度与维度模型一致；
5. 返回逐条 `compiled_sql`、`caliber_trace`、错误和建议修复；
6. 只有全部阻断错误清零后才允许整体确认；
7. 确认后仍逐条创建 `BusinessLogic`，不新造另一份口径权威；
8. 通过 `ModelingCaseLink` 将这些逻辑关联回工单和模型 revision。

## 5.3 标签专项约束

首版必须覆盖：

- 标签主体明确且只能落到一个主维度；
- 每个 case 分支均有输出值；
- 阈值必须来自用户输入、已确认业务规则或数据剖析建议后人工确认，Agent 不得自行决定；
- 标签有效时间与刷新频率明确；
- 标签依赖指标时记录依赖，任务 DAG 中指标先于标签；
- 标签结果列形状继续复用 `metric_compiler.result_column_specs()`。

---

## 6. 统一执行计划 `DeliveryPlanSpec`

## 6.1 设计计划与执行 DAG 的关系

建模工单中新增 `delivery` 规格，表达“交付什么、按什么顺序、由哪些现有制品实现”。它不是第二个调度器。

```json
{
  "steps": [
    {"id": "confirm_ontology", "type": "governance", "status": "done"},
    {"id": "materialize_model", "type": "materialize", "depends_on": []},
    {"id": "sync_source", "type": "sync", "depends_on": ["materialize_model"]},
    {"id": "transform_fact_dim", "type": "transform", "depends_on": ["sync_source"]},
    {"id": "materialize_metrics", "type": "metric", "depends_on": ["transform_fact_dim"]},
    {"id": "publish_dashboard", "type": "data_app", "depends_on": ["materialize_metrics"]}
  ],
  "schedule_cron": "0 2 * * *",
  "rollback": "停止 DAG；保留已发布版本；新表按 staging/swap 回退",
  "acceptance_checks": []
}
```

其中可执行步骤编译成现有 `GovernanceTaskPipeline`；治理确认和应用发布步骤保留在建模工单中，不伪装成 Airflow 任务。

## 6.2 计划确认门槛

进入 `plan_confirmed` 前必须满足：

- Requirement、Context、DimensionalModel、LogicBundle 均为 confirmed 且非 stale；
- 所有目标数据源存在、凭据已配置、连接检查通过；
- 编译结果无 `unsupported` 和 error；
- 每个执行步骤都有 owner、依赖、目标和成功判据；
- 有明确的失败回执和回滚/停止策略；
- 用户看到逐制品 dry-run，不得只看到一句自然语言总结。

## 6.3 执行与验收

- 逐制品仍走 `draft → validate → confirm → execute`；
- 工单 `executing` 阶段由关联制品状态聚合展示，但不复制制品状态；
- 全部执行完成后进入 `verifying`；
- 验收检查运行后由用户确认结果，才能进入 `completed`；
- 任何外部作业失败必须使工单显示 blocked/failed detail，禁止“仅产出成功”冒充“执行成功”。

---

## 7. Agent 与前端优化

## 7.1 Agent 技能

新增一个总控技能 `model`，避免把现有技能推翻：

```text
model：负责建模工单的阶段路由
  ├── onboard：接数据/生成本体草稿
  ├── create：口径提案
  ├── task：任务提案/任务链
  ├── query：验证结果
  └── lineage：影响分析
```

`model` 只解锁阶段工具并叠加对应 prompt，不复制已有工具。

建议新增工具：

| 工具 | 类型 | 作用 |
|---|---|---|
| `get_modeling_case` | 只读 | 读取阶段、当前版本、stale 项和下一步 |
| `propose_requirement_spec` | 纯提案 | 将对话归一成需求规格 |
| `inspect_modeling_context` | 只读 | 汇总本体、对象、关系、数据源、缺口 |
| `propose_context_spec` | 纯提案 | 提出采用的本体与数据快照 |
| `propose_dimensional_model` | 纯提案 | 生成维度模型 Spec |
| `validate_dimensional_model` | 只读 | 调确定性校验器 |
| `propose_logic_bundle` | 纯提案 | 批量生成指标/标签/规则 |
| `propose_delivery_plan` | 纯提案 | 生成交付计划和可执行步骤 |
| `get_modeling_case_status` | 只读 | 回读关联任务、应用与验收状态 |

**确认工具不提供给 LLM。** 确认由前端按钮调用 Modeling API。

## 7.2 Agent 阶段路由

后端根据工单 stage 强制限制下一步：

| 当前阶段 | Agent 应做 | 禁止 |
|---|---|---|
| collecting_requirement | 澄清、表单、需求提案 | 直接出任务/建表 |
| requirement_confirmed | 读取本体和数据、暴露缺口 | 猜数据源 ID |
| context_confirmed | 生成/修订维度模型 | 绕过模型直接出全套 SQL |
| model_confirmed | 生成逻辑包与交付计划 | 改写已确认粒度不留版本 |
| plan_confirmed | 回读任务、解释 dry-run | 自动确认或执行 |
| executing/verifying | 回读状态、运行验收查询 | 编造成功状态 |
| completed | 影响分析、发起新 revision | 原地覆盖历史结果 |

## 7.3 前端信息架构

新增两个入口：

1. **建模工单列表** `/modeling-cases`
2. **建模工单详情** `/modeling-cases/:id`

详情页建议结构：

```text
页头：标题 / 当前阶段 / revision / owner / stale 提示
左侧：需求 → 上下文 → 维度模型 → 逻辑包 → 执行计划 → 验收
中间：当前阶段主编辑/预览区
右侧：对话 Agent + 决策记录 + 影响与缺口
底部：确认/驳回/生成下一步（按 RBAC 和状态显示）
```

维度模型首版不必引入重型图编辑器，可先实现：

- 事实卡片；
- 粒度声明；
- 维度卡片；
- 事实—维度边；
- 错误/警告侧栏；
- 编译结果 diff；
- JSON 只作为高级诊断，不作为主要编辑方式。

## 7.4 与现有 Chat BI 的兼容

- 普通问数仍可不创建建模工单；
- 当用户意图涉及“新建模型/指标/标签/任务/看板”时，Agent 建议创建工单；
- 现有单条提案块保持可用；
- 建模工单模式下，提案块保存到对应 `ModelingCaseSpec`，而不是散落为无关联动作；
- 会话和工单一对多或多对一关系第一版收敛为：一个工单绑定一个主会话，可附加历史 conversation IDs 到 metadata。

---

## 8. API 与权限设计

## 8.1 API

建议新增 `backend/app/api/modeling.py`：

| 方法 | 路径 | 最低角色 | 作用 |
|---|---|---:|---|
| GET | `/api/modeling-cases` | reader | 列表与筛选 |
| POST | `/api/modeling-cases` | editor | 创建工单 |
| GET | `/api/modeling-cases/{id}` | reader | 详情、当前规格、链接、stale |
| PATCH | `/api/modeling-cases/{id}` | editor | 标题、owner 等非确认字段 |
| POST | `/api/modeling-cases/{id}/specs/{kind}` | editor | 保存新 draft revision |
| GET | `/api/modeling-cases/{id}/specs/{kind}/diff` | reader | 与当前 confirmed 版本比较 |
| POST | `/api/modeling-cases/{id}/specs/{kind}/validate` | editor | 确定性校验 |
| POST | `/api/modeling-cases/{id}/specs/{kind}/confirm` | reviewer | 确认规格并推进阶段 |
| POST | `/api/modeling-cases/{id}/specs/{kind}/reject` | reviewer | 驳回并记录原因 |
| POST | `/api/modeling-cases/{id}/compile` | publisher | 编译为逻辑变更、制品与任务链提案 |
| POST | `/api/modeling-cases/{id}/execute` | publisher | 仅推进已逐制品确认的计划，不绕过制品门控 |
| POST | `/api/modeling-cases/{id}/verify` | reviewer | 运行验收检查 |
| POST | `/api/modeling-cases/{id}/complete` | reviewer | 确认交付结果 |
| POST | `/api/modeling-cases/{id}/rebase` | reviewer | 上游版本变化后的显式重基线 |
| GET | `/api/modeling-cases/{id}/impact` | reader | 上游变化与下游失效影响 |

`execute` 端点不得替所有制品自动 confirm。若有关联制品未确认，返回 409 并列出阻断项。

## 8.2 权限

- reader：查看工单、规格、diff、校验报告和结果；
- editor：创建/编辑 draft 规格；
- reviewer：确认需求、上下文、模型、验收结果；
- publisher：编译执行制品、确认执行计划、触发执行与发布数据应用；
- ADMIN Token 继续等价 publisher。

所有权限放入 `auth.py::minimum_role_for` 的集中策略，不在各端点重复发明一套角色判断。

---

## 9. 状态、版本与失效规则

## 9.1 状态转移

| 当前状态 | 动作 | 目标状态 | 前置条件 |
|---|---|---|---|
| collecting_requirement | confirm requirement | requirement_confirmed | RequirementSpec 校验通过 |
| requirement_confirmed | confirm context | context_confirmed | 本体/数据快照有效，阻断缺口清零 |
| context_confirmed | confirm model | model_confirmed | DimensionalModel 无 error |
| model_confirmed | confirm delivery | plan_confirmed | LogicBundle 与 DeliveryPlan 已确认 |
| plan_confirmed | execute | executing | 所有执行制品逐条 confirmed |
| executing | all terminal success | verifying | 真实执行回执成功 |
| verifying | complete | completed | 验收检查通过且用户确认 |
| 任意非终态 | cancel | cancelled | reviewer/publisher + 原因 |
| 任意阶段 | upstream changed | 当前阶段不倒退，相关 spec→stale，case blocked | 显式 rebase 后恢复 |

工单阶段不因一个 draft 保存就倒退；是否能继续由 stale/blocking 聚合判断。这样历史阶段仍可审计，当前阻断也明确。

## 9.2 失效矩阵

| 变更 | 失效内容 |
|---|---|
| RequirementSpec | context、dimensional_model、logic_bundle、delivery、acceptance |
| ContextSpec | dimensional_model、logic_bundle、delivery、acceptance |
| DimensionalModelSpec | logic_bundle、delivery、acceptance |
| LogicBundleSpec | delivery、acceptance |
| DeliveryPlanSpec | 已编译 pipeline、acceptance |
| 本体版本变化 | context 及其全部下游 |
| data source mapping hash 变化 | context 及其全部下游 |
| 目标引擎能力/规约 major 版本变化 | dimensional_model validation、delivery |

## 9.3 幂等

- `content_hash` 相同的 spec 保存不创建新 revision；
- confirm 使用 `(case_id, kind, revision, hash)` 乐观锁；
- compile 使用确认规格哈希生成稳定 compilation key；
- 相同 compilation key 不重复创建 BusinessLogic、Artifact、Pipeline 或 DataApp；
- 执行继续复用现有 GovernanceArtifact 幂等规则。

---

## 10. 分阶段执行计划

## P0 · 基线、边界与文档收敛（2～3 天）

**目标**：开始编码前消除事实漂移，固定验收基线。

| ID | 任务 | 文件/命令 | 验收 |
|---|---|---|---|
| P0-1 | 运行全量后端测试 | `cd backend && .venv/bin/pytest -q` | 记录 collected/passed/failed，不接受只引用旧文档数字 |
| P0-2 | 前端 lint/build | `npm run lint && npm run build` | 全绿；记录 bundle warning |
| P0-3 | 迁移健康检查 | `alembic heads`、迁移测试 | 单一 head；SQLite/PostgreSQL 路径明确 |
| P0-4 | 文档纠偏 | `README.md`、`DW_IMPLEMENTATION.md`、`TASK_PIPELINE_PLAN.md`、`DOMAIN_MODEL.md` | 删除 cluster 现状描述；任务链 DAG/执行通道与代码一致 |
| P0-5 | 建立场景 fixture | 新 `tests/fixtures/modeling_case_sales.json` | 包含订单事实、客户/商品/日期维度、指标、标签、看板目标 |

**退出条件**：主干基线可重复，文档中的“现状”与实际注册表一致。

## P1 · 建模工单与规格版本骨架（1～1.5 周）

**目标**：让需求、确认、版本和失效第一次有权威载体。

| ID | 任务 | 新增/修改文件 | 关键验收 |
|---|---|---|---|
| P1-1 | 数据模型与迁移 | `models/modeling.py`、Alembic migration、`models/__init__.py` | 唯一约束、级联、索引、迁移覆盖模型 |
| P1-2 | Pydantic schema | `schemas/modeling.py` | 六类 spec 强类型校验；extra 字段策略明确 |
| P1-3 | 工单服务 | `services/modeling_case.py` | revision/hash、confirm、reject、stale、rebase、乐观锁 |
| P1-4 | API 与 RBAC | `api/modeling.py`、`api/router.py`、`auth.py` | reader/editor/reviewer/publisher 矩阵成立 |
| P1-5 | 决策账本接缝 | `services/modeling_case.py` 调 `safe_record` | 主事务成功后 best-effort 留痕；账本失败不影响工单 |
| P1-6 | 前端列表/详情骨架 | `pages/ModelingCasesPage.tsx`、`pages/ModelingCasePage.tsx`、`api.ts`、`types.ts` | 可创建、查看阶段、查看 revision/diff/stale |
| P1-7 | 测试 | `test_modeling_case.py`、`test_modeling_case_rbac.py`、`test_modeling_case_staleness.py` | 状态非法跃迁拒绝；上游变更失效正确 |

**退出条件**：不用 Agent，也可手工建立工单、保存需求、确认阶段、修改上游并看到下游 stale。

## P2 · 对话需求确认与本体/数据上下文（1～1.5 周）

**目标**：把已有澄清/表单能力接到 ModelingCase，而不是只留在消息文本里。

| ID | 任务 | 新增/修改文件 | 关键验收 |
|---|---|---|---|
| P2-1 | `model` skill | `services/chat_bi_skills.py` | 正确路由建模意图；普通问数不强制建工单 |
| P2-2 | 工具 schema | `services/chat_bi_tool_schemas.py` | requirement/context 只读与提案工具可见 |
| P2-3 | 工具 dispatch | `services/chat_bi.py` 或拆出的 `services/modeling_agent_tools.py` | 不写库；引用必须来自真实目录 |
| P2-4 | 上下文快照服务 | `services/modeling_context.py` | 本体 version/hash、对象/关系、数据 mapping hash、缺口 |
| P2-5 | 提案渲染块 | `chat_bi_blocks.py`、`ChatBiReferences.tsx` | 需求与上下文卡片可编辑、diff、确认 |
| P2-6 | 会话—工单关联 | `models/modeling.py` / API | 一个主会话可恢复工单，不靠前端内存 |
| P2-7 | 测试 | `test_modeling_agent_requirements.py`、`test_modeling_context_snapshot.py` | 模糊需求会澄清；不得编数据源/对象 id；凭据不入 payload |

**退出条件**：从对话发起后，可走完“需求确认 → 本体与数据确认”，刷新页面仍保持阶段和版本。

## P3 · 维度模型 Spec、校验器、编译预览（2～3 周，核心里程碑）

**目标**：补齐本项目离目标场景最大的能力缺口。

| ID | 任务 | 新增/修改文件 | 关键验收 |
|---|---|---|---|
| P3-1 | 强类型 Spec | `modeling/specs.py` | fact/dimension/bridge/role-playing/SCD/additivity 完整 |
| P3-2 | 本体投影扩展 | `ontology_projection.py` 或 adapter | 能按 ID 解析粒度键、自然键、路径和基数 |
| P3-3 | 确定性 validator | `modeling/validator.py` | §4.3 所列错误码；每个拒绝带 fix |
| P3-4 | 编译器 | `modeling/compiler.py` | 输出契约 patch、逻辑表、任务步骤、应用蓝图，不直接执行 |
| P3-5 | diff | `modeling/diff.py` | 模型 revision 间事实/粒度/维度/SCD 变化可读 |
| P3-6 | Agent 提案工具 | `propose_dimensional_model` / `validate_dimensional_model` | 模型错误可回灌 LLM 修一次；最终以 validator 为准 |
| P3-7 | 模型 UI | `components/modeling/*` | 卡片/边/问题侧栏/编译预览；不强依赖 JSON 编辑 |
| P3-8 | 引擎能力接线 | warehouse registry/adapters | SCD2 等不支持时明确 unsupported，不静默降级 |
| P3-9 | 单元与集成测试 | `test_dimensional_model_*.py` | 粒度、扇出、桥接、SCD、一致性维度、版本 stale |

**退出条件**：订单履约 fixture 可生成一张订单行事实、客户/商品/日期维度；错误模型被确定性拦截；确认后可预览现有 WarehouseGenerator 将产生的物理结构。

## P4 · 批量指标/标签与执行计划（1.5～2 周）

**目标**：将单条口径和任务提案升级为方案级交付，同时保持现有权威不分叉。

| ID | 任务 | 新增/修改文件 | 关键验收 |
|---|---|---|---|
| P4-1 | LogicBundle schema/service | `modeling/specs.py`、`services/modeling_logic_bundle.py` | 批量查重、编译、逐条错误、整体确认 |
| P4-2 | 复用口径编译器 | `expression_candidate.py`、`metric_compiler.py` 调用层 | 不新增第二套 AST→SQL 实现 |
| P4-3 | 标签依赖与有效期 | LogicBundle validator | 标签主体、阈值来源、刷新、依赖 DAG 可验证 |
| P4-4 | DeliveryPlan compiler | `services/modeling_delivery.py` | 设计步骤 → GovernanceTaskPipeline proposal |
| P4-5 | Pipeline 关联 | `task_pipeline.py` / `ModelingCaseLink` | 所有制品可反查所属工单和模型 revision |
| P4-6 | 批量确认 UI | `components/modeling/LogicBundlePanel.tsx` | 可逐条改、批量重验、确认；显示真 SQL 与 trace |
| P4-7 | 计划 UI | `components/modeling/DeliveryPlanPanel.tsx` | DAG、dry-run、阻断项、回滚和验收标准可见 |
| P4-8 | 测试 | `test_modeling_logic_bundle.py`、`test_modeling_delivery.py` | 重名、非法阈值、粒度不一致、依赖环被拦截 |

**退出条件**：一个确认模型可批量产出至少 3 个指标、1 个标签及一条物化→同步→加工→聚合任务 DAG 提案。

## P5 · 数据应用、执行与结果闭环（1.5～2 周）

**目标**：从“设计可编译”推进到“真实执行可验收、结果可发布”。

| ID | 任务 | 新增/修改文件 | 关键验收 |
|---|---|---|---|
| P5-1 | 工单 compile | `services/modeling_case_compiler.py` | 同 hash 幂等；创建 links；不自动确认制品 |
| P5-2 | 执行聚合视图 | `services/modeling_execution.py` | 回读真实 artifact/DagRun，不复制状态 |
| P5-3 | 数据应用蓝图 | 复用 `data_app.py` | 多面板看板绑定同一模型与逻辑 revision |
| P5-4 | 验收检查 | `services/modeling_acceptance.py` | 结果行数、金标准 SQL、数据新鲜度、失败回执 |
| P5-5 | 完成门槛 | ModelingCase service/API | 未验收不得 completed；结果确认写六环账本 |
| P5-6 | 影响分析 | `services/modeling_impact.py` | 本体/口径/数据映射变化可定位受影响模型、任务、应用 |
| P5-7 | E2E 测试 | `test_modeling_case_e2e.py` | 从需求到看板全链；中断后可恢复；失败不假绿 |

**退出条件**：测试数据源上真实跑通一条任务链，验收通过后发布看板；任一步失败时工单不得进入 completed。

## P6 · 真实有效性验证与生产收口（2～4 周）

**目标**：证明价值而不仅是证明 API 存在。

| ID | 任务 | 依据 | 关键验收 |
|---|---|---|---|
| P6-1 | ERPNext/Odoo 数据准备 | `BENCHMARK_DATA_PREP.md` | 投递器在真实实例运行，冻结 baseline |
| P6-2 | 三组对照 | `EFFECTIVENESS_VALIDATION_PLAN.md` | B0/B1/B2 同数据同题，不人为削弱 baseline |
| P6-3 | 维度模型评分 | 新 benchmark scorer | 粒度、事实/维度、键、关系、SCD、扇出正确率 |
| P6-4 | 指标/标签金标准 | benchmark truth | SQL 结果与 truth 对齐；跨系统问题可复现 |
| P6-5 | 故障演练 | DataHub/Airflow/Flink/仓库断连 | 回执真实失败、可重试、无重复副作用 |
| P6-6 | 性能与容量 | 大本体/大结果/多工单 | 响应预算、上下文预算、并发与 DAG 规模达标 |
| P6-7 | 前端分包 | Vite dynamic import/manualChunks | 主 bundle 显著下降，核心首屏不加载全部图表/编辑器 |
| P6-8 | 运维文档 | `DEPLOYMENT.md` | 依赖矩阵、配置、验收、告警和回滚明确 |

**退出条件**：形成可复跑报告，能说明“基于本体和维度模型”相对“直接读 schema 写 SQL”的提升与边界。

---

## 11. 测试计划

## 11.1 测试金字塔

| 层 | 覆盖 |
|---|---|
| 纯函数单测 | Spec 校验、hash、diff、stale 矩阵、粒度/基数/SCD/additivity 规则 |
| 服务测试 | ModelingCase 状态机、revision、confirm、rebase、compile 幂等 |
| API 测试 | RBAC、409 阻断、响应 schema、凭据脱敏 |
| Agent 工具测试 | 工具 schema、真实 ID 约束、提案不写库、错误修复回灌 |
| 编译集成测试 | 模型→契约→LogicalSchema→DDL/ETL→Pipeline |
| UI 构建与交互 | 卡片编辑、diff、确认、恢复、stale 提示 |
| 外部 E2E | DataHub→本体→模型→Airflow/Flink→仓库→Data App |
| Benchmark | 与金标准及无本体 baseline 对照 |

## 11.2 必须钉死的不变量测试

1. Agent 工具调用不能直接确认规格；
2. 未确认模型不能 compile；
3. 未确认制品不能 execute；
4. 上游 revision 变化后下游必为 stale；
5. stale 规格不能进入 plan_confirmed；
6. 相同 hash 重放不产生第二份任务或应用；
7. Spec/快照/账本均不含凭据；
8. N:N 无桥接不能生成聚合计划；
9. 粒度键无法解析不能生成事实表；
10. SCD 能力不足必须 unsupported；
11. 指标和标签 SQL 只能走现有确定性编译器；
12. Airflow/Flink 真实失败不能被记为业务完成；
13. 决策账本写失败不影响主事务，但不得改变权限结论；
14. 普通 Chat BI 问数不受建模工单模式回归影响。

## 11.3 每阶段回归命令

```bash
# 后端全量
cd backend && .venv/bin/pytest -q

# 新模块快速回归
cd backend && .venv/bin/pytest -q \
  tests/test_modeling_case.py \
  tests/test_modeling_case_staleness.py \
  tests/test_dimensional_model_validator.py \
  tests/test_dimensional_model_compiler.py \
  tests/test_modeling_logic_bundle.py \
  tests/test_modeling_delivery.py \
  tests/test_modeling_case_e2e.py

# 既有高风险回归
cd backend && .venv/bin/pytest -q \
  tests/test_chat_bi_skills.py \
  tests/test_chat_bi_form.py \
  tests/test_chat_bi_decision_ledger.py \
  tests/test_metric_compiler.py \
  tests/test_warehouse_generator.py \
  tests/test_agent_pipeline.py \
  tests/test_task_pipeline.py \
  tests/test_pipeline_compiler.py \
  tests/test_data_app.py

# 前端
cd frontend && npm run lint && npm run build
```

---

## 12. 验收场景

## 12.1 主正例：销售履约

用户输入：

> 我想分析过去 90 天订单履约，按客户、商品和渠道看销售额、订单量、延期率，并给高延期风险客户打标签，最后生成每天刷新的销售履约看板。

系统必须做到：

1. 反问并确认“订单还是订单行粒度”；
2. 确认延期定义、时间字段、渠道来源和高风险阈值；
3. 展示采用的本体对象、关系、数据源和映射；
4. 生成订单行事务事实；
5. 生成客户、商品、渠道、日期维度；
6. 校验事实到维度 JOIN 路径及扇出；
7. 生成销售额、订单量、延期率指标；
8. 生成高延期风险客户标签，阈值来自用户确认；
9. 生成物化/同步/加工/聚合任务 DAG；
10. 展示逐制品 dry-run；
11. 真实执行并回读状态；
12. 生成多面板看板；
13. 运行验收 SQL并由用户确认完成；
14. 六环账本能回放谁在哪一步接受或修改了什么。

## 12.2 必测负例

| 负例 | 预期 |
|---|---|
| 用户只说“高价值客户”未给阈值 | 必须澄清，不得自行给 1 万/10 万 |
| 订单表头与订单行同时作为事实但未声明粒度 | `grain_missing/grain_conflict` 阻断 |
| 商品与标签为 N:N 且无桥接 | `bridge_required` 阻断 |
| 库存余额按日期 SUM | `additivity_mismatch` 阻断或要求改末值/平均 |
| 目标引擎不支持所选 SCD2 | `scd_capability_gap`，不得静默改 SCD1 |
| 本体发布新版本 | Context 及下游 stale，要求 rebase |
| 数据源断开 | 执行失败；工单 blocked，不得 completed |
| 重复点击 compile/execute | 不产生重复制品或重复副作用 |

---

## 13. 度量与退出指标

## 13.1 产品指标

| 指标 | P3 目标 | P5 目标 | P6 目标 |
|---|---:|---:|---:|
| 需求一次确认率 | ≥60% | ≥70% | ≥75% |
| 本体/数据上下文人工修改率 | 可观测 | <30% | <20% |
| 维度模型首次校验通过率 | ≥50% | ≥70% | ≥80% |
| 指标/标签编译通过率 | ≥80% | ≥90% | ≥95% |
| dry-run 后参数修改率 | 可观测 | <25% | <15% |
| 工单中断恢复成功率 | ≥95% | ≥99% | ≥99% |
| 假成功回执 | 0 | 0 | 0 |
| 凭据泄漏到 Spec/账本 | 0 | 0 | 0 |

## 13.2 质量指标

- 事实粒度正确率；
- 事实/维度分类准确率；
- JOIN 路径与基数正确率；
- N:N 桥接召回率；
- 主键/自然键识别率；
- SCD 策略人工接受率；
- 指标结果与金标准偏差；
- 标签命中集合与金标准的 precision/recall；
- 看板引用已确认模型/口径的覆盖率。

---

## 14. 风险与缓解

| 风险 | 后果 | 缓解 |
|---|---|---|
| ModelingCase 与决策账本双状态 | 状态分叉 | Case 是权威；账本只审计，测试钉死 |
| 维度模型成为第二本体 | 语义双写 | 模型只引用本体 ID；新增语义走本体变更提案 |
| LLM 生成复杂模型不稳定 | 结果随机 | 强类型 Spec + 确定性 validator/compiler + 一次修复回灌 |
| JSON Spec 演化困难 | 旧 revision 不可读 | `schema_version` + Pydantic migration/upgrade 函数 |
| 自动猜主键/粒度 | 静默算错 | 无证据即阻断并要求人工确认，绝不“候选少就当唯一” |
| SCD2 跨引擎能力不一致 | 执行失败或语义降级 | 动态 capabilities 检查；unsupported 明示 |
| 批量提案一次性太大 | 上下文/界面过载 | 分阶段披露、分页、按业务过程拆模型、结果离场存储 |
| 工单与现有 Chat BI 回归 | 普通问数变重 | `model` skill opt-in；无 case 继续原路径 |
| compile 重复创建制品 | 重复执行 | content hash + compilation key + DB 唯一约束 |
| 本体/映射更新后继续跑旧计划 | 结果失真 | snapshot hash + stale + rebase 硬门槛 |
| 前端继续膨胀 | 首屏慢 | P6 动态 import，建模页面按路由分包 |
| 文档继续漂移 | 错误实施 | P0 清理；as-built 与 plan 文档头部必须标状态 |

---

## 15. 不做事项

本轮明确不做：

1. 不取代 DataHub 的元数据采集；
2. 不让 Agent 自动发布本体或绕过人工确认；
3. 不自建新的 SQL 执行引擎；
4. 不自建新的调度器；
5. 不把所有 Chat BI 问答强制升级为建模工单；
6. 不在第一版实现通用拖拽式 BI 或完整低代码应用平台；
7. 不在第一版支持所有 Kimball 高级模式，首批聚焦事务事实、周期快照、SCD1/SCD2、桥接和角色扮演维度；
8. 不把原始 DSN、密码、Token 保存到工单快照；
9. 不以测试通过代替真实 ERP/DataHub/Airflow/Flink 验证；
10. 不以“LLM 生成了 SQL”作为维度建模完成标准。

---

## 16. 建议排期与人员

以 2～3 名熟悉当前代码的工程师估算：

| 阶段 | 周期 | 主要角色 |
|---|---:|---|
| P0 | 2～3 天 | 全员/技术负责人 |
| P1 | 1～1.5 周 | 后端主导 + 前端 |
| P2 | 1～1.5 周 | Agent/后端 + 前端 |
| P3 | 2～3 周 | 领域建模 + 后端编译器 + 前端 |
| P4 | 1.5～2 周 | Agent/后端 + 前端 |
| P5 | 1.5～2 周 | 数据工程/后端 + 前端 |
| P6 | 2～4 周 | 数据工程、测试、运维 |

里程碑：

- **第 2～3 周末**：需求、本体、数据确认可持久化；
- **第 5～6 周末**：维度模型一级制品可演示；
- **第 7～8 周末**：指标/标签/任务/看板方案级闭环；
- **第 9～12 周**：真实环境试点与 Benchmark；
- **3～6 个月**：多客户生产稳定化。

---

## 17. Definition of Done

本方案不能仅以“页面能点通”判完成。最终 DoD：

- [ ] 一个业务需求拥有唯一建模工单与可恢复阶段；
- [ ] 需求、本体/数据、维度模型、逻辑包、交付计划、验收均版本化；
- [ ] 上游变化会让下游明确 stale；
- [ ] 维度模型显式包含业务过程、粒度、事实、维度、键、SCD、加性和桥接；
- [ ] 模型可被确定性校验并编译到现有本体/契约/数仓生成路径；
- [ ] 指标和标签复用现有结构化表达式与 SQL 证明器；
- [ ] 执行计划编译到现有 GovernanceArtifact/TaskPipeline/Airflow；
- [ ] 任一写操作均经过 RBAC、校验、dry-run 和人工确认；
- [ ] 数据应用只绑定已确认模型与口径版本；
- [ ] 外部执行失败不会显示成功；
- [ ] 重放不产生重复副作用；
- [ ] Spec、快照和账本中无凭据；
- [ ] 普通 Chat BI 路径无回归；
- [ ] 全量自动化测试、前端 lint/build 通过；
- [ ] ERPNext/Odoo 真实 Benchmark 有可复跑报告；
- [ ] 部署、运维、告警、恢复和回滚文档齐全。

---

## 18. 实施顺序最终建议

不要先继续增加零散 Agent 工具，也不要先做重型图形化编辑器。正确顺序是：

```text
P0 事实基线
  → P1 ModelingCase 权威状态与版本
  → P2 对话确认接入工单
  → P3 DimensionalModel Spec + Validator + Compiler
  → P4 批量 LogicBundle + DeliveryPlan
  → P5 执行、数据应用、验收闭环
  → P6 真实 Benchmark 与生产收口
```

其中 **P1 和 P3 是架构关键路径**：

- 没有 P1，后续能力仍会散落在会话、任务和页面里；
- 没有 P3，系统仍只是“本体分层物化 + 指标任务”，不能严谨宣称已完成维度建模。
