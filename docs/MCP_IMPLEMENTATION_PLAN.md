# MCP 架构改造实施计划

## 总体策略

**渐进式迁移**：不是一次性替换 Data Agent，而是逐步迁移能力到 MCP，同时保持系统可用。

**三阶段并行**：
1. **MCP 服务搭建** - 独立进行，不影响现有系统
2. **工具迁移** - 逐个工具迁移，可与现有 Data Agent 共存
3. **前端适配** - 最后切换，一次性完成

**回滚保证**：每个阶段都可以独立回滚，不影响生产环境。

---

## Phase 1: 基础设施搭建（Infrastructure Setup）

**目标**：搭建 MCP 服务端框架，能够注册和调用第一个测试工具

**时间估计**：2-3 天

### 1.1 添加 MCP 依赖

**文件**：`backend/requirements.txt`

```diff
+ # --- MCP Server ---
+ mcp>=0.9.0              # Anthropic Model Context Protocol SDK
```

### 1.2 创建 MCP 服务入口

**新建文件**：`backend/app/mcp/server.py`

```python
"""
MCP 服务器入口

提供 ontoMeta 的核心能力为 MCP 工具。
"""
from typing import Any
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from .tools import TOOL_REGISTRY

# 创建 MCP 服务器实例
server = Server("ontometa")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """列出所有可用工具"""
    return [
        types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.input_schema,
        )
        for tool in TOOL_REGISTRY.values()
    ]

@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """执行工具调用"""
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        raise ValueError(f"Unknown tool: {name}")
    
    # 调用工具实现
    result = await tool.execute(arguments)
    
    # 返回结果
    return [
        types.TextContent(
            type="text",
            text=result.to_json(),
        )
    ]

async def main():
    """启动 MCP 服务器"""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
```

### 1.3 创建工具注册机制

**新建文件**：`backend/app/mcp/tools/__init__.py`

```python
"""
MCP 工具注册表

所有 MCP 工具在这里注册。
"""
from typing import Protocol, Any
from dataclasses import dataclass
import json

class ToolResult:
    """工具执行结果的统一信封"""
    def __init__(
        self,
        success: bool,
        data: Any = None,
        error: str | None = None,
        metadata: dict | None = None,
    ):
        self.success = success
        self.data = data
        self.error = error
        self.metadata = metadata or {}
    
    def to_json(self) -> str:
        return json.dumps({
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }, ensure_ascii=False, indent=2)

class McpTool(Protocol):
    """MCP 工具接口"""
    name: str
    description: str
    input_schema: dict
    
    async def execute(self, arguments: dict) -> ToolResult:
        """执行工具逻辑"""
        ...

# 工具注册表
TOOL_REGISTRY: dict[str, McpTool] = {}

def register_tool(tool: McpTool):
    """注册一个 MCP 工具"""
    TOOL_REGISTRY[tool.name] = tool
    return tool
```

### 1.4 实现第一个测试工具

**新建文件**：`backend/app/mcp/tools/query_ontology.py`

```python
"""
query_ontology 工具实现
"""
from sqlalchemy.orm import Session
from ...database import get_db
from ...services.ontology_query import OntologyQueryService
from . import register_tool, McpTool, ToolResult

@register_tool
class QueryOntologyTool:
    name = "query_ontology"
    description = "查询本体结构和业务对象列表"
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {
                "type": "string",
                "description": "本体 ID（留空查询所有）",
            },
            "include_unpublished": {
                "type": "boolean",
                "description": "是否包含未发布的本体",
                "default": False,
            },
        },
    }
    
    async def execute(self, arguments: dict) -> ToolResult:
        """执行查询"""
        try:
            db: Session = next(get_db())
            
            ontology_id = arguments.get("ontology_id")
            include_unpublished = arguments.get("include_unpublished", False)
            
            # 调用现有服务
            query_service = OntologyQueryService(db)
            
            if ontology_id:
                # 查询单个本体
                ontology = query_service.get_ontology(
                    ontology_id,
                    include_unpublished=include_unpublished
                )
                if not ontology:
                    return ToolResult(
                        success=False,
                        error=f"Ontology not found: {ontology_id}"
                    )
                data = [ontology]
            else:
                # 查询所有本体
                data = query_service.list_ontologies(
                    include_unpublished=include_unpublished
                )
            
            return ToolResult(
                success=True,
                data={
                    "ontologies": [
                        {
                            "id": o.id,
                            "domain_name": o.domain_name,
                            "version": o.version,
                            "objects_count": len(o.objects or []),
                            "published_at": o.published_at.isoformat() if o.published_at else None,
                        }
                        for o in data
                    ]
                },
                metadata={
                    "count": len(data),
                    "include_unpublished": include_unpublished,
                }
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e)
            )
```

