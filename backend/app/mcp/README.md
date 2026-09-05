# ontoMeta MCP 服务

本体工程和数据治理能力的 MCP (Model Context Protocol) 封装。

除工具外，服务器还通过 MCP prompts 交付 `backend/app/mcp/skills/` 下的 Skill 指引（不消费
prompts 的客户端用 `get_playbook` 工具取同一份正文）；Skill 的启用状态和部署级覆写由 Web 端
「Agent 接入 → 技能」管理，那里也可以把生效正文**直接安装到 Agent 读取 Skill 的目录**
（后端主机上的绝对路径），不必下载 ZIP 再解压。

**出口契约只有一份**：`ontometa-output` 是所有回答的格式、状态口径、截断/ID 规则和"需要
用户选择时怎么问"的总控；其余 skill 正文写 `{{OUTPUT_CONTRACT}}` 占位符，下发时替换成它的
正文。改回答格式只改这一份，导出与安装出去的每份仍自带完整契约。

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 启动 HTTP 服务

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

MCP 服务固定挂载在 `/mcp/`，所有请求都必须携带 Principal/Admin Bearer Token。

### 3. 与 Claude Desktop 集成

#### 远程 HTTP 配置

编辑 Claude Desktop 配置文件：

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

添加（把地址和令牌替换为部署值）：

```json
{
  "mcpServers": {
    "ontometa": {
      "type": "http",
      "url": "https://<你的后端地址>/mcp/",
      "headers": { "Authorization": "Bearer <你的 Principal Token>" }
    }
  }
}
```

⚠️ **注意**：`Principal Token` 决定身份与权限；匿名请求不会被接受。令牌请在
「Agent 接入 → 令牌」创建，不要使用 Admin Token。

#### 使用项目配置文件

项目根目录有一个 `mcp-config.json` 模板，复制其中的配置到 Claude Desktop 配置文件。

### 4. 重启客户端

配置后重启客户端，工具和 Skill prompts 才会加载。

### dsh 工作流 skill

DeepSeek Harness（dsh）可加载项目提供的 `ontometa-mcp` skill。skill 会把本体查询、
大结果聚合、任务六环、publisher 门控、宿主交互确认、异步长轮询和结果呈现固化为一套提示，减少猜 ID、
把受理误报成成功、以及执行失败后重复提交等问题。

在 dsh 中推荐显式调用：

```text
/ontometa-mcp 查询 erpnext 本体概览
```

Skill 文件位于后端包 `backend/app/mcp/skills/`；dsh profile 通过自定义 skill 根挂载，其他
MCP 客户端则通过 `list_prompts` / `get_prompt` 获取。普通任务不会自动套用 ontoMeta 规则；
需要确定的 MCP 路由和执行边界时，请使用 `/ontometa-mcp`。

### 5. 验证

在 Claude Desktop 中，你应该能看到 ontoMeta 工具可用。试试：

```
请列出所有已发布的本体
```

Claude 会调用 `query_ontology` 工具查询数据库。

## 可用工具

查询、提案工具是只读的；生命周期工具只把治理任务写入
``GovernanceArtifact``，并通过既有校验/确认闸门提交执行。它们不直接写数仓表。
每个工具的最低角色见「身份与权限」。

### 本体查询

| 工具 | 作用 |
|------|------|
| `query_ontology` | 本体列表 / 单个本体（域名、版本、发布状态） |
| `query_objects` | 本体内的业务对象，可按角色（`business_object`/`data_table`/`bridge`/`technical`）、关键词、待复核过滤 |
| `query_object_detail` | 单个对象的属性、进出关系、绑定口径、物理落点 |
| `query_relations` | 对象关系（两端对象名、结构类型、基数）；JOIN 的连接键在 `source_evidence` |

### 业务口径（指标 / 标签 / 规则）

| 工具 | 作用 |
|------|------|
| `search_logics` | 按关键词检索口径；`formalized` 标出该条能否编译成 SQL |
| `get_logic` | 单条口径的完整定义：文字口径 + 形式化 AST、绑定对象与字段、ADS 落点 |
| `compile_metric` | 把已发布且已形式化的口径编译成 Doris SQL，附口径展开轨迹与语义证书 |

