# Data Agent vs MCP 架构对比

## 架构演进

```
┌─────────────────────────────────────────────────────────────┐
│                    当前架构（Data Agent）                     │
└─────────────────────────────────────────────────────────────┘

用户 → 前端 → /api/chat-bi/ask → chat_bi.py (7000行)
                                      ↓
                              ┌───────┴────────┐
                              │ 意图门控        │
                              │ 技能路由        │
                              │ 工具分发(30+)   │
                              │ 接地验证        │
                              │ 6环确认         │
                              │ 渲染块投影      │
                              └───────┬────────┘
                                      ↓
                              核心引擎（独立）


┌─────────────────────────────────────────────────────────────┐
│                    目标架构（MCP + 通用 Agent）               │
└─────────────────────────────────────────────────────────────┘

用户 → 前端 ──────┬────→ MCP 工具(20个) → 核心引擎
                  │
                  └────→ 通用 Agent (Claude Code / Claude Desktop)
                              ↓
                         智能推理 + 工具编排
```

---

## 详细对比

### 1. 核心组件对比

| 组件 | 当前 Data Agent | MCP 架构 | 差异 |
|------|----------------|----------|------|
| **Agent 层** | 专用 Data Agent<br>7000+ 行 chat_bi.py | 通用 Agent<br>(Claude Opus 5 等) | 推理能力更强<br>持续升级 |
| **工具层** | 30+ 个工具<br>分散在 tool_schemas | 20 个 MCP 工具<br>统一注册表 | 精简、标准化 |
| **约束层** | 混杂在代码和提示词 | 在 MCP 工具内部 | 清晰、可测试 |
| **业务逻辑** | 核心引擎（独立） | 核心引擎（独立） | 无变化 ✅ |
| **前端** | 调用专用 API | 调用 MCP 工具 | 更灵活 |

### 2. 代码复杂度对比

#### 当前 Data Agent

```
backend/app/services/
├── chat_bi.py                     7,000+ 行  ← 工具分发、编排
├── chat_bi_tool_schemas.py        2,200+ 行  ← 工具定义
├── chat_bi_skills.py                302  行  ← 技能系统
├── chat_bi_ledger.py             25,000+ 行  ← 六环确认
├── chat_bi_blocks.py                186  行  ← 渲染块
├── chat_bi_references.py            ???  行  ← 引用解析
├── agent_grounding.py               234  行  ← 接地验证
├── answer_verifier.py               527  行  ← F4 核验
└── ...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总计：~35,000+ 行专用 agent 代码
```

#### MCP 架构

```
backend/app/mcp/
├── server.py                        ~100 行  ← MCP 服务入口
├── tools/
│   ├── __init__.py                  ~100 行  ← 工具注册表
│   ├── base.py                      ~200 行  ← 基类和工具
│   ├── query/                    ~1,000 行  ← 6 个查询工具
│   ├── task/                     ~1,500 行  ← 6 个任务工具
│   ├── ontology/                 ~1,000 行  ← 4 个本体工具
│   └── governance/                 ~500 行  ← 3 个治理工具
└── ...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总计：~4,400 行（薄包装层）

+ 核心引擎：不变，仍然是 ~5,000 行
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总新增代码：~4,400 行
可删除代码：~35,000 行
净减少：~30,600 行（-87%）
```

### 3. 工具对比

#### 当前 Data Agent（30+ 个工具）

**基础工具（7个）**
- `search_objects` - 搜索对象
- `get_object` - 获取对象详情
- `search_relations` - 搜索关系
- `search_logics` - 搜索口径
- `get_logic` - 获取口径详情
- `get_domain_overview` - 域概览
- `select_skill` - 技能选择

**任务工具（8个）**
- `propose_sync`
- `propose_transform`
- `propose_materialize`
- `propose_metric`
- `propose_onboard`
- `propose_action`
- `get_task_status`
- `get_task_options`

**查询工具（5个）**
- `run_sql`
- `get_lineage`
- `get_landing`
- `search_data_sources`
- `get_data_source`

**表单工具（4个）**
- `propose_form` - 提议表单
- `confirm_action` - 确认动作
- `get_preference` - 获取偏好
- `propose_preference` - 提议偏好

**建模工具（3个）**
- `get_modeling_case`
- `lint_against_standard`
- `get_ops_record`

**其他（3个）**
- `clarify_question`
- `refuse_answer`
- `get_conversation_summary`

**问题**：
- ❌ 太多低频工具（如 `get_preference`）
- ❌ 表单工具应该是前端职责
- ❌ 内部辅助工具（如 `select_skill`）不应暴露
- ❌ 工具职责不清晰

#### MCP 架构（20 个工具）

**本体建模（4个）** - 核心能力
- `infer_ontology_from_datahub`
- `classify_business_objects`
- `infer_relationships`
- `validate_ontology`