### 1.5 添加 MCP 服务启动脚本

**新建文件**：`backend/scripts/start_mcp_server.sh`

```bash
#!/bin/bash
# 启动 ontoMeta MCP 服务器

set -e

cd "$(dirname "$0")/.."

# 激活虚拟环境（如果存在）
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# 启动 MCP 服务器（stdio 模式）
python -m app.mcp.server
```

### 1.6 测试第一个工具

**验收标准**：
1. ✅ MCP 服务器可以启动
2. ✅ `query_ontology` 工具可以注册
3. ✅ 通过 Claude Desktop 或 Claude Code 可以调用工具
4. ✅ 工具返回正确的本体列表

**测试方法**：
```bash
# 1. 启动 MCP 服务器
cd backend
bash scripts/start_mcp_server.sh

# 2. 在 Claude Desktop 配置中添加：
# {
#   "mcpServers": {
#     "ontometa": {
#       "command": "/path/to/backend/scripts/start_mcp_server.sh"
#     }
#   }
# }

# 3. 在 Claude Desktop 中测试：
# "使用 query_ontology 工具查询所有本体"
```

---

## Phase 2: 核心工具迁移（Core Tools Migration）

**目标**：逐个迁移 MCP_TOOL_DESIGN.md 中定义的 20 个工具

**时间估计**：2-3 周

### 迁移顺序（按优先级）

#### 2.1 查询工具（只读，低风险） - 第 1 周

1. ✅ `query_ontology` - 已在 Phase 1 完成
2. `query_business_objects`
3. `query_relationships`
4. `get_lineage`
5. `get_landing`
6. `execute_sql`

**目标**：用户可以通过通用 agent 查询本体信息

#### 2.2 运维记录工具（只读） - 第 1 周

7. `get_task_status`
8. `list_task_runs`
9. `get_ops_record`

**目标**：用户可以查询任务执行状态和历史

#### 2.3 治理工具（只读） - 第 2 周

10. `validate_against_policy`
11. `lint_task_spec`
12. `get_active_governance_standard`

**目标**：用户可以在提交前校验合规性

#### 2.4 任务提案工具（写操作，高风险） - 第 2-3 周

13. `propose_sync_task`
14. `propose_transform_task`
15. `propose_materialize_task`
16. `propose_metric_task`

**目标**：用户可以通过通用 agent 创建任务提案
**关键约束**：必须实现完整的审计和校验

#### 2.5 本体建模工具（写操作，高风险） - 第 3 周

17. `infer_ontology_from_datahub`
18. `classify_business_objects`
19. `infer_relationships`
20. `validate_ontology`

**目标**：用户可以通过通用 agent 执行本体建模

### 每个工具的实施步骤

对于每个工具：

1. **创建工具实现文件** - `backend/app/mcp/tools/{tool_name}.py`
2. **实现 execute() 方法** - 调用现有引擎，包装返回结果
3. **添加约束层** - 权限检查、参数校验、业务规则
4. **实现审计** - 写操作自动记入决策账本
5. **编写单元测试** - `backend/tests/mcp/tools/test_{tool_name}.py`
6. **编写集成测试** - 真实场景端到端测试
7. **更新文档** - 工具使用说明和示例

### 工具模板

