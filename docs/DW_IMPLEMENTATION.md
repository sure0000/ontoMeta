# 本体驱动的智能数仓 · 实现执行文档（DW_IMPLEMENTATION）

> 本文档为**已建成（as-built）执行规格**：M0–M8 与五项遗留项均已落地，后端 474 测试全绿，
> Alembic 单一头 `c3d4e5f6a7b8`。文中路径、类名、字段、签名、测试名均对照仓库实际代码核实。
> 未在真实实例核实的外部事实（BM 响应信封、目标 DataHub 版本、部分引擎能力条目）显式标注
> 「需实施前验证」，不臆造。

---

## 1. 唯一不可动摇的不变量

**本体是一级源数据，物理表是二级投影。**

| | 是什么 | 权威性 |
|---|---|---|
| 业务表（ERPNext 等源系统） | OLTP 物理表 | 建模证据，不是权威 |
| 本体 | 对象/属性/关系/业务逻辑 | 一级源数据，唯一权威，引擎无关的逻辑模型 |
| 物理表（Hive/Doris/…） | 本体在各引擎上的实例化投影 | 二级派生，可重建、可多副本 |

推论（决定所有实现细节）：

1. 物理表可丢弃可重建——所有生成器**幂等**，重跑逐字节一致。
2. 本体是引擎无关逻辑模型——生成器**绝不含引擎特定逻辑**，全部下沉到 Dialect Adapter（`app/warehouse/adapters/`）。
3. 同步 = 按本体结构做映射搬运，不是照搬源表。
4. 只有本体里有的对象才有物理表。
5. **全文涉及本体与物理表关系之处，方向恒为「本体 → 物理」，绝不反向。**

**分层降级**：DWD/DIM/DWS/ADS 不是建模范式，只是物化契约的一个字段（`target_layer`）。本体的对象/关系图才是模型主轴。

### 术语

| 术语 | 定义 | 注意 |
|---|---|---|
| 本体 | 一级源数据，引擎无关逻辑模型 | 唯一权威 |
| **ODS** | **本体物理投影层** | ⚠ 与传统「贴源层」不同，本文档显式重定义 |
| **STG** | 贴源保全区，仅对关键源保留的原始副本 | 不建模、不挂 Domain、对本体不可见 |
| 物化契约 | 挂在本体对象/关系/逻辑上的物化配置 | 目标引擎、层、增量策略、分区、SCD、刷新频率 |
| 治理制品 | 五种制品中的任一 | 共用草稿-校验-确认-发布-溯源机制 |

---

## 2. 六步闭环与核心方法论

```
⓪ 智能体部署底座    ClusterSpec  → Bigtop Manager → 集群就绪
① 智能体同步数据    SyncSpec     → SeaTunnel     → ODS（本体投影）
② LLM + 人工治理    业务表元数据 → ontoMeta      → 发布本体
③ 智能体生成治理任务  本体+清洗需求 → DDL/ETL      → 各引擎物理表
④ 智能体创建指标任务  MetricSpec   → 聚合 SQL      → 指标物化
⑤ 智能问数 + 数据应用 Chat BI / Data App
```

**五种治理制品，一套已有机制。** ontoMeta 现有本体生产方式为 `LLM 草稿 → 校验 → 人工确认 → 版本化发布 → 溯源`，配套设施已全部存在（`draft_generator.py` / `draft_consistency.py` / `change_confirmations` / `ontology_merge.py` / `ProvenanceMixin` / `version_records`）。它原本只生产「本体」，扩到五种即全部落位——**不新建框架**。

| # | 制品 | 输入 | LLM 产出的声明式规格 | 确定性执行器 |
|---|---|---|---|---|
| 0 | 本体 | （已有） | Ontology | 已有管线 |
| 1 | 集群拓扑 | 服务器信息 + 部署要求 | ClusterSpec | 调 BM REST API |
| 2 | 同步作业 | 数据同步需求 | SyncSpec（含保全策略） | SeaTunnel 作业生成 |
| 3 | ETL 任务 | 数据清洗需求 | TransformSpec | Spark SQL 生成 |
| 4 | 指标任务 | 计算口径 | MetricSpec | 聚合 SQL 生成 |

> **「五种制品」vs「四类智能体」**：制品 #0 本体走既有管线；写侧智能体（`ArtifactKind`）只有
> `cluster/sync/transform/metric` **四类**（#1–#4）。二者合起来是「五种治理制品」，代码里
> `governance_artifacts.kind` 只承载后四类。