**数据任务（6个）** - 核心能力
- `propose_sync_task`
- `propose_transform_task`
- `propose_materialize_task`
- `propose_metric_task`
- `get_task_status`
- `list_task_runs`

**查询（6个）** - 核心能力
- `query_ontology`
- `query_business_objects`
- `query_relationships`
- `execute_sql`
- `get_lineage`
- `get_landing`

**治理（3个）** - 核心能力
- `validate_against_policy`
- `lint_task_spec`
- `get_active_governance_standard`

**运维记录（1个）** - 聚合 13 族
- `get_ops_record` (family: task_run|pipeline|datasource|...)

**优势**：
- ✅ 聚焦核心能力
- ✅ 职责清晰（读 vs 写）
- ✅ 标准化（统一错误处理、审计）
- ✅ 可组合（通用 agent 自己决定如何组合）

### 4. 约束层对比

#### 当前 Data Agent

**意图门控**
```python
# chat_bi.py:5560-5577
def _classify_intent(question: str) -> str:
    # 规则判定 + 关键词匹配
    if 命中取数标记:     return "analytical"
    if 命中运行记录标记: return "operational"
    if 命中结构标记:     return "structural"
    return "general"
```
**问题**：硬编码规则，难以维护

**技能路由**
```python
# chat_bi.py:5596-5599
def _auto_select_skill(question, intent):
    if "物化" in question: return "task"
    if "血缘" in question: return "lineage"
    # ...
```
**问题**：关键词匹配，容易误判

**工具收窄**
```python
# chat_bi_tool_schemas.py:1764-1801
SKILL_TOOL_ALLOWLIST = {
    "query": frozenset({"search_objects", "get_object", "run_sql", ...}),
    "task": frozenset({"propose_sync", "propose_transform", ...}),
    # ...
}
```
**问题**：白名单机制，每个技能只能看到部分工具

**接地验证**
```python
# chat_bi.py:6502-6516
if intent == "general" and not points_to_entity:
    general_waives = True  # 豁免接地
```
**问题**：复杂的豁免逻辑，容易答错

#### MCP 架构

**工具内约束**
```python
# 每个 MCP 工具内部
async def execute(self, arguments: dict) -> ToolResult:
    # 1. 参数校验
    self._validate_arguments(arguments)
    
    # 2. 权限检查（写操作）
    if self._is_write_operation():
        self._check_permissions(db, arguments)
    
    # 3. 业务规则检查（写操作）
    violations = self._check_business_rules(db, arguments)
    if violations:
        return ToolResult(success=False, data={"violations": violations})
    
    # 4. 调用核心引擎
    result = service.method(**arguments)
    
    # 5. 审计日志（写操作）
    if self._is_write_operation():
        self._audit_log(db, arguments, result)
    
    return ToolResult(success=True, data=result)
```

**优势**：
- ✅ 约束在工具内部，清晰可测试
- ✅ 通用 agent 可以尝试调用任何工具
- ✅ 工具层负责拒绝不合规的调用
- ✅ 返回结构化错误和修复建议

**意图判定**：不需要！通用 agent 自己决定调用哪个工具

**技能路由**：不需要！通用 agent 自己决定工作流程

**工具收窄**：不需要！所有工具都可见，agent 自己选择

**接地验证**：在工具内部！`execute_sql` 自动调用 F4 验证

### 5. 六环确认对比

#### 当前 Data Agent

**流程**：
1. Agent 提出提案 → 
2. 用户确认 intent 环 → 
3. Agent 继续 ontology 环 → 
4. 用户确认 ontology 环 →
5. ... (6 环)

**问题**：
- ❌ Agent 需要知道"6 环"的概念
- ❌ 流程僵化，无法跳过或合并
- ❌ 前端被动展示，无法控制流程

**代码**：25,000+ 行 `chat_bi_ledger.py`

#### MCP 架构

**流程**：
1. Agent 调用 `propose_*` 工具 →
2. 工具返回提案 + proposal_id →
3. 前端展示提案，用户逐环确认 →
4. 用户确认后，前端调用执行 API

**优势**：
- ✅ Agent 不需要知道确认流程
- ✅ 前端完全控制 UI 和流程
- ✅ 可以灵活调整流程（合并环、跳过环）
- ✅ 审计日志仍然完整（工具自动记录）

**代码**：复用现有的 `chat_bi_ledger.py`（决策账本），前端负责 UI

### 6. 前端集成对比

#### 当前 Data Agent

```typescript
// 前端调用专用 API
const response = await fetch('/api/chat-bi/ask', {
  method: 'POST',
  body: JSON.stringify({ question: '查询所有本体' })
});

const { blocks } = await response.json();
// blocks = [
//   { type: 'markdown', content: '...' },
//   { type: 'table', data: [...] }
// ]

// 前端根据 block type 渲染
blocks.forEach(block => {
  switch(block.type) {
    case 'markdown': return <Markdown content={block.content} />;
    case 'table': return <Table data={block.data} />;
    // ...
  }
});
```