**文件结构**：
```
backend/app/mcp/tools/
├── __init__.py              # 工具注册表
├── base.py                  # 基类和通用工具
├── query/                   # 查询工具
│   ├── query_ontology.py
│   ├── query_business_objects.py
│   └── ...
├── task/                    # 任务工具
│   ├── propose_sync.py
│   ├── propose_transform.py
│   └── ...
├── ontology/                # 本体建模工具
│   ├── infer_ontology.py
│   └── ...
└── governance/              # 治理工具
    └── ...
```

**工具实现模板**：

```python
"""
{tool_name} 工具实现

复用引擎：{engine_path}
"""
from sqlalchemy.orm import Session
from ...database import get_db
from ...services.{service} import {Service}
from ...services.chat_bi_ledger import ChatBiLedgerService
from . import register_tool, McpTool, ToolResult

@register_tool
class {ToolClass}:
    name = "{tool_name}"
    description = "{description}"
    input_schema = {
        # JSON Schema
    }
    
    async def execute(self, arguments: dict) -> ToolResult:
        """执行工具逻辑"""
        db: Session = next(get_db())
        
        try:
            # 1. 参数校验
            self._validate_arguments(arguments)
            
            # 2. 权限检查（写操作）
            if self._is_write_operation():
                self._check_permissions(db, arguments)
            
            # 3. 业务规则检查（写操作）
            if self._is_write_operation():
                violations = self._check_business_rules(db, arguments)
                if violations:
                    return ToolResult(
                        success=False,
                        error="Business rule violations",
                        data={"violations": violations}
                    )
            
            # 4. 调用核心引擎
            service = {Service}(db)
            result = service.{method}(**arguments)
            
            # 5. 审计日志（写操作）
            if self._is_write_operation():
                self._audit_log(db, arguments, result)
            
            # 6. 返回结果
            return ToolResult(
                success=True,
                data=self._format_result(result),
                metadata={
                    "as_of": datetime.utcnow().isoformat(),
                    "source": "{authoritative_source}"
                }
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e)
            )
    
    def _validate_arguments(self, arguments: dict):
        """参数校验"""
        # 必填参数检查
        # 类型检查
        # 范围检查
        pass
    
    def _check_permissions(self, db: Session, arguments: dict):
        """权限检查"""
        # RBAC 检查
        # 资源所有权检查
        pass
    
    def _check_business_rules(self, db: Session, arguments: dict) -> list:
        """业务规则检查"""
        # 调用 lint 引擎
        # 返回 violations 列表
        pass
    
    def _audit_log(self, db: Session, arguments: dict, result: Any):
        """审计日志"""
        ledger_service = ChatBiLedgerService(db)
        ledger_service.record_decision(
            # ...
        )
    
    def _format_result(self, result: Any) -> dict:
        """格式化返回结果"""
        pass
    
    def _is_write_operation(self) -> bool:
        """是否写操作"""
        return "propose" in self.name or "infer" in self.name
```

### 测试策略

#### 单元测试

**文件**：`backend/tests/mcp/tools/test_{tool_name}.py`

```python
import pytest
from app.mcp.tools.{tool_name} import {ToolClass}

@pytest.fixture
def tool():
    return {ToolClass}()

@pytest.mark.asyncio
async def test_execute_success(tool, db_session):
    """测试成功路径"""
    arguments = {
        # 有效参数
    }
    result = await tool.execute(arguments)
    
    assert result.success
    assert result.data is not None
    assert result.error is None

@pytest.mark.asyncio
async def test_execute_validation_error(tool):
    """测试参数校验失败"""
    arguments = {
        # 无效参数
    }
    result = await tool.execute(arguments)
    
    assert not result.success
    assert result.error is not None

@pytest.mark.asyncio
async def test_execute_business_rule_violation(tool, db_session):
    """测试业务规则违反"""
    arguments = {
        # 违反业务规则的参数
    }
    result = await tool.execute(arguments)
    
    assert not result.success
    assert "violations" in result.data
```

#### 集成测试

**文件**：`backend/tests/integration/mcp/test_{tool_name}_integration.py`