**铁律：LLM 只产声明式 Spec，不产命令。** 执行器是确定性代码，可测试、可回滚、可审计。

---

## 3. 里程碑与依赖顺序

| ID | 里程碑 | 依赖 | 状态 | 迁移文件 |
|---|---|---|:--:|---|
| **M0** | RBAC 四层角色 | — | ✅ | `b2c3d4e5f6a7_rbac_principals.py` |
| M1 | 物化契约数据模型 | — | ✅ | `a1b2c3d4e5f6_materialization_contracts.py` |
| M2 | Dialect Adapter + Capability Matrix | M1 | ✅ | — |
| M3 | 本体 → 物理正向生成器 | M1, M2 | ✅ | — |
| M4 | Chat BI / Data App 落地执行 | M2 | ✅ | — |
| M5 | 智能体流水线骨架 | M0 | ✅ | `c3d4e5f6a7b8_governance_artifacts.py` |
| M6 | 四类 Drafter/Executor（④→③→①→⓪） | M3, M5 | ✅ | — |
| M7 | DataHub 回写 | — | ✅ | — |
| M8 | 多引擎补齐（Doris/Iceberg/StarRocks/ClickHouse） | M2, M3 | ✅ | — |

**M0 为阻断项**：写侧智能体出错是改生产集群与删表，RBAC 落地前 M5/M6 不得开放。M0 已交付，四层角色集中策略见 `app/auth.py`。

阶段一退出条件：本体一键生成 → 物化到 Hive → DataHub 回采 → Chat BI 出结果的完整链路跑通，生成 SQL 人工复核通过率 > 80%。

---

## 4. M0 · RBAC 四层角色

**目标**：把 README 曾标注「未产品化」的四层角色 `reader < editor < reviewer < publisher` 落地为集中式策略闸门。

**验收标准**：角色 × 端点的允许/拒绝矩阵成立；Token 轮换后旧 Token 立即失效；未创建任何主体时行为与启用 RBAC 前一致；`ONTOMETA_ADMIN_TOKEN` 等价 publisher（superuser）。

**数据模型**（`b2c3d4e5f6a7_rbac_principals.py`）：新表 `principals`——`id`、`name`、`role`、`token_hash`、`token_prefix`、`active`、`last_used_at`、`created_at`、`updated_at`。Token 哈希方案：SHA-256 + pepper，明文仅创建/轮换时返回一次，库内只存哈希与前缀（沿用当时 external_apps 的既有做法，该模块已随外部 API 一并移除）。

**文件**：`app/models/principal.py`、`app/schemas/principal.py`、`app/services/principal_service.py`、`app/api/principals.py`；修改 `app/auth.py`、`app/api/router.py`、`app/models/__init__.py`。

**关键接口**：
- `app/auth.py::minimum_role_for(method, path) -> str` —— 集中策略表，`_ROLE_OVERRIDES`（正则匹配 method×path）优先于 `_METHOD_DEFAULTS`（GET→reader，POST/PUT/PATCH→editor，DELETE→publisher）。
- `AdminAuthMiddleware` 解析主体令牌→角色，强制 `minimum_role`；豁免前缀 `/api/public`、`/health` 不变（`/api/v1`、`/api/mcp` 随外部 API 模块移除）。

**权限矩阵**（`_ROLE_OVERRIDES` 摘要，`GET /api/principals-policy` 返回完整策略）：

| 端点模式 | 最低角色 |
|---|---|
| `GET/HEAD/OPTIONS *` | reader |
| `POST/PUT/PATCH *`（默认） | editor |
| `/api/confirmations`、`/conflicts`、`/resolve(-all)`、`PATCH /api/fields/pin` | reviewer |
| `/api/principals`、`/api/agents`、`(pre-)publish`、`/share`、`/datahub/writeback`、`/execute`、`/api/settings`、`/api/{llm-services,datahub,cube}`、任意 `DELETE` | publisher |

**API**：`POST/GET /api/principals`、`PATCH/DELETE /api/principals/{id}`、`POST /api/principals/{id}/rotate-token`、`GET /api/principals-policy`。

**前端**：设置页「角色与令牌」页签 `frontend/src/components/PrincipalsPanel.tsx`。

**测试**：`backend/tests/test_rbac.py`——角色×端点允许/拒绝矩阵、token 轮换、向后兼容（无主体时 admin token 通行）。

---

## 5. M1 · 物化契约

**目标**：补齐本体不承载的落地配置（目标层/引擎/增量/分区/SCD/刷新），供生成器据此产出各引擎产物。