口径是本体侧的**权威定义**。照着 `expression_summary` 那段文字重写 SQL，同一个指标在
问数、数据应用、物化三处就会各算各的——所以拿数走 `search_logics` → `compile_metric` →
`execute_sql`，产出的 `caliber_trace` 是口径证据，`fanout_note` / `warnings`
（JOIN 可能放大这个数）必须一路带到答案里。

`compile_metric` 只产 SQL、不连数仓，故是 `reader`；真要执行仍要过 `execute_sql` 的闸门。
编译失败带 `code` + `hint`（`no_expression` 尚未形式化、`logic_not_found` 不存在或未发布、
维度不可关联……），照着修，不要绕开口径改写 SQL。

### 血缘 / 落点 / 运行记录

| 工具 | 作用 |
|------|------|
| `get_lineage` | 对象的上下游邻域；`is_derivation` 区分**数据加工血缘**与业务关系；可选 Mermaid |
| `get_landing` | 对象/口径落到哪张物理表、建了吗、搬了吗、能不能查 |
| `get_ops_record` | 按问题族读运行记录：任务执行 / 任务链 / 本体版本 / 治理规约 / 草稿生成 / 合并报告 / 待复核冲突 / 数据源 / 数据应用 / 依赖组件 / 生产割接 |

三条使用纪律：

1. **血缘只认 `structure_type=derivation` 的边**。外键/引用是业务关系，不是「数据从这里来」。
   而且发布只提升业务对象，血缘边常年停在草稿——已发布视图为空时
   `metadata.unpublished_derivation_edges` 会说清草稿里还压着多少条，别把空图读成「没有上游」。
2. **没有落点登记就是没落地**。`get_landing` 报 `not_landed` 时不要按 `ods_{域}_{表}`
   之类的规则推一个表名；keyword 定位默认跨本体，候选带 `domain_name` 用来消歧。
3. **`get_ops_record` 的 task_run 给不出远端失败原因**。投递回执自陈的是「投递成功」，
   终态 failed 来自 Airflow 对账——此时 `metadata.failed_without_reason` 会点名这些任务，
   改用 `get_task_status` 取 `run_url` 看远端日志。会话相关的族（`decision`、
   `scope=conversation`）在 MCP 下明确拒绝：无会话协议塞假 id 会读到别人的记录。

### 取数辅助

| 工具 | 最低角色 | 作用 |
|------|----------|------|
| `find_join_path` | reader | 两对象间本体认可的关联路径：每跳的 ON 键、基数链、扇出风险、可用的 `sql_hint` |
| `profile_values` | 同 `execute_sql` | 某字段**实际存着什么值**：TopN 取值与频次 / 数值区间 / 时间区间 / 空值率 |

两者堵的都是「SQL 语法完全合法、结果却是错的」：

- `find_join_path` 返回 `found=0` 是**结论不是故障**——本体中这两个对象确实无从关联，
  此时不得自造 JOIN。`joinable=false` / `sql_hint=null` 表示 ON 键推不出来，
  半截 SQL 比没有更坏，所以干脆不给。`fanout_risk` 非空表示这条路径会放大度量，
  `safe_aggs` 是仍然安全的聚合。
- `profile_values` **读真实数据，与 `execute_sql` 同价**（取同一份 `agent_run_sql_min_role`，
  不写死）——一次画像等于一句 `SELECT DISTINCT`，写成 reader 就是一个绕过 SQL 权限的后门。
  数据没落地或投影未就绪时返回 `available=false` 与原因，不报错、也不得据此猜字面量。
  就绪判定与落点映射走 `query_routing.prepare_object_read`，与 Data Agent 同一份闸门。

### 数据源与 SQL

| 工具 | 作用 |
|------|------|
| `list_datasources` | 已配置的业务源库与数仓（**不返回任何凭据**） |
| `validate_sql` | 只读校验，不连库 |
| `execute_sql` | 在默认 Doris 数仓执行只读 SQL；可选附加 Vega-Lite 预览 spec；没有显式配置的默认仓就拒绝执行 |

### 任务（治理制品）

| 工具 | 作用 |
|------|------|
| `list_tasks` | 同步/加工/聚合/物化任务列表，附落点、调度与回执摘要 |
| `get_task_status` | 单个任务的状态、Spec、校验报告、回执，并尽力回读 Airflow 实时态 |
| `get_ontology_overview` | 一次返回本体元信息、角色/板块分布和业务对象精简清单 |

