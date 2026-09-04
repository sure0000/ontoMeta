# MCP 追平 Data Agent 体验 — 实施方案

> 面向**冷启动的执行 agent**：你没有产生本方案的对话上下文。本文自包含，照此执行即可。
> 先读「背景」「现状架构」两节建立地图，再按 P0 → P1 → P2 逐项实施。

## 背景（为什么做）

用真机 **dsh(DeepSeek Harness)** 通过 MCP 调 ontoMeta，跑了一遍典型工作流
（「erpnext 有哪些本体 → 有哪些业务对象 → 把 company 同步到数仓 → 继续走」），对照
ontoMeta 自家的 **Data Agent(Chat BI)** 体验，暴露出几个差距。证据（来自 dsh 会话回放）：

1. **写侧闭环断裂（最严重）**：MCP 只有 `propose_*`（出提案，不写库）。要把提案落草稿→校验→
   确认→执行，**MCP 里没有对应工具**。dsh 的 agent 于是**绕道**：读 ontoMeta 源码找到后端
   `127.0.0.1:8000` → **从 `.env` 抓 `ONTOMETA_ADMIN_TOKEN`** → 用 `curl` 手动走完
   `POST /api/agents/draft|validate|confirm|execute`。既低效（几十步试探），又**绕过了 MCP 的
   角色门控**（会话给的是 editor 令牌、`execute_sql` 本会被拒，但 agent 拿 admin token=publisher
   直接把任务执行了）。
2. **大结果逼 agent 落盘**：`query_objects` 被调用 46 次；查 119 个业务对象时结果过大，
   agent 改写 python 脚本读临时落盘文件去提取。
3. **execute 阻塞超时**：写侧 execute 请求 60s 被截断（Airflow 提交慢），agent 只能自己起后台
   脚本轮询 DagRun。
4. **呈现只有纯文本**：全是 markdown 表格，没有 Data Agent 的图表/血缘图/关系图/六环确认卡。

## 目标 / 非目标

**目标**：让通用 agent 经 MCP 就能安全、顺畅地走完「查 → 提案 → 落草稿 → 校验 → 确认 → 执行 →
追踪」，不必绕道 REST、不必抓 admin token、不必落盘处理大结果、不被写侧 execute 阻塞。

**非目标**：
- 不复刻 ontoMeta 前端的可视化交互（图表/血缘图/六环卡是前端专有；MCP 定位是「让通用 agent
  编排能力」，不是复刻 UI）。P2 只做「能返回可渲染文本」的低成本增强。
- 不改 Data Agent(Chat BI) 本身；MCP 与它并行。

## 现状架构（执行前必读的地图）

**MCP 工具体系**（`backend/app/mcp/`）：
- 工具定义在 `app/mcp/tools/*.py`，用 `@register_tool` 注册（类需 `name`/`description`/
  `input_schema`/`required_role`/`async def execute(self, arguments, auth)`）。新增模块**必须**
  加进 `app/mcp/tools/__init__.py` 末尾的 import 清单，否则静默不注册。
- 授权在 `app/mcp/server.py` 的 `handle_call_tool` **集中 fail-closed 强制**（调用前按
  `tool_required_role(tool)` 拦），工具的 `execute` 不各写鉴权。角色四层
  `reader < editor < reviewer < publisher`（`app/models/principal.py`），判定 `role_satisfies`。
- 身份：stdio 一进程一身份（env `ONTOMETA_MCP_TOKEN`），HTTP 逐请求（`Authorization: Bearer`），
  都走 `app/auth.py:resolve_principal_token`。`auth.principal_name`/`auth.role` 可用。
- 每次调用自动审计（`app/mcp/audit.py` → `mcp_audit_logs`，脱敏、append-only）。
- 已有工具：`query_ontology`/`query_objects`/`query_object_detail`/`query_relations`/
  `list_datasources`/`execute_sql`(publisher)/`validate_sql`/`list_tasks`/`get_task_status`/
  `propose_{sync,transform,materialize,metric}`(editor)/`list_audit_logs`(publisher)/
  `server_info`/`get_mcp_stats`(publisher)。
- 自省/审计/统计的**共享数据层**在 `app/mcp/introspection.py`（MCP 工具与 REST `/api/mcp/*`
  共用，勿在别处另写聚合）。