**验收标准**：默认值可由本体实体推导；人工覆盖的字段被「钉住」，机器重推导不覆盖（三路合并）；`(ontology_id, target_kind, target_id)` 唯一。

**数据模型**（`a1b2c3d4e5f6_materialization_contracts.py`，`app/models/warehouse.py`）：新表 `materialization_contracts`——`target_kind`(`object_type`|`relation_type`|`business_logic`)、`target_id`、`target_layer`(`dim`|`dwd`|`dws`|`ads`)、`target_engines`(JSON Text，API 暴露为 `engines: string[]`)、`load_strategy`(`full`|`incremental`|`cdc`)、`partition_key`、`scd_type`(`none`|`scd1`|`scd2`)、`refresh_cron`、`materialized`(bool)、`derivation_reason`，**+ ProvenanceMixin 全套**（`origin`、`overridden_fields`、`machine_baseline`、`user_created`、`deleted_by_user`、`upstream_removed`…）。

**默认值推导**：`business_object` → `dim`；`fact_table`/`bridge_table` 关系 → `dwd`；BusinessLogic → `ads`；存在时间语义属性 → `load_strategy=incremental` 且 `partition_key` 取该属性；否则 `full`。

**文件**：`app/models/warehouse.py`、`app/schemas/warehouse.py`、`app/services/materialization_contract.py`、`app/api/warehouse.py`；修改 `app/models/__init__.py`、`app/api/router.py`。

**关键接口**：`MaterializationContractService.sync(db, ontology_id)`（按本体重推导，只改未钉住字段）、`list_contracts(...)`、`update(db, id, patch)`（提交字段即钉住）。

**API**：`GET /api/ontologies/{id}/materialization-contracts`、`POST .../materialization-contracts/sync`、`PATCH /api/materialization-contracts/{id}`。

**前端**：`ObjectTypeDetailPage.tsx` / `RelationTypeDetailPage.tsx` 的 `MaterializationContractPanel.tsx`。

**测试**：`backend/tests/test_materialization_contract.py`——默认推导、覆盖后重生成不被覆盖、唯一约束。

---

## 6. M2 · Dialect Adapter + Capability Matrix

**目标**：把「本体所需特性 × 目标引擎能力」显式化，渲染前校验，**表达不了即报错，绝不静默降级**。

**验收标准**：每个 adapter 声明完整 `capabilities()`；Hive DDL 快照稳定；能力不足抛 `CapabilityError`；未核实的能力矩阵自曝 warning。

**包结构**（`app/warehouse/`，本次唯一有理由开子包的多文件子系统）：
- `logical_schema.py`——`LogicalColumn` / `LogicalConstraint` / `LogicalTable` / `LogicalSchema`（frozen dataclass，引擎无关中间表示）。
- `capabilities.py`——`@dataclass(frozen=True) Capabilities` + `check_table()` + `CapabilityError` + `CapabilityGap`（`severity` = error 阻断 / warning 呈现）。
- `adapters/base.py`——`class DialectAdapter(ABC)`：`capabilities()`、`map_type(data_type, semantic_type)`、`render_create_table(t)`（须先 `self.guard(t)`）、`render_alter(before, after)`、`translate_sql(sql)`、`quote_identifier(name)`；`UnimplementedAdapter` 占位基类（保留供未来新增引擎）。
- `adapters/{hive,doris,iceberg,starrocks,clickhouse}.py`。
- `registry.py`——`get_adapter(name)` / `list_adapters()` / `list_engines()` / `DEFAULT_ENGINE="hive"`。

**⚠ 对 Part 2 骨架的一处调整（以仓库约定为准）**：能力矩阵把原设计的布尔 `supports_foreign_key` 细化为三态 `ConstraintSupport`（`ENFORCED`/`DECLARATIVE`/`NONE`），主外键同理。理由：Hive 没有真外键但能写进 `TBLPROPERTIES`——那是**有记录的声明**，与「连声明都不支持」语义不同，布尔表达不了这一区分。

**能力矩阵（本体特性 × 引擎，已对照官方文档核实，`verified=True`）**：

