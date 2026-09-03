# 通用 Agent 交互能力分析

## 核心问题

当前 Data Agent 提供了丰富的表单填写和交互能力，通用 agent 能否覆盖？

---

## 当前 Data Agent 的交互模式

### 1. 表单填写交互

**典型场景**：创建同步任务

```
用户: "帮我同步 ERP 的客户表"

Data Agent:
[调用 propose_sync 工具]
[返回渲染块: type="form"]

前端渲染表单：
┌─────────────────────────────────────────┐
│ 创建同步任务                             │
├─────────────────────────────────────────┤
│ 任务名称: [ERP 客户表同步            ] │
│ 源数据源: [ERP (MySQL)           ▼]    │
│ 源表:     [customer              ]      │
│ 目标数据源: [数据仓库 (Doris)     ▼]   │
│ 同步策略: ○ 全量  ● 增量  ○ CDC       │
│ 调度:     [0 */6 * * *           ]      │
│                                         │
│ [取消]  [提交]                          │
└─────────────────────────────────────────┘
```

**问题**：
- ❌ 通用 agent 返回自然语言或 JSON，不能直接渲染表单
- ❌ 表单字段（下拉框选项、校验规则）需要前端预定义

### 2. 六环确认交互

**典型场景**：逐环确认

```
用户: "物化客户对象到数据仓库"

轮次 1（意图确认）:
Agent: 你希望将「客户」对象物化到数据仓库，对吗？
前端: [显示意图卡片，用户点击"确认"]

轮次 2（本体确认）:
Agent: 将使用「ERP 域」下的「客户」对象，包含 15 个字段，对吗？
前端: [显示对象详情卡片，用户点击"确认"]

轮次 3（数据确认）:
Agent: 源数据表为 erp.customer，共 12,345 行，对吗？
前端: [显示数据预览，用户点击"确认"]

... (共 6 环)

最后: 所有环确认完成，现在可以执行了。
```

**问题**：
- ❌ 通用 agent 需要在每轮等待用户确认
- ❌ 前端需要识别"这是确认请求"并展示特定 UI

### 3. 澄清问题交互

**典型场景**：参数不明确

```
用户: "同步客户表"

Agent: 我需要确认几个信息：
1. 源数据库是哪个？（ERP / CRM / 电商）
2. 同步策略？（全量 / 增量 / CDC）
3. 调度频率？（实时 / 每小时 / 每天）

用户: "ERP，增量，每 6 小时"

Agent: 好的，创建增量同步任务...
```

**问题**：
- ❌ 通用 agent 可以问问题，但需要多轮对话
- ❌ 用户体验不如直接填表单

---

## 通用 Agent 的交互能力

### 方式 1：纯对话（最基础）

**能力**：
- ✅ 自然语言问答
- ✅ 多轮澄清
- ✅ 解释和引导

**局限**：
- ❌ 无法渲染复杂表单
- ❌ 无法展示结构化数据（需前端解析）
- ❌ 用户需要用文字描述所有参数

**用户体验**：⭐⭐ - 可用但繁琐

**示例**：
```
用户: "同步客户表"
Agent: "我需要一些信息：
        1. 源数据库名称？
        2. 目标数据源？
        3. 同步策略（全量/增量/CDC）？
        4. 调度 cron 表达式？"
用户: "源是 erp_db，目标是 warehouse，增量，0 */6 * * *"
Agent: [调用 propose_sync_task 工具]
      "任务已创建，ID: task-123"
```

### 方式 2：结构化提示（推荐）

**Claude Code / Claude Desktop 支持结构化输出**

**能力**：
- ✅ 返回 JSON Schema
- ✅ 前端可解析并渲染表单
- ✅ 验证和错误提示

**用户体验**：⭐⭐⭐⭐ - 接近当前 Data Agent

**实现**：

#### 2.1 MCP 工具返回 schema

```python
@register_tool
class ProposeSyncTaskTool:
    name = "propose_sync_task"
    
    # 工具 schema 本身就是表单定义
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "title": "任务名称",
                "description": "给任务起个名字"
            },
            "source_datasource_id": {
                "type": "string",
                "title": "源数据源",
                "description": "选择源数据库",
                # 动态选项
                "enum_provider": "list_datasources",
            },
            "source_tables": {
                "type": "array",
                "items": {"type": "string"},
                "title": "源表",
                "description": "要同步的表（支持多选）"
            },
            "strategy": {
                "type": "string",
                "title": "同步策略",
                "enum": ["full", "incremental", "cdc"],
                "default": "full",
                "description": "全量：每次同步所有数据\n增量：只同步变化的数据\nCDC：实时捕获变更"
            },
            "schedule": {
                "type": "string",
                "title": "调度",
                "pattern": "^[0-9\\s\\*\\/,\\-]+$",
                "placeholder": "0 */6 * * *",
                "description": "Cron 表达式，如：0 */6 * * * 表示每 6 小时"
            }
        },
        "required": ["name", "source_datasource_id", "source_tables", "strategy"]
    }
    
    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        # 业务逻辑
        # ...
```

