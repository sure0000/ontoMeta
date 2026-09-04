# MCP 架构改造

> **核心论点**：ontoMeta 的护城河是本体工程能力和领域工具链，而非特定的 agent 实现。

通过 MCP (Model Context Protocol) 暴露核心能力，配合通用 agent（如 Claude Code），可以获得更强的推理能力、持续升级、跨系统协同，并实现真正的平台化。

---

## 📚 文档导航

### 核心设计文档

1. **[MCP_ARCHITECTURE_REDESIGN.md](./MCP_ARCHITECTURE_REDESIGN.md)** - 架构设计总览
   - 战略方向和核心论点
   - 目标架构和设计原则
   - 当前架构分析和核心引擎评估
   - 执行摘要和风险评估

2. **[MCP_TOOL_DESIGN.md](./MCP_TOOL_DESIGN.md)** - 工具详细设计
   - 20 个 MCP 工具的完整定义
   - 参数、返回值、约束层设计
   - 与现有引擎的映射关系

3. **[MCP_IMPLEMENTATION_PLAN.md](./MCP_IMPLEMENTATION_PLAN.md)** - 实施计划
   - 5 个阶段的详细实施步骤
   - 时间表和里程碑
   - 测试策略和验收标准

### 快速理解

**10 秒版本**：
- 当前：Data Agent (7000+ 行) 作为唯一入口
- 未来：20 个 MCP 工具 + 通用 agent
- 收益：代码降低 70%，维护降低 50%，能力更强

**1 分钟版本**：

| 维度 | 当前 | 改造后 | 提升 |
|------|------|--------|------|
| **推理能力** | 受限于特定模型 | 通用 agent 持续升级 | ⬆️ |
| **代码复杂度** | 7000+ 行胶水代码 | 20 个薄工具包装 | ⬇️ 70% |
| **维护成本** | 模型升级需调整 | 工具稳定，自动适配 | ⬇️ 50% |
| **生态整合** | 孤立系统 | MCP 生态（Confluence、GitHub） | ⬆️ |
| **平台化** | 只能在本项目使用 | 可被其他应用调用 | ⬆️ |

**5 分钟版本**：阅读 [MCP_ARCHITECTURE_REDESIGN.md](./MCP_ARCHITECTURE_REDESIGN.md) 的"执行摘要"部分

---

## 🎯 核心发现

### ✅ 护城河已验证

经过完整代码探索，证实核心能力都是独立可调用的：

| 引擎 | 文件 | 行数 | Agent 依赖度 | 改造成本 |
|------|------|------|-------------|---------|
| **本体建模** | draft_generator.py | 1491 | **极低** | 低 |
| **任务生成** | metric_compiler.py | 1097 | **低** | 低 |
| **语义导航** | semantic_navigator.py | 260 | **低** | 低 |
| **治理规约** | lint.py | 489 | **零** | 极低 |
| **接地验证** | answer_verifier.py | 527 | **中** | 低 |

**结论**：所有核心引擎都可以直接包装为 MCP 工具，不需要重构。

### ⚠️ 当前 Data Agent 的局限

- **chat_bi.py**：7000+ 行，主要是工具分发和结果投影（胶水代码）
- **约束混杂**：意图门控、工具收窄、接地验证分散在提示词和代码中
- **6 环僵化**：确认流程应该由前端控制，不是 agent 职责
- **孤立系统**：无法与其他系统（Confluence、GitHub）协同

### 🚀 MCP 改造优势

1. **更强推理**：通用 agent（Claude Opus 5）比当前 Data Agent 更智能
2. **代码简化**：删除 7000+ 行胶水代码，保留核心引擎
3. **持续升级**：底层模型升级自动受益，不需要调整 agent 层
4. **跨系统协同**：通过 MCP 与 Confluence、GitHub、Slack 等协同
5. **真正平台化**：其他应用可以调用 ontoMeta 的能力

---

## 📋 工具清单

### Data Agent parity 生命周期（已实现）

MCP 现在可以在不绕道 REST 的情况下编排治理任务：`propose_*` 预览后，用 editor
调用 `draft_task`（落治理草稿并自动校验）和 `validate_task`；只有 publisher 才能调用
`confirm_task` 与异步 `execute_task`。执行工具立即返回，最终 Airflow/数据状态通过
`get_task_status` 轮询。外部 agent 默认使用最小权限 Principal Token，不要暴露
`ONTOMETA_ADMIN_TOKEN` 或让 agent 读取 `backend/.env`。