### 交互式建数流程

| 工具 | 作用 |
|------|------|
| `start_task_flow` | 用户想建任务但参数没给全时先调它：只问系统定不下来的那几项 |
| `advance_task_flow` | 提交答案并推进；参数齐了给 `status="review"`（执行审查），确认后给可照抄的 `draft_task` 参数 |
| `open_task_form` / `wait_task_form` | 客户端没有原生问答工具时，改用控制台上的一次性网页表单 |

问题与候选取自 `ChatBiService.build_task_form`，**与 Web 表单同源**。流程**不存服务端状态**：
由 `(kind, answers)` 完全决定，`answers` 每次原样带回即可续问。

**只问定不下来的**：有默认值、唯一候选、可选项一律自动填，摆进最后那张执行审查里一次核对。
审查摆的是 Drafter 派生的 Spec（来源 → 落点、装载方式、调度、引擎）+ 校验阻断项，
并给一个 `plan_digest`：确认必须把它原样写回 `__confirm_plan`，改过参数旧 digest 自动失效——
"确认过的方案"与"执行的方案"因此必须是同一份。闭集字段取不到候选时返回 `blocked`，
不放一个没校验过的值进 Spec。

### 任务提案

| 工具 | 作用 |
|------|------|
| `propose_sync` | 源库表 → 数仓 ODS |
| `propose_transform` | ODS → 加工结果表 |
| `propose_materialize` | 本体对象 → 物理表（只出 DDL） |
| `propose_metric` | 已发布业务口径 → ADS 结果表（`business_logic_id` 用 `search_logics` / `get_logic` 取，须 `formalized=true`） |

关系图可通过 `query_relations(include_mermaid=true)` 获取当前结果页的 Mermaid 文本；
结果只覆盖当前分页，若 `metadata.truncated=true` 应继续分页。图表预览同理使用
`execute_sql(include_vega_lite=true)`，spec 只内嵌最多 100 行样本，不替代完整查询结果。

### 任务生命周期

| 工具 | 最低角色 | 作用 |
|------|----------|------|
| `draft_task` | editor | 将 `draft_payload` 落成治理草稿并立即校验 |
| `validate_task` | editor | 重跑校验闸门与 dry-run |
| `confirm_task` | publisher | 确认已通过校验的任务 |
| `execute_task` | publisher | 异步派发已确认任务，立即返回；用 `get_task_status` 轮询 |
| `wait_task_status` | reader | 服务端等待状态变化/终态，避免客户端用 sleep 高频轮询 |

推荐工作流：先 `propose_*` 预览，再 `draft_task`；确认校验报告无阻断项后由有执行授权的
publisher 调用 `confirm_task` 和 `execute_task`。`execute_task` 返回成功只代表已受理，
不代表 Airflow 或数据搬运已经成功；受理后用 `wait_task_status` 等终态。

默认仍需在任务详情逐条开启「允许 Agent 代执行」。可信的 dsh Web 本机 stdio 部署可由管理员
显式开启「本机宿主交互确认」：dsh 用原生 `ask_user_question` 展示任务方案并得到人类批准，
再把 `get_task_status` 返回的任务 digest 同时传给 confirm/execute。digest 绑定任务 Spec 与校验
报告，内容变化后旧确认失效；远程 HTTP、匿名默认角色与 Admin bootstrap token 不接受此模式。

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
| `query_*` / `search_logics` / `get_logic` / `compile_metric` / `get_lineage` / `get_landing` / `get_ops_record` / `find_join_path` / `list_datasources` / `list_tasks` / `get_task_status` / `wait_task_status` / `validate_sql` | `reader` |
| `propose_*` / `draft_task` / `validate_task` | `editor` |
| `confirm_task` / `execute_task` | `publisher` |
| `execute_sql` / `profile_values` | `agent_run_sql_min_role`（默认 `publisher`，与 Data Agent 代跑 SQL 同价） |
| `list_audit_logs` / `get_mcp_stats` | `publisher` |
| `server_info` | `reader` |

**审计**：每次调用（成功 / 失败 / **被拒** / **被限流**）都追加一条 `mcp_audit_logs`（谁、
什么身份、哪个工具、成没成、耗时、脱敏入参），只追加不改写，publisher 可用
`list_audit_logs` 回读、`get_mcp_stats` 聚合。