#### 2.2 前端渲染

**方案 A：前端预定义表单（推荐）**

```typescript
// frontend/src/components/tools/ProposeSyncTaskForm.tsx

/**
 * propose_sync_task 工具的表单组件
 * 
 * 由前端根据工具 schema 定义，不依赖 agent 返回
 */
export function ProposeSyncTaskForm(props: {
  onSubmit: (values: any) => void;
}) {
  const [dataSources, setDataSources] = useState([]);
  
  // 加载数据源选项
  useEffect(() => {
    mcpClient.callTool('list_datasources', {}).then(result => {
      setDataSources(result.data.datasources);
    });
  }, []);
  
  return (
    <Form onFinish={props.onSubmit}>
      <Form.Item 
        name="name" 
        label="任务名称" 
        rules={[{ required: true }]}
      >
        <Input placeholder="如：同步 ERP 客户表" />
      </Form.Item>
      
      <Form.Item 
        name="source_datasource_id" 
        label="源数据源"
        rules={[{ required: true }]}
      >
        <Select>
          {dataSources.map(ds => (
            <Option key={ds.id} value={ds.id}>{ds.name}</Option>
          ))}
        </Select>
      </Form.Item>
      
      <Form.Item name="strategy" label="同步策略">
        <Radio.Group>
          <Radio value="full">全量</Radio>
          <Radio value="incremental">增量</Radio>
          <Radio value="cdc">CDC</Radio>
        </Radio.Group>
      </Form.Item>
      
      {/* ... 其他字段 */}
      
      <Button type="primary" htmlType="submit">创建任务</Button>
    </Form>
  );
}
```

**用户体验**：
```
用户: "帮我同步 ERP 的客户表"

前端识别意图 → 展示 ProposeSyncTaskForm 表单
（预填充：name="同步 ERP 客户表"）

用户填写表单 → 点击"创建任务"

前端 → mcpClient.callTool('propose_sync_task', formValues)

Agent 不需要渲染表单，只负责执行工具
```

**方案 B：动态表单（更灵活）**

```typescript
// frontend/src/components/DynamicToolForm.tsx

/**
 * 根据 MCP 工具 schema 动态生成表单
 * 
 * 类似 JSON Schema Form
 */
export function DynamicToolForm(props: {
  toolName: string;
  toolSchema: JSONSchema;
  onSubmit: (values: any) => void;
}) {
  return (
    <Form onFinish={props.onSubmit}>
      {Object.entries(props.toolSchema.properties).map(([key, prop]) => {
        // 根据类型渲染不同组件
        if (prop.type === 'string' && prop.enum) {
          return (
            <Form.Item key={key} name={key} label={prop.title}>
              <Select>
                {prop.enum.map(opt => <Option value={opt}>{opt}</Option>)}
              </Select>
            </Form.Item>
          );
        }
        
        if (prop.type === 'string') {
          return (
            <Form.Item key={key} name={key} label={prop.title}>
              <Input placeholder={prop.placeholder} />
            </Form.Item>
          );
        }
        
        // ... 其他类型
      })}
      
      <Button type="primary" htmlType="submit">提交</Button>
    </Form>
  );
}
```

### 方式 3：混合模式（最佳）

**结合对话和表单**

**流程**：
1. 用户用自然语言表达意图
2. 通用 agent 理解意图，调用 MCP 工具（或返回工具建议）
3. 前端识别工具，展示预定义表单（预填充 agent 提取的参数）
4. 用户补充/修改参数，提交
5. 前端调用 MCP 工具

**示例**：

```
用户: "帮我每 6 小时同步 ERP 的客户表到数据仓库"

通用 Agent:
[理解意图]
- 工具: propose_sync_task
- 参数推断:
  - name: "同步 ERP 客户表"
  - source_datasource_id: [需要用户选择]
  - source_tables: ["customer"]
  - strategy: "incremental" (根据"每 6 小时"推断增量)
  - schedule: "0 */6 * * *"

前端:
[识别到工具调用]
[展示 ProposeSyncTaskForm，预填充推断的参数]

┌─────────────────────────────────────────┐
│ 创建同步任务                             │
├─────────────────────────────────────────┤
│ 任务名称: [同步 ERP 客户表           ] │ ← 已填充
│ 源数据源: [请选择               ▼]     │ ← 需选择
│ 源表:     [customer              ]      │ ← 已填充
│ 目标数据源: [数据仓库 (Doris)     ▼]   │ ← 已填充（默认）
│ 同步策略: ○ 全量  ● 增量  ○ CDC       │ ← 已选中
│ 调度:     [0 */6 * * *           ]      │ ← 已填充
│                                         │
│ [取消]  [提交]                          │
└─────────────────────────────────────────┘

用户: [选择源数据源] → [点击提交]

前端: [调用 mcpClient.callTool('propose_sync_task', ...)]

Agent 只返回结果，无需管表单
```