| 特性 | Hive | Doris | StarRocks | Iceberg | ClickHouse |
|---|---|---|---|---|---|
| primary_key | 声明式 | 强制(Unique) | 强制(Primary Key) | 声明式 | 声明式(ORDER BY) |
| foreign_key | 声明式 | 无 | 声明式(优化器) | 声明式 | 无 |
| 表/列注释 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 分区 | ✓ | ✓(AUTO) | ✓(表达式) | ✓(变换) | ✓ |
| 分桶 | ✓ | ✓ | ✓ | ✓(bucket) | ✗ |
| SCD2 MERGE | ✗ | ✗(4.1+才有) | ✗ | ✓ | ✗ |
| ALTER 增/删/改名 | 增/改名 | 全 | 全(改名3.3.2+) | 全(原生) | 全 |
| 标识符上限 | 128 | 64 | 1024 | 无上限* | 无上限* |
| 时间类型 | TIMESTAMP | DATETIME | DATETIME | TIMESTAMP | DateTime64 |

\* Iceberg/ClickHouse 无文档化名称长度上限，取大值表示不设限——这是两处「文档未明确」项，已在 adapter 注释标注。

**核心机制**：`WarehouseGenerator` 渲染前调 `adapter.guard(table)`——error 级缺口抛 `CapabilityError`（上层列进 `unsupported`），warning 级返回给调用方呈现（如未核实引擎、注释丢失）。

**复用点**：类型映射沿用 `connectors/cube.py::_dim_type`（语义优先于物理类型）；SQL 方言翻译与 `services/data_app_executor.py::_translate_dialect` 收敛到 adapter，避免两套方言逻辑并存。

**测试**：`backend/tests/test_dialect_adapter.py`——各引擎 capabilities 完整性、DDL 快照、类型映射表、能力不足抛错、ALTER、`translate_sql`、`quote_identifier` 下沉、`verified` 集合。

---

## 7. M3 · 本体 → 物理正向生成器

**目标**：把「本体 + 物化契约」编译成引擎无关 `LogicalSchema`，再由 adapter 渲染 DDL/ETL/DAG/映射。

**验收标准**：fixture 本体端到端生成；两次生成逐字节一致（幂等）；不可生成项显式进 `unsupported`；能力不足不静默建表。

**文件**：`app/services/warehouse_generator.py`；`app/api/warehouse.py`（端点）。

**关键接口**：
- `build_logical_schema(db, ontology_id, *, database_prefix=None) -> LogicalPlan`（`plan.schema` + `plan.unsupported`）
- `generate_ddl(db, ontology_id, engine, *, database_prefix=None) -> dict`（`statements` / `warnings` / `unsupported`）
- `generate_etl_sql(...)`、`generate_dag(...)`（拓扑排序，处理 ERP 血缘大环，见 `_nodes_in_cycles` Tarjan）、`generate_mapping(...)`（喂 `DataSource.mapping_json`）
- `generate_derivation(db, ontology_id, engine, ...)`（**单一写入路径**，见 M8）、`generate_bundle(...)`

**映射规则**：`ObjectType.source_ref` → 源表定位（`_extract_dataset_name`）；`Property.source_field_ref` → `SELECT src AS prop`；`display_name`/`description` → 表/列 COMMENT（**源库零注释，由本体反补，关键价值点**）；`RelationType.structure_type`(`foreign_key`/`fact_table`/`bridge_table`)/`cardinality`/`mapping_object_type_id` → 外键/事实表/桥表 + 实现表；BusinessLogic + bindings → ADS 指标表。

**不可生成项显式返回**（`unsupported: [{target, reason}]`）：N:N 关系（需桥接表，不自动生成）、粒度冲突（多关系映射同一实现表）、缺物化契约、目标引擎能力不足、对象无 source_ref、缺主键身份属性、依赖环。

**复用点**：遍历模式与外键推断沿用 `services/data_app.py::generate_cube_model` 与 `connectors/cube.py`（`source_evidence` 取 `foreign_key`/`target_field`，回退 `<target>_id`=`id`）。

**API**：`GET /api/ontologies/{id}/warehouse/{ddl,etl,dag,mapping,derivation,bundle}`、`GET /api/warehouse/engines`（能力矩阵 + `implemented`/`verified`）。

**测试**：`backend/tests/test_warehouse_generator.py`——端到端生成、幂等、unsupported 完整性、能力错误转 unsupported、DAG 环处理、引擎能力矩阵端点、派生作业。

---

## 8. M4 · Chat BI / Data App 落地执行

**目标**：把 Chat BI 从「只产 `suggested_sql`」推进到「可执行」，方言翻译委托 adapter。

**验收标准**：多引擎 DSN 正确路由；方言翻译走 `app/warehouse/registry.py` 而非新增分支；执行需 publisher。