`query_objects` 支持 `group_by=role|segment` 聚合模式，先返回分布再按需分页取明细，
避免把大型本体的全部对象塞进 agent 上下文。

**29 个已注册工具**（以 `app/mcp/tools/` 的 `TOOL_REGISTRY` 为准；括号内是服务器强制的最低角色）：

### 本体与口径查询（8 个）
- `query_ontology`（reader）- 查询本体结构和业务对象列表。可以查询所有本体，或按 ID 查询特定本体
- `get_ontology_overview`（reader）- 一次返回本体元信息、对象角色/板块分布和业务对象精简清单。用于快速建立本体地图；需要完整字段时再调用 query_objects 或 query_object_detail
- `query_objects`（reader）- 查询本体中的业务对象（表/实体）。可按角色、关键词过滤，或用 group_by=role/segment 只取分布统计。关键词同时匹配对象标识名、显示名、描述和物理源表名（source_ref）
- `query_object_detail`（reader）- 查询单个业务对象的详情：属性（字段）、进出关系、绑定的业务口径、物理落点
- `query_relations`（reader）- 查询本体中的业务对象关系（外键/引用/包含/转化）。关系两端给的是对象名，写 JOIN 时的连接键在 source_evidence 里
- `search_logics`（reader）- 按关键词检索业务口径：指标（GMV/客单价）、标签（客户分层）、规则（金额必须为正）。关键词匹配标识名、显示名与描述；默认只看已发布口径
- `get_logic`（reader）- 查询单个业务口径的完整定义：表达式（文字口径 + 形式化 AST）、绑定的业务对象与字段、ADS 落点。要拿可执行 SQL 用 compile_metric，不要照着表达式自己重写
- `compile_metric`（reader）- 把一条**已发布且已形式化**的口径按给定维度/过滤/时间粒度编译成 Doris SQL，并返回口径展开轨迹（caliber_trace）、JOIN 路径与语义证书

### 血缘 / 落点 / 运行记录（3 个）
- `get_lineage`（reader）- 查某个业务对象的血缘与上下游邻域（中心对象 + depth 跳关系）
- `get_landing`（reader）- 读已发布业务对象或业务口径的**真实物理落点**：落到哪张表、表建了吗、数搬了吗、现在能不能查
- `get_ops_record`（reader）- 读**已经发生过**的权威运行记录，只读，不创建也不执行任何任务。按 family 选族：

### 取数辅助（2 个）
- `find_join_path`（reader）- 查两个业务对象之间**本体认可的**关联路径：每一跳的关系、ON 连接键、基数链，以及可直接用的 `sql_hint`（FROM/JOIN 片段）
- `profile_values`（publisher）- 查某个字段**实际存着什么值**：类别/标识字段给 TopN 取值与频次、去重数；度量字段给最小/最大/均值；时间字段给时间区间；另有空值率

### 数据源与 SQL（3 个）
- `list_datasources`（reader）- 列出已配置的数据源：业务源库（business_source）与数仓（warehouse）。建同步任务时源端取 business_source、目标端取默认 Doris 仓。不返回任何凭据
- `validate_sql`（reader）- 校验 SQL 是否为合法的单条只读查询。不连数据库、不执行
- `execute_sql`（publisher）- 在默认 Doris 数仓执行只读 SQL 并返回结果行

### 任务提案（4 个）
- `propose_sync`（editor）- 生成数据同步任务提案：把源库表搬进数仓 ODS。落点恒为 ODS 库、表名 ods_{数据域}_{原表名}，不可指定。只出提案，不写库、不执行
- `propose_transform`（editor）- 生成数据加工（清洗/转换）任务提案：读已同步就绪的 ODS，产出加工结果表。只出提案，不写库、不执行
- `propose_materialize`（editor）- 生成本体物化任务提案：把本体对象建成物理表（只出建表 DDL，不搬数据）。人工建模、没有物理源表的对象要先物化。只出提案，不写库、不执行
- `propose_metric`（editor）- 生成指标（聚合）任务提案：按已发布的业务口径产出 ADS 结果表。只出提案，不写库、不执行