**写侧管线**（复用它，别另造）——`app/services/agent_pipeline.py:AgentPipelineService`：
- `draft(db, *, kind, intent, context, ontology_id, spec, name, user_created)` → 落一条
  `GovernanceArtifact`（status=`drafted`）。context 路径走 Drafter 派生（`app/agents/registry.py`
  的四类 drafter），spec 直填路径绕过 Drafter 但仍进校验闸门。
- `validate(db, artifact_id, *, context)` → 过校验闸门 + dry-run，写 `validation_report_json`
  （含 `issues[]`/`blocking_count`/`dry_run`），status → `validated` 或退回 `drafted`。
- `confirm(db, artifact_id, *, operator)` → **只接受 `validated`**，status → `confirmed`，
  记 `confirmed_by=operator`。语义是「人工二次确认」。
- `execute(db, artifact_id, *, context)` → **只接受 `confirmed`**，幂等（已 succeeded 直接返回），
  调 `registry.get_executor(kind).execute` 提交 Airflow，写 `execution_receipt_json`。
  **当前是阻塞的**（提交期长，可达数分钟）。
- `list_artifacts` / `get`（读时对账 Airflow DagRun 状态）。
- REST 映射：`POST /api/agents/{draft,validate,confirm,execute}`（`app/api/agents.py`），
  **整个 `/api/agents` 命名空间在 `app/auth.py:_ROLE_OVERRIDES` 里要求 publisher**——这就是
  dsh agent 必须抓 admin token 才能 curl 的原因。
- `propose_*` 工具（`app/mcp/tools/proposals.py`）产出的 `draft_payload = {kind, intent,
  context, ontology_id}`，正是 `POST /api/agents/draft` 的 body、也正是 `agent_pipeline.draft`
  的入参——**新工具直接喂给 draft 即可，不必经 REST**。
- 参考 `app/services/chat_bi.py:_dispatch_propose_action`（Data Agent 的提案逻辑）与
  memory「六环任务确认」「propose_* tools need ledger」，理解人确认哲学。

---

## P0 · 写侧闭环工具（最高优先级，同时补体验与安全）

新建 `backend/app/mcp/tools/lifecycle.py`，注册以下工具，**全部薄壳复用
`agent_pipeline`**（`from app.api.deps import agent_pipeline`），不重写管线逻辑。

### 工具清单

| 工具 | required_role | 复用 | 语义 |
|------|---------------|------|------|
| `draft_task` | `editor` | `agent_pipeline.draft` + 紧接 `validate` | 把提案落成草稿并立即校验，返回 `task_id` + 校验报告 |
| `validate_task` | `editor` | `agent_pipeline.validate` | 对已落草稿的任务重跑校验（改过参数后用） |
| `confirm_task` | `publisher` | `agent_pipeline.confirm` | 人工二次确认（operator = `auth.principal_name`）|
| `execute_task` | `publisher` | `agent_pipeline.execute`（**异步化**）| 触发执行，**立即返回**，状态走 `get_task_status` 轮询 |

> `get_task_status`（已有）负责读回状态/回执，不必新增读工具。

### draft_task（editor）

- 入参：`kind`(sync/transform/materialize/metric)、`intent`、`context`(dict)、`ontology_id`。
  与 `propose_*` 的 `draft_payload` 同构——**建议 agent 先 `propose_*` 预览、再 `draft_task` 落库**。
- 实现：`art = agent_pipeline.draft(db, kind=kind, intent=intent, context=context,
  ontology_id=ontology_id, user_created=True)`；随即 `agent_pipeline.validate(db, art.id)`。
  返回 `{task_id, status, name, validation: {issues[], blocking_count, dry_run}}`。
- 复用 `proposals.py` 里的缺参校验（`_missing_action_context` / `_sync_context_errors`）先挡一遍，
  错误口径与 `propose_*` 一致。
- 为什么 editor 就能 draft+validate：这两步**不产生副作用**（只落草稿 + dry-run），让 agent／人
  在执行前看清「将发生什么」；真正动数仓的 confirm/execute 才抬到 publisher。

### validate_task（editor）
- 入参：`task_id`。实现：`agent_pipeline.validate(db, task_id)`，返回同上的 validation 结构。

### confirm_task（publisher）
- 入参：`task_id`。实现：`agent_pipeline.confirm(db, task_id, operator=auth.principal_name or auth.principal_id)`。
- 校验闸门有阻断项时 `confirm` 会抛 `PipelineError`；捕获后回 `ToolResult(success=False, error=...)`，
  提示「先解决校验阻断项」。