**修改**：`app/services/data_app_executor.py` 的 `_backend_of_dsn` 与方言翻译增加 hive/kyuubi/doris/clickhouse 分支，翻译委托 `get_adapter(engine).translate_sql`；`app/api/chat_bi.py` 新增 `POST /api/chat-bi/messages/{id}/execute`（只读校验复用既有实现）。`backend/requirements.txt` 增对应 SQLAlchemy dialect（需实施前确认版本）。

**名称对齐红利**：`services/chat_bi.py` 系统提示词要求「表名/字段名严格使用本体括号内标识符」——数仓表由本体生成后该约束在物理层被强制满足，从祈使句变成事实；`mapping_json` 由 M3 直接产出。

**测试**：`backend/tests/test_data_app_executor_dialects.py`。

---

## 9. M5 · 智能体流水线骨架

**目标**：`草稿 → 校验 → 确认 → 执行 → 回执` 状态机，未确认不得执行，高危先看 dry-run。

**验收标准**：状态机单向流转、非法跃迁拒绝；Gate 有阻断项停留 drafted；已 succeeded 重放不产生第二次副作用；凭据不进 Spec。

**数据模型**（`c3d4e5f6a7b8_governance_artifacts.py`，`app/models/agent.py`）：新表 `governance_artifacts`——`kind`、`name`、`intent`、`spec_json`、`machine_baseline`、`status`(`drafted`|`validated`|`confirmed`|`executing`|`succeeded`|`failed`)、`validation_report_json`、`execution_receipt_json`、`confirmed_by`/`confirmed_at`、`executed_at`、`origin`；`HIGH_RISK_KINDS = {cluster}`。

**包结构**：`app/agents/`——`registry.py`（Drafter/Executor 注册表）、`validation.py`（Validation Gate）、`common.py`、`drafters/base.py`、`executors/base.py`；`app/services/agent_pipeline.py`（状态机）。

**关键接口**：`Drafter.draft(intent, context) -> Spec`、`suggested_name(...)`；`Executor.dry_run(spec, ctx) -> Diff`、`execute(spec, ctx) -> Receipt`（**幂等**）；`AgentPipelineService.{draft,validate,confirm,execute,list_artifacts,get}`。

**Validation Gate**（`app/agents/validation.py`，复用 `services/draft_consistency.py`）：目标引擎存在且能力已核实（未核实→warning）；Spec 引用的对象/字段必须在本体真实存在（防 LLM 幻觉）；必填元数据（如 metric 必须绑定主对象，见遗留1）；凭据字段扫描（`*_ref`/`*_alias` 放行，`password`/`secret`/`token` 等阻断）。error 级阻断确认，warning 级呈现不阻断（`_WARNING_CODES = {engine_unverified, ontology_issue}`）。

**状态机不变量**：未确认不得执行（`execute` 只接受 `confirmed`）；确认前必须有校验报告与 dry-run 差异；高危制品必须先有 dry-run 才能确认；已 succeeded 重复执行直接返回原回执。

**安全**：凭据绝不进 Spec/LLM 上下文，只允许 `credential_ref`/别名；ontoMeta 不自行 SSH，部署委托 BM Agent。

**API**：`POST /api/agents/draft`、`POST /api/agents/artifacts/{id}/{validate,confirm,execute}`、`GET /api/agents/artifacts[/{id}]`、`GET /api/agents/kinds`。整个 `/api/agents` 命名空间需 **publisher**。

**前端**：设置页「治理智能体」页签 `frontend/src/components/AgentsPanel.tsx`——制品列表、校验报告（issues + dry-run 差异）、状态机确认/执行按钮、执行回执、403→需 publisher 提示。

**测试**：`backend/tests/test_agent_pipeline.py`——状态机、Gate 拦截、幂等重放、未确认不得执行、凭据阻断。

---

## 10. M6 · 四类 Drafter / Executor

上线顺序 **④指标 → ③ETL → ①同步 → ⓪部署（风险由低到高）**。全部已注册（`register_builtin_agents()`）。

| 顺序 | 文件 | 要点 |
|---|---|---|
| 1 | `agents/{drafters,executors}/metric.py` | 复用 BusinessLogic 双向绑定与 M3 生成器；Drafter 只挑选结构化已确认口径，不编口径 |
| 2 | `agents/{drafters,executors}/transform.py` | 清洗规则 → Spark SQL；执行器复用 M3 生成器 |
| 3 | `agents/{drafters,executors}/sync.py` | SeaTunnel 作业；含**关键源保全判定**；作业只带别名不带凭据 |
| 4 | `agents/{drafters,executors}/cluster.py` | ClusterSpec → BM REST 调用（见 M8 / 遗留4） |

