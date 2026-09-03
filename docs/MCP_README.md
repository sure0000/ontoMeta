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

20 个 MCP 工具，覆盖所有现有功能：

### 本体建模工具（4 个）
- `infer_ontology_from_datahub` - 从 DataHub 推断本体
- `classify_business_objects` - 分类业务对象
- `infer_relationships` - 推断对象关系
- `validate_ontology` - 按规约校验本体

### 数据任务工具（6 个）
- `propose_sync_task` - 提议同步任务
- `propose_transform_task` - 提议清洗任务
- `propose_materialize_task` - 提议物化任务
- `propose_metric_task` - 提议指标任务
- `get_task_status` - 查询任务状态
- `list_task_runs` - 列出运行记录

### 查询工具（6 个）
- `query_ontology` - 查询本体结构
- `query_business_objects` - 查询业务对象
- `query_relationships` - 查询关系
- `execute_sql` - 执行只读 SQL
- `get_lineage` - 查询血缘
- `get_landing` - 查询物理落点

### 治理工具（3 个）
- `validate_against_policy` - 规约校验
- `lint_task_spec` - 检查 spec 合规性
- `get_active_governance_standard` - 获取当前规约

### 运维记录工具（1 个）
- `get_ops_record` - 查询各类运维记录（13 个族）

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