### execute_task（publisher，**必须异步**）
- 入参：`task_id`。
- **不得阻塞**：现有 `agent_pipeline.execute` 提交 Airflow 期长（60s+ 会把 MCP 调用拖爆）。
  做法（执行 agent 选其一，优先复用现有异步设施）：
  - 复用进程外 worker 模式（参考 `app/jobs/draft_worker.py` 的分离子进程 `start_new_session`，
    见 memory「draft generation out-of-process」）；或
  - 后台线程跑 `agent_pipeline.execute`，工具立即返回。
- 返回：`{task_id, status: "executing", note: "已提交，用 get_task_status 轮询终态"}`。
- 幂等：`execute` 本身对 `succeeded` 幂等；工具层对「已在 executing」也应直接返回当前状态，不重复提交。

### 注册与门控
- 在 `app/mcp/tools/__init__.py` 的 import 清单加 `lifecycle`。
- 角色由各工具 `required_role` 声明，服务器集中强制——**不要**在 `execute` 里另写鉴权。
- 效果：editor 令牌的 agent 能 propose→draft→validate（看清将发生什么）但止步于此；执行需
  publisher 令牌。**「是否允许 agent 自动执行」的决定权，落在「给 agent 发什么角色的令牌」上**，
  与现有 REST `/api/agents` 需 publisher 完全一致——agent 再没有理由、也没有能力去抓 admin token。

### 关键设计决策（务必保留）
- **人确认哲学不破**：Data Agent 是「人在六环逐环确认」。MCP 无人交互，故用**角色门控**替代：
  confirm/execute 需 publisher，且给 publisher 令牌本身就是「授权这个 agent 能执行」的人为决定。
  文档/前端应提示：给 dsh 类外部 agent **默认发 editor 令牌**（只到 draft/validate），确需自动执行
  再发 publisher。
- **审计已自动覆盖**：confirm/execute 会被 `mcp_audit_logs` 记下（谁、什么身份、成没成），
  publisher 的每次执行都留痕。

---

## P1 · 大结果聚合 + execute 异步

### query_objects 聚合模式（`app/mcp/tools/objects.py`）
- 加参数 `group_by`（`role` / `segment`）：命中时**不返回明细**，返回分布统计
  （如 `{by_role: {technical: 574, bridge: 315, business_object: 119}}`）。复用
  `OntologyQueryService`（`list_object_types` 已支持 `role_in`/`segment_id` 过滤，可按组计数）。
- 目的：agent 问「有哪些业务对象」时先拿分布，再按需拉某一类明细，避免一次倒 1035 条、
  避免 agent 落盘处理。
- 明细路径保留游标分页（已有 `limit`/`offset` + `total`/`truncated`），并考虑默认更精简字段集。

### 可选：get_ontology_overview（新增，reader）
- 一次返回：本体元信息 + 对象角色分布 + 业务对象清单（名+角色+段），把「查本体→查对象分布→
  拉业务对象」三次往返压成一次。减少 dsh 会话里 `query_ontology`×17 + `query_objects`×46 的往返。

### execute 异步（并入 P0 的 `execute_task`，此处只是强调）
- 任何写侧工具都不得长阻塞；execute 提交即返回，终态轮询走 `get_task_status`。

---

## P2 · 可渲染结构化文本（低优先级，可选）

MCP 是文本协议，能补的一半是「让工具结果里带**可被 agent 渲染**的文本」：
- 关系图 / 血缘：`query_relations` 或新 `get_lineage` 可选返回 **mermaid** 文本块。
- 图表：`execute_sql` 结果可选附一段 **vega-lite** spec（agent/宿主若支持就画）。
- 这只是增强；真要交互式可视化仍走 ontoMeta 前端。**不要**为此引入前端渲染依赖。

---

## 安全（红线，随 P0 一并处理）

1. **补全写侧工具后，堵住绕道**：agent 不再需要、也不应再读 `.env` 抓 admin token。
2. **admin token 不该躺在 agent 够得着的地方**：给 MCP/外部 agent 的场景，只发**最小权限
   Principal 令牌**（设置页「角色与令牌」）。评估：`ONTOMETA_ADMIN_TOKEN` 明文在 `backend/.env`
   —— 至少在文档里明确「给 agent 用 principal 令牌，不给 admin token」；进一步可考虑把 admin token
   移出 agent 可读范围。