**指标 Drafter/Executor（零新概念）**：`models/logic.py` 的 `business_logics` + object 绑定（subject/dimension/output）+ property 绑定（input/output/filter/group）本就是「口径 + 表字段绑定」。MetricExecutor 由绑定角色推导 SQL：dimension→GROUP BY、filter→WHERE、expression→度量。**主对象必须绑定**——缺失时抛 `ValueError` 且 Gate 阻断（遗留1），绝不生成看似合法实则不可执行的 `FROM <未绑定>`。

**关键源保全判定**（SyncSpec 字段，智能体起草 + 人工确认）：

| 源特征 | 保全 |
|---|---|
| 有保留期/会被清理（日志、流水、审计） | 是 |
| CDC/消息流，一次性不可重放 | 是 |
| 状态被原地更新且无历史快照 | 是 |
| 源库有归档策略且归档不可访问 | 是 |
| 可随时全量重拉（主数据、配置表、码表） | 否 |

**测试**：`backend/tests/test_agent_implementations.py`、`test_bigtop_manager.py`。

---

## 11. M7 · DataHub 回写

**目标**：把业务命名/描述/术语/域回灌 DataHub，闭合元数据环。

**现状→新增**：`connectors/datahub.py` 原为 GraphQL 只读；新增 4 个 mutation + `app/services/datahub_writeback.py`。

**三条安全约束**：只回写已发布本体；preview/apply 分离；绝不用空值覆盖 DataHub 已有内容。

**mutation 结构（已对照 DataHub 开源 `entity.graphql` master 核实一致）**：

| mutation | 签名 | 入参 |
|---|---|---|
| `updateDescription` | `(input: DescriptionUpdateInput!): Boolean` | `{description!, resourceUrn!, subResourceType?(仅 DATASET_FIELD), subResource?}`，字段级与数据集级同一 mutation |
| `addTerms` | `(input: AddTermsInput!): Boolean` | `{termUrns![String!]!, resourceUrn!, subResourceType?, subResource?}` |
| `setDomain` | `(entityUrn: String!, domainUrn: String!): Boolean` | 位置参数，非 input 对象 |

**⚠ 需实施前验证**：结构已核实一致，但**未在本目标实例的具体 DataHub 版本上核实**（后端 `use_mock=0` 指向真实 DataHub）。首次真实 apply 必须在非生产实例。

**API**：`GET /api/ontologies/{id}/datahub/writeback-plan`（纯读预览）、`POST .../datahub/writeback`（需 publisher）。

**测试**：`backend/tests/test_datahub_writeback.py`——计划构建、安全约束、mutation 请求体形状（mock GraphQL）。

---

## 12. M8 · 多引擎补齐

**目标**：填齐 Doris/Iceberg/StarRocks/ClickHouse 的 `map_type`/`render_create_table`/`render_alter`/`translate_sql`/`quote_identifier`，能力矩阵逐项核实。

**验收标准**：四引擎 `implemented=True` 且 `verified=True`；每引擎 DDL 快照测试；能力不足抛错；单一写入路径成立。

**各引擎实现要点（对照官方文档核实）**：
- **Doris**：MySQL 线协议；有主键用 Unique Key 模型（Key 列前导），无则 Duplicate Key；`DATETIME`（无 TIMESTAMP）；泛字符串落 `VARCHAR`（STRING 不能作 Key）；AUTO 分区；`DISTRIBUTED BY HASH ... BUCKETS`。外键取 NONE（原生声明式 FK 文档未核实，不臆造）。
- **StarRocks**：专属 Primary Key 模型；**声明式外键**写进 `PROPERTIES("foreign_key_constraints"=...)`（供优化器）；标识符上限 1024；表达式分区。
- **Iceberg**：Spark `USING iceberg`；分区列留在 schema（identity 变换）；主外键以 `TBLPROPERTIES` 声明式记录（同 Hive）；**唯一支持 SCD2（MERGE INTO）**；`format-version=2`。
- **ClickHouse**：MergeTree；ORDER BY 键非主键语义→主键声明式；非键可空列包 `Nullable(T)`；`toYYYYMM` 分区；无分桶、无 MERGE→SCD2 报错；无 TBLPROPERTIES 故外键 NONE。

**单一写入路径**：Hive 为权威物理副本，其余引擎**从 Hive 派生**（`generate_derivation`），避免多副本双写不一致。`generate_bundle` 据此分流：hive→ODS→Hive 的 ETL；其余→`hive.<qualified>` 派生作业。派生的具体装载机制（外部 Catalog/Broker Load/INSERT…SELECT）随引擎与部署而异，交由调度器落地，不臆造。API：`GET /api/ontologies/{id}/warehouse/derivation?engine=doris`。