### 任务生命周期与追踪（6 个）
- `draft_task`（editor）- 把 propose_* 返回的 draft_payload 落成治理任务并立即校验。只写治理草稿并做 dry-run，不确认、不执行数仓变更
- `validate_task`（editor）- 重跑治理任务的校验闸门与 dry-run；不确认、不执行
- `confirm_task`（publisher）- 确认一个已通过校验的治理任务。publisher 令牌代表调用方已获执行授权；本工具只确认，不触发执行
- `execute_task`（publisher）- 异步执行一个已确认的治理任务并立即返回。返回成功只表示已受理；最终结果必须用 get_task_status 轮询
- `list_tasks`（reader）- 列出数据治理任务（同步 sync / 加工 transform / 聚合 metric / 物化 materialize）。可按类型、状态、本体过滤。只读；读的同时会对账 Airflow 状态，不触发执行
- `get_task_status`（reader）- 回读单个数据任务的状态、Spec、校验报告与执行回执，并尽力回读 Airflow DagRun 的实时状态（读不到就退回制品态）。只读，不触发执行

### 自省 / 审计 / 监控（3 个）
- `server_info`（reader）- 回读本 MCP 服务器状态：版本、传输方式、工具清单与各自最低角色、当前会话身份、限流配置、审计表可达性。用于自查「我这条会话是什么权限、某工具为什么被拒」
- `list_audit_logs`（publisher）- 回读 MCP 工具调用审计日志（谁、什么身份、调了哪个工具、成没成、是否被授权拦下）。按时间倒序，可按工具名、是否成功、是否被拒过滤。仅 publisher
- `get_mcp_stats`（publisher）- 基于审计表的 MCP 使用统计：总调用量、成功/失败/被拒/被限流数、按工具与角色分组。仅 publisher
### 尚未实现（设计稿里的名字，别当成可调用工具）

下列工具只存在于 `MCP_TOOL_DESIGN.md` 等设计稿中，**registry 里没有**，调用会直接失败。
它们对应 Data Agent 仍未追平的能力面，按优先级排：

- 资产目录：`list_datasets`（`get_lineage`/`get_landing`/`get_ops_record` 已实现，见上）
- 取数辅助：`locate_entities`、`scout_query`（`find_join_path`/`profile_values` 已实现，见上）
- 治理规约：`lint_against_standard`、`validate_against_policy`、`get_active_governance_standard`
- 本体建模：`propose_ontology_draft`、`propose_dimensional_model`、`propose_logic_batch`、
  `create_modeling_case`（写侧或长耗时，要先想清 MCP 下「异步 + 人工确认」怎么表达）
- 数据接入：`list_onboarding_targets`、`propose_datasource`
- 任务链与呈现：`propose_pipeline`、`propose_panel`、`propose_dashboard`

---

## 🗓️ 时间表

| 阶段 | 内容 | 时间 | 里程碑 |
|------|------|------|--------|
| **Phase 1** | 基础设施搭建 | 2-3 天 | 第一个工具可用 |
| **Phase 2** | 核心工具迁移 | 2-3 周 | 20 个工具全部可用 |
| **Phase 3** | 前端适配 | 1-2 周 | 前端可调用 MCP |
| **Phase 4** | 切换与验证 | 1 周 | 完全切换 |
| **Phase 5** | 开放与平台化 | 1-2 周 | 对外开放 |
| **总计** | | **6-8 周** | **完成改造** |

---

## 🎬 下一步

### 立即行动

1. **评审设计文档**（本周）
   - [ ] 团队评审三份核心文档
   - [ ] 达成改造共识
   - [ ] 确定优先级和时间表

2. **启动 Phase 1**（Week 1）
   - [ ] 添加 MCP SDK 依赖
   - [ ] 搭建 MCP 服务框架
   - [ ] 实现第一个测试工具（`query_ontology`）
   - [ ] 验证端到端可用性

3. **持续推进**（Week 2-8）
   - [ ] 按计划迁移工具
   - [ ] 适配前端
   - [ ] A/B 测试
   - [ ] 完全切换

### 需要决策的问题

1. **时间投入**：是否同意投入 6-8 周进行改造？
2. **优先级**：是否暂停其他功能开发，集中资源？
3. **风险接受度**：是否接受渐进式迁移的风险？

---

## 📞 联系

- **设计负责人**：Claude Code
- **创建日期**：2026-09-03
- **分支**：`mcp`
- **状态**：设计完成，等待评审

---

## 🔗 相关资源

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Claude Code 文档](https://claude.ai/code)

---

**开始探索**：阅读 [MCP_ARCHITECTURE_REDESIGN.md](./MCP_ARCHITECTURE_REDESIGN.md)