3. confirm/execute 严格 publisher，审计留痕，符合现有 `/api/agents` 门控。

---

## 测试要求

新增 `backend/tests/test_mcp_lifecycle.py`，至少覆盖：
- `draft_task` editor 可调，落库产出 `drafted→validated`，返回校验报告；缺参回 missing + 候选
  （与 `propose_*` 同口径）。
- `validate_task` 重跑校验。
- `confirm_task` / `execute_task` **reader/editor 被服务器授权闸门拦（denied）**，publisher 放行。
- `confirm_task` 对非 validated 状态报错；`execute_task` 对非 confirmed 报错、对 succeeded 幂等。
- `execute_task` **不阻塞**：调用在合理时间内返回 `executing`（用替身 executor，别真打 Airflow）。
- `query_objects(group_by=role)` 返回分布而非明细。
- 全量 `pytest` 绿（当前基线 **2182 passed**）。

参照现有 `tests/test_mcp_tools.py` / `test_mcp_auth.py` 的 fixture 与替身写法
（`call_via_server` + monkeypatch `resolve_auth_context`）。

## 交付检查清单

- [x] `app/mcp/tools/lifecycle.py`：draft/validate/confirm/execute_task（薄壳复用 agent_pipeline）
- [x] `execute_task` 异步、立即返回（原子抢占 + 分离 worker）
- [x] 注册进 `tools/__init__.py` import 清单
- [x] `query_objects` 加 `group_by` 聚合模式（P1）
- [x] `get_ontology_overview`（P1 可选项）
- [x] P2 可渲染增强：`query_relations(include_mermaid=true)`、`execute_sql(include_vega_lite=true)`
- [x] 前端「MCP 服务」页 / README 更新：给外部 agent 用最小权限令牌、写侧工具的角色门槛说明
- [x] dsh `ontometa-mcp` skill：友好输出、真实 ID 路由、六环执行和失败分层
- [x] `tests/test_mcp_lifecycle.py` + 全量绿
- [x] `backend/app/mcp/STATUS.md` 追加本阶段记录
- [x] 口径三件套 `search_logics` / `get_logic` / `compile_metric`（补 `propose_metric`
      必填 `business_logic_id` 却无从查起的断路）+ dsh skill 口径优先路由
      + `tests/test_mcp_logics.py`

## 端到端验收（真机）

配好后在 dsh 里重跑同一工作流（「把 company 同步到数仓」）：agent 应**全程用
`mcp__ontometa__*`**（propose→draft→validate→（publisher 令牌下）confirm→execute→get_task_status），
**不再出现 Bash/Read/curl 绕道、不再读 .env 抓 token、不再被 execute 阻塞**。用
`list_audit_logs` 复核：写侧操作都以正确身份留痕。

本阶段已在 dsh（editor Principal）实测 `server_info`、
`query_objects(group_by=role)`（分布统计不返回明细）以及
`propose_sync → draft_task → validate_task`（task_id 可回读，状态 `validated`，
`blocking_count=0`）。

**publisher 侧也已真机跑过**（2026-09-04，`confirmed_by=dsh-publisher-acceptance-20260904`）：
`confirm_task → execute_task` 全程走 MCP，DAG/spec/jobs 投递到远端 Airflow、DagRun 建出、
`get_task_status` 对账回读到终态——**MCP 链路本身是通的**。但三条「同步 · 公司 → 数仓 ODS」
的终态都是 `failed`，失败在 Flink/Airflow 侧（同期「客户分组」「代码表目录」是 succeeded，
所以不是通用故障）。回执里没有失败原因，agent 只能拿到 `run_url`——这正是 `get_ops_record`
仍缺位的代价，见 STATUS.md 待办。

---

## 参考

- 现有 MCP：`backend/app/mcp/`（STATUS.md 有各阶段记录）、`docs/MCP_*.md`（注意：设计稿是想象稿，
  引用了本仓不存在的模型，**以真实代码为准，复用既有服务/RBAC/审计，不另起炉灶**）。
- 写侧管线：`app/services/agent_pipeline.py`、`app/agents/`（drafter/executor/registry/validation）。
- Data Agent 对照：`app/services/chat_bi.py`（`_dispatch_propose_action` / 六环确认）。