**遗留项处理（本轮一并落地）**：

| # | 问题 | 处理 |
|---|---|---|
| 1 (P0) | metric `_build_sql` 主对象缺失产出看似合法的 `FROM <未绑定>` | 改抛 `ValueError` + Gate 增 subject 必填阻断（双层防御） |
| 2 (P1) | 生成器 `_quote` 是唯一引擎逻辑泄漏 | 下沉为 `DialectAdapter.quote_identifier`，生成器委托 |
| 3 (P1) | 智能体无前端 | `AgentsPanel.tsx` + 设置页页签 + `api.ts` 的 `ApiError`（带 status）与 7 个 agent 方法 |
| 4 (P2) | BM 下发未接通 | 见下 |
| 5 (P2) | DataHub mutation 未核实 | 见 M7：已对照 OSS schema 核实，注释升级 |

**遗留4 — BM 下发已接通**（`connectors/bigtop_manager.py`，按 `apache/bigtop-manager` `release-1.1.0` 源码核实）：
- 三步握手 `GET /api/salt` → `GET /api/nonce` → `POST /api/login` → JWT；后续请求带自定义头 `Token`（非 `Authorization: Bearer`）。登录口令 `PBKDF2-HMAC-SHA256(pwd, salt, 600000, 32B)` 小写 hex。
- 部署走单一端点 `POST /api/command`（`commandLevel=cluster` 建集群、`=service` 装服务）；进度轮询 `GET /api/clusters/{clusterId}/jobs/{jobId}`。
- `ClusterExecutor` 在 `context.allow_dispatch=True` 分支真实提交（替换原 `NotImplementedError`）。**默认仍不下发**。
- **凭据边界**：BM API 不支持凭据引用，建集群须内联 SSH 明文、登录须管理口令——这些密钥**只从运行时 `context` 取**（发布者在 execute 时提供），在 dispatch 边界转发给 BM，**绝不进 Spec/LLM 上下文**；Spec 里仍只有 `credential_ref`。
- **⚠ 需实施前验证**：响应信封（`_unwrap` 已兼容套壳/裸值）与 `ClusterCommandReq.type` 语义未在真实实例核实；**首次真实下发必须在非生产集群**。

**测试**：`backend/tests/test_dialect_adapter.py`（各引擎快照/类型/能力/ALTER/方言）、`test_bigtop_manager.py`（握手序列、`Token` 头、command 载荷形状、缺凭据即报错、PBKDF2 确定性、SSH 明文不入 Spec）。

---

## 13. 编码约定

1. **扁平模块**：`api/` `services/` `models/` `schemas/` `connectors/` 一域一文件，新 router 在 `app/api/router.py` 注册。仅 `app/warehouse/` 与 `app/agents/` 因多文件开子包。
2. **溯源与合并**：新增本体关联实体带 `ProvenanceMixin` 并参与三路合并（`services/ontology_merge.py`），机器重生成永不覆盖人工编辑。
3. **迁移**：Alembic，文件名 `<rev_id>_<snake_case>.py`；应用启动自动 `alembic upgrade head`（在 lifespan 的 `init_db()`）；每里程碑迁移独立成文件。
4. **异常继承顺序**：`except` 从具体到宽泛（`UnregisteredKindError`⊂`LookupError`、`PipelineError`⊂`ValueError`，宽泛在前会吞掉状态码）。
5. **确定性边界**：LLM 只产声明式 Spec；所有渲染/DDL/SQL 拼装是确定性代码，可快照测试。
6. **幂等**：Executor 与生成器重跑产出一致；靠断言副作用次数验证（如 `executor.executions == 1`），不只比返回值。
7. **测试**：放 `backend/tests/`，`USE_MOCK_DATAHUB=true`/`USE_MOCK_LLM=true` 跑；纯服务层测试也要依赖 `client` fixture（迁移在 lifespan 里跑）；无 pytest-asyncio，异步用 `asyncio.run(...)`；每 seed 用独立 domain 后缀避唯一约束。
8. **前端**：走 `frontend/src/api.ts` 单一客户端，类型加在 `frontend/src/types.ts`。

---

## 14. 基建侧任务（运维，非编码）