```python
import pytest
from app.mcp.server import server

@pytest.mark.integration
@pytest.mark.asyncio
async def test_call_tool_end_to_end(test_client):
    """端到端测试工具调用"""
    response = await test_client.post(
        "/mcp/call_tool",
        json={
            "name": "{tool_name}",
            "arguments": {
                # 参数
            }
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"]
```

---

## Phase 3: 前端适配（Frontend Adaptation）

**目标**：前端可以调用 MCP 工具，并展示结果

**时间估计**：1-2 周

### 3.1 添加 MCP 客户端

**文件**：`frontend/src/services/mcpClient.ts`

```typescript
/**
 * MCP 客户端
 * 
 * 封装 MCP 工具调用逻辑
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

class McpClient {
  private client: Client;
  private transport: StdioClientTransport;
  
  async connect() {
    // 连接到 MCP 服务器
    this.transport = new StdioClientTransport({
      command: "/path/to/backend/scripts/start_mcp_server.sh",
    });
    
    this.client = new Client(
      {
        name: "ontometa-frontend",
        version: "1.0.0",
      },
      {
        capabilities: {},
      }
    );
    
    await this.client.connect(this.transport);
  }
  
  async callTool(name: string, arguments: Record<string, any>) {
    const result = await this.client.callTool({
      name,
      arguments,
    });
    
    return JSON.parse(result.content[0].text);
  }
  
  async listTools() {
    return this.client.listTools();
  }
}

export const mcpClient = new McpClient();
```

### 3.2 重构前端路由

当前前端直接调用 `/api/chat-bi/ask`，需要改为：
1. **轻量查询** → 调用 MCP 工具（通过通用 agent）
2. **任务提案** → 先调用 MCP 工具生成提案，再展示确认 UI
3. **六环确认** → 完全由前端控制，不再依赖 agent

### 3.3 渲染适配

MCP 工具返回结构化 JSON，前端需要：
1. 解析 JSON
2. 根据工具类型选择渲染组件
3. 展示结果

**示例**：

```typescript
// 旧方式：Data Agent 返回渲染块
const response = await fetch('/api/chat-bi/ask', {
  method: 'POST',
  body: JSON.stringify({ question: '查询所有本体' })
});
const { blocks } = await response.json();
// blocks = [{ type: 'markdown', content: '...' }, ...]

// 新方式：通用 agent + MCP 工具
const result = await mcpClient.callTool('query_ontology', {});
// result = { success: true, data: { ontologies: [...] } }
// 前端自己决定如何渲染
```

---

## Phase 4: 切换与验证（Cutover & Validation）

**目标**：完全切换到 MCP + 通用 agent，移除旧的 Data Agent

**时间估计**：1 周

### 4.1 A/B 测试

**策略**：
1. 保留旧的 `/api/chat-bi/*` 端点
2. 新增 `/api/mcp/*` 端点
3. 前端通过 feature flag 切换

**Feature Flag**：
```typescript
// frontend/src/config.ts
export const USE_MCP_AGENT = process.env.REACT_APP_USE_MCP === 'true';
```

### 4.2 性能对比

**指标**：
- 平均响应时间
- LLM 调用次数
- 工具调用次数
- 用户满意度

**目标**：
- 响应时间不增加
- LLM 调用次数不增加（或减少，因为通用 agent 更智能）
- 用户满意度不下降

### 4.3 功能覆盖验证

**清单**：
- [ ] 本体查询
- [ ] 业务对象查询
- [ ] 关系查询
- [ ] SQL 执行
- [ ] 血缘查询
- [ ] 落点查询
- [ ] 任务提案（同步、清洗、物化、指标）
- [ ] 任务状态查询
- [ ] 运维记录查询
- [ ] 治理规约校验
- [ ] 本体建模（推断、分类、关系）

### 4.4 回归测试

**运行现有测试套件**：
```bash
cd backend
pytest tests/ -v

cd frontend
npm test
```

**目标**：所有测试通过

### 4.5 完全切换

**步骤**：
1. 将 `USE_MCP_AGENT` 默认设置为 `true`
2. 观察 1-2 天，监控错误率
3. 如果稳定，移除旧代码：
   - 删除 `backend/app/services/chat_bi.py`（7000+ 行）
   - 删除 `backend/app/services/chat_bi_*.py` 相关文件
   - 删除 `backend/app/api/chat_bi.py`
