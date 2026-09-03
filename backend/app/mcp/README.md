# ontoMeta MCP 服务

本体工程和数据治理能力的 MCP (Model Context Protocol) 封装。

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 测试 MCP 服务器

```bash
cd backend
python -m app.mcp.server
```

服务器会启动并等待 stdin 输入（MCP 协议使用 stdio 通信）。

### 3. 与 Claude Desktop 集成

#### 方法 A：手动配置

编辑 Claude Desktop 配置文件：

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

添加：

```json
{
  "mcpServers": {
    "ontometa": {
      "command": "python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/Users/me/Documents/ontoMeta/backend",
      "env": {
        "PYTHONPATH": "/Users/me/Documents/ontoMeta/backend"
      }
    }
  }
}
```

⚠️ **注意**：修改 `cwd` 为你的实际项目路径。

#### 方法 B：使用项目配置文件

项目根目录有一个 `mcp-config.json` 模板，复制其中的配置到 Claude Desktop 配置文件。

### 4. 重启 Claude Desktop

配置后需要重启 Claude Desktop，工具才会加载。

### 5. 验证

在 Claude Desktop 中，你应该能看到 ontoMeta 工具可用。试试：

```
请列出所有已发布的本体
```

Claude 会调用 `query_ontology` 工具查询数据库。

## 可用工具

### 查询工具

#### `query_ontology`

查询本体列表。

**参数**：
- `ontology_id`（可选）：本体 ID，留空查询所有
- `include_unpublished`（可选）：是否包含未发布的本体，默认 false

**示例**：

```json
{
  "ontology_id": "ont-xxx",
  "include_unpublished": false
}
```

## 目录结构

```
backend/app/mcp/
├── __init__.py
├── __main__.py          # 启动入口
├── server.py            # MCP 服务器实现
└── tools/
    ├── __init__.py      # 工具注册机制
    └── query.py         # 查询工具
```

## 开发指南

### 添加新工具

1. 在 `tools/` 目录创建新文件（如 `propose.py`）
2. 使用 `@register_tool` 装饰器注册工具：

```python
from . import register_tool, ToolResult, AuthContext

@register_tool
class MyTool:
    name = "my_tool"
    description = "工具描述"
    input_schema = {
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "参数说明"
            }
        },
        "required": ["param1"]
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        # 实现工具逻辑
        try:
            result = do_something(arguments["param1"])
            return ToolResult(
                success=True,
                data={"result": result}
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e)
            )
```

3. 在 `server.py` 中导入：

```python
from .tools import query, propose  # 添加新模块
```

4. 重启 MCP 服务器

### 测试工具

可以使用 MCP Inspector 测试工具：

```bash
npx @modelcontextprotocol/inspector python -m app.mcp.server
```

## 调试

### 查看日志

MCP 服务器的日志会输出到 stderr：

```bash
python -m app.mcp.server 2>mcp-server.log
```

### 常见问题

**Q: Claude Desktop 看不到工具**

A: 检查：
1. 配置文件路径是否正确
2. Python 路径是否正确（`which python`）
3. 是否重启了 Claude Desktop
4. 查看 Claude Desktop 的日志（Help → View Logs）

**Q: 工具调用失败**

A: 检查：
1. 数据库连接是否正常
2. 查看 `mcp-server.log` 日志
3. 确认参数格式是否正确

## 后续计划

- [ ] 添加更多查询工具（对象、属性、关系）
- [ ] 添加任务提案工具（sync/transform/materialize/metric）
- [ ] 添加本体建模工具
- [ ] 实现认证和权限控制
- [ ] 添加审计日志

## 参考

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [MCP SDK (Python)](https://github.com/modelcontextprotocol/python-sdk)
- [完整设计文档](../docs/MCP_README.md)