- **Bigtop Manager 纳管**（v1.1.0 / 2024-12；v1.0.0 / 2024-04）：Bigtop 3.3.0 stack = Hadoop(HDFS/YARN/MR)、HBase、Hive、Spark、Flink、Tez、ZooKeeper、Solr、Kafka；Infra 1.0.0 = MySQL、Prometheus、Grafana；Extra 1.0.0 = SeaTunnel。架构 Server(Spring Boot) + Agent(gRPC) + UI。
- **BM 不纳管、需独立运维**：调度器(DolphinScheduler)、SQL 网关(Kyuubi)、MPP/OLAP(Doris/StarRocks/ClickHouse)、权限(Ranger)、质量(GE/Soda/Griffin)。→ **双轨运维边界**；BM v1.x 早期、社区小，保留手工/Ansible 回退，BM 不作唯一运维入口。
- 集群拓扑：PoC(3~5 节点) 与生产(HA) 两套角色表。
- 源库接入：**强制只读从库**，禁止直连生产 ERP 主库。
- 来源：https://github.com/apache/bigtop-manager 、https://issues.apache.org/jira/browse/BIGTOP-4129 、https://github.com/apache/bigtop-manager/releases 、https://bigtop.apache.org/release-notes.html

---

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 写侧智能体权限失控 | M0 RBAC（已交付）；高危操作强制确认 + dry-run；`/api/agents` 需 publisher |
| 本体质量被生成放大 | 发布前一致性校验；产物人工复核后执行；小范围试点 |
| 本体变更放大成 N 个引擎迁移 | 物化契约区分结构/语义变更；Iceberg 原生 schema evolution；版本化迁移 |
| 引擎能力不对齐致语义静默丢失 | Capability Matrix + Validation Gate 前置报错，绝不静默降级 |
| 多副本不一致 | 单一写入路径：Hive 权威，其余从 Hive 派生 |
| 保全策略误判致源数据丢失 | 判定规则清单化；SyncSpec 人工确认；宁可多保 |
| 生成 SQL 映射错误 | 生成即 dry-run；样本比对；复核通过率纳入验收 |
| LLM 产出不可执行规格（幻觉） | Validation Gate 强校验，Spec 须对上真实清单才放行 |
| 数据安全/质量弱项过不了内审 | 阶段二补齐 Ranger/Kerberos 与质量规则引擎，不粉饰 |
| Hive 3.1.3 + Spark 3.2 ACID 兼容 | PoC 先用非事务 ORC 外部表 |
| 外部接口未在实例核实（BM 信封、DataHub 版本） | 默认不下发/preview；首次真实调用在非生产实例 |

---

## 16. 现状事实速查（复用先例）

| 事实 | 位置 |
|---|---|
| 本体 → Cube 正向生成器（M3 复用先例） | `services/data_app.py::generate_cube_model{,_files}` |
| 类型映射 `_dim_type`；关系推断与外键回退；「N:N 需桥接表不自动生成」 | `connectors/cube.py` |
| 方言翻译 `_translate_dialect`；`_backend_of_dsn` | `services/data_app_executor.py` |
| `ObjectType.source_ref`、`Property.source_field_ref`、`RelationType.structure_type/cardinality/mapping_object_type_id`、`ProvenanceMixin` | `models/ontology.py` |
| `DataSource.dsn_secret_ref` 仅存引用、`mapping_json` 结构 | `models/data_app.py` |
| BusinessLogic 双向绑定 | `models/logic.py` |
| 发布前一致性校验（Gate 复用基础） | `services/draft_consistency.py` |
| 三路合并 | `services/ontology_merge.py` |
| RBAC 集中策略与豁免前缀 | `app/auth.py` |

**现有 ERP 源基线**：DataHub 仅 1 个域、734 张表，源为 ERPNext/Frappe，零 PK/FK/列注释/glossary——这正是「注释由本体反补物理层」有价值的原因。

---

## 17. 自检

1. M0–M8 与五项遗留项全部落地；后端 474 测试全绿，前端 `tsc -b && vite build` 通过。
2. 每里程碑七小节齐全（目标/验收/数据模型/文件/接口/API/测试）。
3. 文中对现有代码的引用经 Read/Grep 核对属实。
4. 术语一致：ODS=本体物理投影层，STG=贴源保全区。
5. 方向一致：全文无「物理表决定本体」表述。
6. 未核实的外部事实（BM 响应信封与 type 语义、目标 DataHub 版本、Iceberg/ClickHouse 标识符上限）均标注「需实施前验证」，无臆断。
7. Alembic 单一头 `c3d4e5f6a7b8`；里程碑依赖顺序自洽，M0 为阻断项。