**问题**：
- ❌ 绑定特定的渲染块格式
- ❌ Agent 需要知道前端如何渲染
- ❌ 前端被动接受，无法干预

#### MCP 架构

```typescript
// 前端可以选择：

// 方式 1：直接调用 MCP 工具
const result = await mcpClient.callTool('query_ontology', {});
// result = { success: true, data: { ontologies: [...] } }
// 前端自己决定如何渲染

// 方式 2：通过通用 agent
const response = await fetch('/api/agent/chat', {
  method: 'POST',
  body: JSON.stringify({ 
    message: '查询所有本体',
    mcp_tools: ['query_ontology', 'query_business_objects']
  })
});

const { text, tool_calls } = await response.json();
// text = agent 的自然语言回复
// tool_calls = agent 调用的工具和结果

// 前端可以：
// 1. 展示 agent 的文本
// 2. 识别工具调用，展示特定 UI（如表格）
// 3. 允许用户继续对话
```

**优势**：
- ✅ 前端有更多控制权
- ✅ 可以直接调用工具（跳过 agent）
- ✅ 可以通过 agent 调用（自然语言）
- ✅ 不绑定特定格式

### 7. 生态整合对比

#### 当前 Data Agent

```
孤立系统：
- ontoMeta 前端 → Data Agent → ontoMeta 后端
- 无法与其他系统协同
```

#### MCP 架构

```
开放生态：

┌──────────────┐
│ Claude Code  │───┐
└──────────────┘   │
                   │
┌──────────────┐   │    ┌─────────────────┐
│Claude Desktop│───┼───→│ ontoMeta MCP    │
└──────────────┘   │    │ 服务             │
                   │    └─────────────────┘
┌──────────────┐   │
│ VS Code      │───┤
└──────────────┘   │
                   │
┌──────────────┐   │
│ 自定义应用    │───┘
└──────────────┘

同时可以调用其他 MCP 服务：
- Confluence MCP
- GitHub MCP
- Slack MCP
- ...

跨系统工作流：
用户："从 Confluence 读取需求 → 
      在 ontoMeta 建立本体 → 
      生成代码 → 
      提交 PR 到 GitHub"
```

### 8. 性能对比（预估）

| 指标 | 当前 Data Agent | MCP 架构 | 差异 |
|------|----------------|----------|------|
| **平均响应时间** | 3-5 秒 | 2-4 秒 | ⬇️ 20% |
| **LLM 调用次数** | 2.6 次/问题 | 2.0 次/问题 | ⬇️ 23% |
| **代码复杂度** | 35,000+ 行 | 4,400 行 | ⬇️ 87% |
| **工具调用延迟** | 内部调用 | MCP 协议 | ⬆️ <50ms |
| **内存占用** | ~500 MB | ~200 MB | ⬇️ 60% |

**说明**：
- 响应时间降低：通用 agent 推理更快
- LLM 调用减少：通用 agent 更智能，少绕弯
- 工具调用延迟增加：MCP 协议开销，但可忽略（<50ms）
- 内存占用降低：删除大量胶水代码

---

## 迁移路径对比

### 方案 A：完全重写（不推荐）

```
时间：3-6 个月
风险：高
优势：全新架构，无历史包袱
劣势：业务中断，无法回滚
```

### 方案 B：渐进式迁移（推荐）

```
时间：6-8 周
风险：低
优势：可与现有系统共存，随时回滚
劣势：需要同时维护两套代码（短期）

阶段：
Week 1-2: MCP 基础设施 + 查询工具（只读，低风险）
Week 3-4: 任务和治理工具（写操作，中风险）
Week 5-6: 本体建模 + 前端适配（高风险）
Week 7:   A/B 测试 + 切换
Week 8:   完全切换 + 清理旧代码
```

---

## 总结

| 维度 | 当前 Data Agent | MCP 架构 | 结论 |
|------|----------------|----------|------|
| **推理能力** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | MCP 胜 |
| **代码复杂度** | ⭐ | ⭐⭐⭐⭐⭐ | MCP 胜 |
| **维护成本** | ⭐⭐ | ⭐⭐⭐⭐⭐ | MCP 胜 |
| **灵活性** | ⭐⭐ | ⭐⭐⭐⭐⭐ | MCP 胜 |
| **生态整合** | ⭐ | ⭐⭐⭐⭐⭐ | MCP 胜 |
| **稳定性** | ⭐⭐⭐⭐ | ⭐⭐⭐ | Data Agent 胜（已验证） |
| **迁移成本** | - | ⭐⭐⭐ | 中等 |

**推荐**：采纳 MCP 架构，6-8 周完成渐进式迁移。

---

**创建时间**：2026-09-03
**作者**：Claude Code