4. 更新文档

---

## Phase 5: 开放与平台化（Opening & Platformization）

**目标**：将 MCP 服务开放给其他应用使用

**时间估计**：1-2 周

### 5.1 发布 MCP 服务

**方式 1：本地安装**
```bash
# 用户在本地安装 ontoMeta
git clone https://github.com/your-org/ontoMeta.git
cd ontoMeta/backend
pip install -e .

# 配置 Claude Desktop
# ~/.config/claude/claude_desktop_config.json
{
  "mcpServers": {
    "ontometa": {
      "command": "ontometa-mcp-server",
      "args": ["--database-url", "postgresql://..."]
    }
  }
}
```

**方式 2：远程服务**
```bash
# 部署 MCP 服务到云端
# 用户通过 HTTP 调用（需要鉴权）
{
  "mcpServers": {
    "ontometa": {
      "url": "https://ontometa.example.com/mcp",
      "apiKey": "your-api-key"
    }
  }
}
```

### 5.2 Skill 定义

为常见场景定义 skill，帮助用户快速上手：

**文件**：`skills/ontology-modeling.md`
```markdown
# Ontology Modeling Skill

本技能用于从数据源推断本体并建模。

## 工具
- infer_ontology_from_datahub
- classify_business_objects
- infer_relationships
- validate_ontology

## 工作流
1. 连接 DataHub 并列出域
2. 选择域并推断本体
3. 审核业务对象分类
4. 验证本体合规性
5. 发布本体

## 示例
用户："帮我从 DataHub 的 ERP 域建立本体"
```

### 5.3 文档和示例

**文档**：
- MCP 服务端安装指南
- 工具 API 参考
- Skill 使用指南
- 最佳实践

**示例**：
- 如何在 Claude Desktop 中使用
- 如何在 VS Code 中使用
- 如何在自己的应用中集成

---

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| MCP SDK 不稳定 | 阻塞开发 | 提前验证 SDK，准备降级方案 |
| 性能下降 | 用户体验差 | A/B 测试，性能监控 |
| 功能缺失 | 用户抱怨 | 完整功能覆盖验证 |
| 通用 agent 理解错误 | 答非所问 | 改进工具描述，添加示例 |
| 审计遗漏 | 合规风险 | 自动化审计测试，代码审查 |
| 权限绕过 | 安全风险 | 在工具层实现权限检查 |

---

## 成功标准

1. **功能完整性**：所有现有 Data Agent 功能都可通过 MCP 工具实现
2. **性能不下降**：响应时间和 LLM 调用次数不增加
3. **用户满意度**：用户反馈积极，认为更灵活
4. **代码简化**：删除 7000+ 行 Data Agent 代码
5. **平台化**：其他应用可以使用 ontoMeta 的能力

---

## 时间表

| 阶段 | 内容 | 时间 | 里程碑 |
|------|------|------|--------|
| Phase 1 | 基础设施搭建 | 2-3 天 | 第一个工具可用 |
| Phase 2.1 | 查询工具迁移 | 1 周 | 只读查询完全可用 |
| Phase 2.2 | 运维记录工具 | 3 天 | 运维问题可查询 |
| Phase 2.3 | 治理工具 | 3 天 | 规约校验可用 |
| Phase 2.4 | 任务提案工具 | 1 周 | 任务提案可用 |
| Phase 2.5 | 本体建模工具 | 1 周 | 本体建模可用 |
| Phase 3 | 前端适配 | 1-2 周 | 前端可调用 MCP |
| Phase 4 | 切换与验证 | 1 周 | 完全切换 |
| Phase 5 | 开放与平台化 | 1-2 周 | 对外开放 |
| **总计** | | **6-8 周** | **完成改造** |

---

## 下一步

1. ✅ 完成设计文档
2. ⬜ 评审设计，获得团队共识
3. ⬜ 开始 Phase 1 实施
4. ⬜ 每周回顾进度，调整计划
