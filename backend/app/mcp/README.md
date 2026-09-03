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
      "command": "/Users/me/Documents/ontoMeta/backend/.venv/bin/python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/Users/me/Documents/ontoMeta/backend",
      "env": {
        "PYTHONPATH": "/Users/me/Documents/ontoMeta/backend",
        "ONTOMETA_MCP_TOKEN": "<你的 Principal Token 或 ONTOMETA_ADMIN_TOKEN>"
      }
    }
  }
}
```

⚠️ **注意**：
- `cwd` 与路径改成你的实际项目路径。
- `command` 要指向 **venv 里的解释器**——系统 `python` 装不到本项目的依赖，服务器会
  在客户端里静默启动失败（Claude Desktop 只显示「工具不可用」，不显示 ImportError）。
- `ONTOMETA_MCP_TOKEN` 决定这条会话的**身份与权限**（见下节「身份与权限」）。不填 = 匿名
  只读（默认 `reader`），只能用 `query_*` 等只读工具；要提案/代跑 SQL 得填够权限的 Token。

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

全部为**只读**（不写库、不执行）。写侧（草稿落库、确认、执行）仍走 REST + 人工确认，
MCP 这边不开口子。每个工具的最低角色见「身份与权限」。

### 本体查询

| 工具 | 作用 |
|------|------|
| `query_ontology` | 本体列表 / 单个本体（域名、版本、发布状态） |
| `query_objects` | 本体内的业务对象，可按角色（`business_object`/`data_table`/`bridge`/`technical`）、关键词、待复核过滤 |
| `query_object_detail` | 单个对象的属性、进出关系、绑定口径、物理落点 |
| `query_relations` | 对象关系（两端对象名、结构类型、基数）；JOIN 的连接键在 `source_evidence` |

### 数据源与 SQL

| 工具 | 作用 |
|------|------|
| `list_datasources` | 已配置的业务源库与数仓（**不返回任何凭据**） |
| `validate_sql` | 只读校验，不连库 |
| `execute_sql` | 在默认 Doris 数仓执行只读 SQL；没有显式配置的默认仓就拒绝执行 |

### 任务（治理制品）

| 工具 | 作用 |
|------|------|
| `list_tasks` | 同步/加工/聚合/物化任务列表，附落点、调度与回执摘要 |
| `get_task_status` | 单个任务的状态、Spec、校验报告、回执，并尽力回读 Airflow 实时态 |

### 任务提案

| 工具 | 作用 |
|------|------|
| `propose_sync` | 源库表 → 数仓 ODS |
| `propose_transform` | ODS → 加工结果表 |
| `propose_materialize` | 本体对象 → 物理表（只出 DDL） |
| `propose_metric` | 已发布业务口径 → ADS 结果表 |

### 审计与运维

| 工具 | 作用 |
|------|------|
| `list_audit_logs` | 回读工具调用审计（谁、什么身份、调了什么、成没成、是否被拒）；仅 publisher |
| `server_info` | 自省：版本、工具清单与各自最低角色、当前会话身份、限流配置、审计可达性 |
| `get_mcp_stats` | 使用统计：总量 / 成功 / 被拒 / 被限流、按工具与角色分组；仅 publisher |

提案工具的三条约束：

1. **Spec 由真 Drafter 派生**（`app/agents/drafters`），调用方只给 `intent` + `context`。
   ODS 落点、引擎、装载方式、任务命名都是从本体与契约推导的，给了也会被覆盖。
2. **缺参当场说清**，并附**真实候选**（数据源 id/名称/连通状态），不让模型自己编 id。
3. **不写库、不执行**。返回的 `draft_payload` 由人在前端确认后 `POST /api/agents/draft`
   落成草稿，再经 validate / confirm / execute。

## 身份与权限

一条 stdio 会话（一个子进程）就是**一个身份**，在服务器启动时由 `ONTOMETA_MCP_TOKEN`
解析一次，与 REST 的 `X-Admin-Token` / Principal Token 同价：

| `ONTOMETA_MCP_TOKEN` | 角色 |
|----------------------|------|
| = `ONTOMETA_ADMIN_TOKEN` | `publisher`（superuser） |
| = 某启用中 Principal 的 Token | 该主体角色 |
| 不填 / 不匹配 | 匿名，`mcp_default_role`（默认 `reader`） |

每个工具声明所需的最低角色（4 层：reader < editor < reviewer < publisher），服务器在
调用前**统一强制、fail-closed**：

| 工具 | 最低角色 |
|------|----------|
| `query_*` / `list_datasources` / `list_tasks` / `get_task_status` / `validate_sql` | `reader` |
| `propose_*` | `editor` |
| `execute_sql` | `agent_run_sql_min_role`（默认 `publisher`，与 Data Agent 代跑 SQL 同价） |
| `list_audit_logs` / `get_mcp_stats` | `publisher` |
| `server_info` | `reader` |

**审计**：每次调用（成功 / 失败 / **被拒** / **被限流**）都追加一条 `mcp_audit_logs`（谁、
什么身份、哪个工具、成没成、耗时、脱敏入参），只追加不改写，publisher 可用
`list_audit_logs` 回读、`get_mcp_stats` 聚合。

**限流**：进程内滑动窗口，防 agent 失控循环打爆数仓/DB。每工具每分钟上限
`MCP_RATE_LIMIT_PER_MINUTE`（默认 120，0=关闭），`execute_sql` 单独更严
`MCP_EXECUTE_SQL_RATE_LIMIT_PER_MINUTE`（默认 30）。命中回 `rate_limited`（与授权的
`denied` 区分），并做审计去重。

配置建议：给 MCP 单独建一个**最小权限的 Principal**（`POST /api/principals`），而不是把
`ONTOMETA_ADMIN_TOKEN` 塞进客户端配置——admin token 是 superuser，泄漏面更大。

## 目录结构

```
backend/app/mcp/
├── __init__.py
├── __main__.py          # 启动入口
├── server.py            # MCP 服务器实现（stdio）+ 限流/授权闸门 + 审计包裹
├── auth.py              # 会话身份解析（env Token → 角色）
├── audit.py             # 审计写入点（append-only，脱敏，吞异常）
├── rate_limit.py        # 进程内滑动窗口限流
├── STATUS.md            # 实施状态与阶段计划
└── tools/
    ├── __init__.py      # 工具注册机制 + ToolResult / AuthContext / required_role
    ├── _common.py       # 会话与序列化辅助
    ├── query.py         # 本体查询
    ├── objects.py       # 对象 / 关系查询
    ├── datasources.py   # 数据源目录
    ├── sql.py           # 只读 SQL
    ├── tasks.py         # 任务（治理制品）回读
    ├── proposals.py     # 任务提案
    ├── audit.py         # 审计回读（list_audit_logs，publisher）
    └── monitoring.py    # 自省与统计（server_info / get_mcp_stats）