**用户体验**：⭐⭐⭐⭐⭐ - 比当前 Data Agent 更好

**优势**：
- ✅ 自然语言输入（用户友好）
- ✅ 表单补充（精确控制）
- ✅ 智能预填充（减少输入）
- ✅ 保留灵活性（可修改任何参数）

---

## 六环确认的替代方案

### 当前问题

六环确认机制僵化：
- Agent 需要知道"6 环"的概念
- 每环需要单独对话轮次
- 无法跳过或合并

### 新方案：提案 + 审批工作流

**流程**：

```
┌─────────────────────────────────────────────────────────────┐
│ Step 1: Agent 生成完整提案                                    │
└─────────────────────────────────────────────────────────────┘

用户: "物化客户对象"
Agent: [调用 propose_materialize_task 工具]
       [返回完整提案，包含所有 6 环的信息]

↓

┌─────────────────────────────────────────────────────────────┐
│ Step 2: 前端展示提案详情（一次性展示所有环）                  │
└─────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ 物化任务提案                              │
├──────────────────────────────────────────┤
│ ✓ 意图                                    │
│   将「客户」对象物化到数据仓库             │
│                                          │
│ ✓ 本体                                    │
│   对象: 客户 (ERP 域)                     │
│   字段: 15 个 (id, name, phone, ...)     │
│                                          │
│ ✓ 数据                                    │
│   源表: erp.customer (12,345 行)         │
│   预估行数: 12,345                        │
│                                          │
│ ✓ 执行方案                                │
│   策略: 全量物化                          │
│   目标: dwd.dwd_customer                  │
│   调度: 每日 02:00                        │
│                                          │
│ ✓ 执行                                    │
│   Flink 任务，预估 5 分钟                 │
│                                          │
│ ✓ 结果                                    │
│   产出表: dwd.dwd_customer                │
│   可用性: 同步后立即可查                  │
│                                          │
│ [全部确认并执行]  [修改]  [取消]         │
└──────────────────────────────────────────┘

用户可以：
1. 点击"全部确认并执行" - 一键完成
2. 点击某一环的"修改"按钮 - 只改这一环
3. 点击"取消" - 放弃

↓

┌─────────────────────────────────────────────────────────────┐
│ Step 3: 记录决策（自动）                                      │
└─────────────────────────────────────────────────────────────┘

前端点击"确认"时，自动调用决策记录 API：
- 记录每一环的确认人、时间、内容
- 写入不可变的审计日志

↓

┌─────────────────────────────────────────────────────────────┐
│ Step 4: 执行任务                                             │
└─────────────────────────────────────────────────────────────┘

前端调用执行 API，传入 proposal_id
后端检查：所有环已确认 → 执行
```

**优势**：
- ✅ 一次对话生成完整提案
- ✅ 用户可以一键确认（快）
- ✅ 用户可以逐环审查（细）
- ✅ 支持修改和重新生成
- ✅ 审计日志完整保留

**Agent 职责**：
- 只负责生成提案（调用 propose_* 工具）
- 不需要知道确认流程

**前端职责**：
- 展示提案（6 环卡片）
- 收集用户确认
- 调用执行 API

**对比当前 Data Agent**：

| 维度 | 当前 Data Agent | 新方案 | 优势 |
|------|----------------|--------|------|
| **对话轮次** | 6+ 轮（每环一轮） | 1 轮 | ✅ 更快 |
| **用户体验** | 线性，必须按顺序 | 并行，可一键确认 | ✅ 更灵活 |
| **可修改性** | 某环修改需重来 | 任意环可独立修改 | ✅ 更友好 |
| **审计** | 完整 | 完整 | ➡️ 同样好 |

---

## 澄清问题的替代方案

### 当前问题

参数不明确时，Agent 需要多轮追问：

```
轮次 1:
用户: "同步客户表"
Agent: "源数据库是哪个？"

轮次 2:
用户: "ERP"
Agent: "同步策略？"

轮次 3:
用户: "增量"
Agent: "调度频率？"

轮次 4:
用户: "每 6 小时"
Agent: "好的，创建任务..."
```