**限流**：进程内滑动窗口，防 agent 失控循环打爆数仓/DB。每工具每分钟上限和
`execute_sql` 的单独上限在「Agent 接入 → MCP 配置」配置（默认 120/30，0=关闭）。命中回
`rate_limited`（与授权的 `denied` 区分），并做审计去重。

配置建议：给 MCP 单独建一个**最小权限的 Principal**（Agent 接入 →「令牌」，或
`POST /api/principals`）。外部 agent 默认使用 editor（只能提案、落草稿、校验）；只有明确
授权自动确认和执行时才使用 publisher。不要把 `ONTOMETA_ADMIN_TOKEN` 塞进客户端配置，
也不要让 agent 读取 `backend/.env`；admin token 是 superuser，泄漏面更大。

## 远程 HTTP 服务

MCP 固定使用远程 HTTP：挂到后端的 `/mcp/` 路由，agent 用「服务地址 + Principal 令牌」连接，
不碰任何本地路径。匿名访问不支持，无令牌请求直接 401。

客户端配置（以 Claude Desktop 为例，Claude Code 用 `claude mcp add ontometa -t http <url> -H ...`）：

```json
{
  "mcpServers": {
    "ontometa": {
      "type": "http",
      "url": "https://<你的后端地址>/mcp/",
      "headers": { "Authorization": "Bearer <你的令牌>" }
    }
  }
}
```

要点：

- 端点带**尾斜杠** `/mcp/`（Starlette Mount 对无斜杠的 `/mcp` 会 307 重定向）。
- 身份**逐请求**从 `Authorization: Bearer` 解析，与 REST 共用一份角色判定；无令牌一律 401。
- 传输用 JSON 响应模式（非 SSE 流），以兼容主后端的 `BaseHTTPMiddleware`。
- 前端**Agent 接入**菜单分别展示 MCP 配置、MCP 工具、审计监控、技能和令牌。

## 目录结构

```
backend/app/
├── jobs/
│   └── artifact_execution_worker.py # 生命周期异步执行 worker
└── mcp/
    ├── __init__.py
    ├── __main__.py          # 启动入口
    ├── server.py            # MCP 服务器实现 + 限流/授权闸门 + 审计包裹 + 逐请求身份
    ├── auth.py              # 身份解析（stdio env Token / HTTP 逐请求 Bearer → 角色）
    ├── audit.py             # 审计写入点（append-only，脱敏，吞异常）
    ├── rate_limit.py        # 进程内滑动窗口限流
    ├── http_app.py          # 远程 Streamable HTTP 传输（挂 /mcp）+ 匿名拦截
    ├── introspection.py     # 自省/审计/统计的共享数据层（MCP 工具与 REST 共用）
    ├── STATUS.md            # 实施状态与阶段计划
    └── tools/
        ├── __init__.py      # 工具注册机制 + ToolResult / AuthContext / required_role
        ├── _common.py       # 会话与序列化辅助
        ├── query.py         # 本体查询
        ├── objects.py       # 对象 / 关系查询
        ├── datasources.py   # 数据源目录
        ├── sql.py           # 只读 SQL
        ├── tasks.py         # 任务（治理制品）回读
        ├── proposals.py     # 任务提案（只读）
        ├── lifecycle.py     # 草稿/校验/确认/异步执行
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
- [x] Data Agent parity P0/P1 生命周期工具（4 个）与对象聚合
- [x] `get_ontology_overview`（本体概览聚合）
- [x] Phase 3 认证（env Token → 4 层角色）+ 授权（工具级 required_role，fail-closed）+ 审计
- [x] Phase 4 限流（进程内滑动窗口）+ 运维自省（`server_info`）/ 监控（`get_mcp_stats`）
- [x] Phase 5（部分）远程 HTTP 传输（`/mcp/`，逐请求鉴权）+ 前端管理页（设置页 MCP Tab）
- [ ] Phase 5（剩余）资源级权限、本体建模类工具、血缘/落点工具、远程传输生产加固

## 参考

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [MCP SDK (Python)](https://github.com/modelcontextprotocol/python-sdk)
- [完整设计文档](../../../docs/MCP_README.md)