```

审计表模型在 `app/models/mcp_audit.py`，迁移在
`alembic/versions/e2d48fc8520a_mcp_audit_logs.py`。用例在 `backend/tests/test_mcp_auth.py`。

用例在 `backend/tests/test_mcp_tools.py`。

## 开发指南

### 添加新工具

1. 在 `tools/` 目录创建新文件（如 `lineage.py`）
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

3. 在 `tools/__init__.py` 末尾的导入清单里加上新模块——注册靠的是 import 副作用，
   漏了不会报错，工具只是**悄悄消失**：

```python
from . import query, objects, datasources, sql, tasks, proposals, mine  # noqa
```

4. 在 `tests/test_mcp_tools.py` 的 `EXPECTED_TOOLS` 里补上工具名（就是为了让上一步
   的遗漏变成一条红色用例），并补一条真实调用的用例。

5. 重启 MCP 服务器

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

见 [STATUS.md](./STATUS.md)。简述：

- [x] Phase 1 基础设施
- [x] Phase 2 核心只读工具（16 个）
- [x] Phase 3 认证（env Token → 4 层角色）+ 授权（工具级 required_role，fail-closed）+ 审计
- [x] Phase 4 限流（进程内滑动窗口）+ 运维自省（`server_info`）/ 监控（`get_mcp_stats`）
- [ ] Phase 5 资源级权限、本体建模类工具、血缘/落点工具、远程传输（streamable HTTP）

## 参考

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [MCP SDK (Python)](https://github.com/modelcontextprotocol/python-sdk)
- [完整设计文档](../../../docs/MCP_README.md)