### 新方案：智能预填充 + 表单补充

```
用户: "同步客户表"

通用 Agent:
[理解意图 → propose_sync_task]
[推断部分参数]
- source_tables: ["customer"]
- 其他参数：未知

前端:
[展示表单，预填充 "customer"]
[高亮未填充字段，提示用户补充]

┌─────────────────────────────────────────┐
│ 创建同步任务                             │
├─────────────────────────────────────────┤
│ ⚠️ 以下信息需要补充：                    │
│                                         │
│ 源数据源: [请选择               ▼] ←━━━ │
│ 源表:     [customer              ]      │
│ 同步策略: [请选择               ▼] ←━━━ │
│ 调度:     [请输入 cron           ] ←━━━ │
│                                         │
│ [取消]  [提交]                          │
└─────────────────────────────────────────┘

用户: [一次性补充所有缺失信息] → [提交]

一轮对话完成！
```

**优势**：
- ✅ 避免多轮追问
- ✅ 用户一次看到所有需要填的
- ✅ 可以跳过，先填其他字段

---

## 特殊交互场景

### 场景 1：选择题

**当前 Data Agent**：
```
Agent: "请选择数据源：
        1. ERP (MySQL)
        2. CRM (PostgreSQL)
        3. 电商 (MongoDB)"
用户: "1"
```

**通用 Agent**：
```
方案 A：自然语言
用户: "ERP"
Agent: [理解 → 映射到 datasource_id]

方案 B：表单下拉框（推荐）
前端直接展示 Select 组件
```

### 场景 2：多步向导

**当前 Data Agent**：
```
Agent: "第 1 步：选择源..."
用户: [选择]
Agent: "第 2 步：选择目标..."
用户: [选择]
Agent: "第 3 步：配置参数..."
```

**通用 Agent**：
```
前端: 展示多步表单（Ant Design Steps）
用户: 一次性完成所有步骤（或逐步）
提交时一次性调用工具
```

### 场景 3：实时预览

**当前 Data Agent**：
```
Agent: [返回 SQL]
前端: [展示 SQL + 执行按钮]
用户: [预览结果]
```

**通用 Agent**：
```
前端: 表单中有"预览"按钮
用户: [输入参数] → [点击预览]
前端: [调用 preview_sql 工具] → [展示结果]
用户: [满意] → [提交表单]
```

---

## 结论对比

| 交互能力 | 当前 Data Agent | 通用 Agent + 前端 | 结论 |
|---------|----------------|------------------|------|
| **表单填写** | ⭐⭐⭐⭐ 专用表单 | ⭐⭐⭐⭐⭐ 智能预填充 + 表单 | ✅ 更好 |
| **六环确认** | ⭐⭐⭐ 多轮对话 | ⭐⭐⭐⭐⭐ 一次展示 + 灵活确认 | ✅ 更好 |
| **澄清问题** | ⭐⭐⭐ 多轮追问 | ⭐⭐⭐⭐ 智能推断 + 表单补充 | ✅ 更好 |
| **自然语言** | ⭐⭐⭐ 受限于提示词 | ⭐⭐⭐⭐⭐ 通用 agent 更强 | ✅ 更好 |
| **复杂向导** | ⭐⭐⭐⭐ 专用实现 | ⭐⭐⭐⭐ 前端实现 | ➡️ 相当 |

---

## 实施建议

### Phase 1：基础对话（立即可用）
- ✅ 通用 agent 可以立即使用
- ✅ 纯对话模式，无需前端改动
- ⚠️ 用户体验一般

### Phase 2：表单集成（推荐）
- ⬜ 前端为每个 propose_* 工具预定义表单
- ⬜ Agent 返回工具调用 + 参数
- ⬜ 前端识别并展示表单（预填充）
- ✅ 用户体验优秀

### Phase 3：动态表单（可选）
- ⬜ 前端根据 MCP 工具 schema 动态生成表单
- ⬜ 类似 JSON Schema Form
- ✅ 最灵活，无需为每个工具写前端代码

---

## 最终结论

**通用 agent 可以完全覆盖当前 Data Agent 的交互能力，甚至更好**：

1. **表单填写**：通过"智能推断 + 前端表单"实现，用户体验更好
2. **六环确认**：一次性展示所有环，更快更灵活
3. **澄清问题**：智能预填充 + 表单补充，避免多轮追问
4. **自然语言**：通用 agent 的推理能力更强

**关键**：
- Agent 负责理解意图和调用工具
- 前端负责展示表单和收集输入
- 两者职责清晰，配合更好

**不是"能否覆盖"的问题，而是"如何做得更好"的问题。**
