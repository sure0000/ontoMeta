# MCP 服务实施状态

**当前阶段**：Phase 4 ✅ 已完成（限流 + 运维自省/监控）
**下一阶段**：Phase 5 能力补齐与开放（含资源级权限、远程传输）
**更新时间**：2026-09-03

---

## ✅ Phase 1: 基础设施

- MCP SDK 集成、工具注册机制（`@register_tool` + `TOOL_REGISTRY`）
- `ToolResult` 统一信封、`AuthContext` 骨架
- stdio 服务器入口、首个工具 `query_ontology`

### Phase 1 的两处返工

1. **依赖没真装上**。`requirements.txt` 写的是 `mcp>=0.9.0`，而 `server.py` 用的是
   2.x 才有的 `on_list_tools` / `on_call_tool` 回调（1.x 是 `@server.list_tools()`
   装饰器）。venv 里根本没装过 `mcp`，所以这个矛盾一直没暴露。已改为 `mcp>=2.1.0`。
2. **失败没置 `is_error`**。工具失败时只把 `success: false` 写进 JSON 正文，MCP 客户端
   看到的仍是一次成功调用——模型得自己从文本里读出失败，重试与降级全部失灵。
   已在 `_text_result` 里让 `is_error` 跟着 `success` 走，并补 `structured_content`。

---

## ✅ Phase 2: 核心工具（13 个，全部只读）

| 模块 | 工具 |
|------|------|
| `query.py` | `query_ontology` |
| `objects.py` | `query_objects` / `query_object_detail` / `query_relations` |
| `datasources.py` | `list_datasources` |
| `sql.py` | `execute_sql` / `validate_sql` |
| `tasks.py` | `list_tasks` / `get_task_status` |
| `proposals.py` | `propose_sync` / `propose_transform` / `propose_materialize` / `propose_metric` |

### 第一版是照着想象中的 schema 写的

Phase 2 的初版四个模块**全部无法导入**，且当时没有任何用例发现——唯一的「测试」是一个
手跑脚本。留档以免重犯：

| 初版写法 | 真实情况 |
|----------|----------|
| `from services.query_gateway import QueryGateway` | 该模块不存在；只读 SQL 在 `services/data_app_executor` |
| `SyncTask` / `TransformTask` / `MaterializeTask` / `MetricTask` | 四类任务共用一张表 `GovernanceArtifact`（`kind` 区分） |
| `ObjectType.role` / `.source_table_name` / `.entity_status` | 真实列是 `table_role` / `source_ref` / `status` |
| `RelationType.source_object_id` / `.relation_type` | 真实列是 `source_object_type_id` / `structure_type` |
| `Property.is_primary_key` / `.nullable` | 不存在；有的是 `required` / `semantic_type` |
| 提案里手拼 `ods_{schema}_{table}` / `dwd_` / `dm_` 前缀 | 落点由 Drafter 按 `ods_{数据域}_{原表名}` 派生，前缀不是调用方能定的 |

### 重写后的三条口径

1. **读侧复用服务层，不另写 ORM 查询**。对象摘要里的 `source_provenance` /
   `landing` / `property_count` 都是派生值，绕开 `OntologyQueryService` 直接查表会
   静默丢掉它们（对象会整片置灰、落点错位）。任务查询同理走 `AgentPipelineService`
   ——它在读时对账 Airflow DagRun 状态，直接查表读到的是陈旧状态。
2. **只读校验复用 `data_app_executor.is_read_only`**。初版另写了一份关键字黑名单，
   两套校验一旦分叉，宽的那套就是实际的安全边界。执行目标同样复用
   `resolve_domain_data_source`（fail-closed：没有显式默认 Doris 仓就不执行）。
3. **提案走真 Drafter，且不写库**。`propose_*` 调用 `registry.get_drafter(kind).draft()`
   派生 Spec，再过一次 `validate_spec` 规约校验，返回 `draft_payload` 供人在前端确认后
   `POST /api/agents/draft`。缺参当场回 `missing` + 真实候选（复用
   `_missing_action_context` / `_action_context_candidates`，键名不另抄一份）。

### 验证

- `tests/test_mcp_tools.py`：43 条，覆盖注册表、schema 合法性、各工具真实调用、
  stdio 回调层（含 `is_error` 与未知工具）。
- 真实开发库端到端：erpnext 本体 1035 个对象 / 1279 条关系、28 条任务（含回执与
  Airflow 实时态回读）、`propose_sync` 派生出
  `sys.waits_by_user_by_latency → ods.ods_erpnext_waits_by_user_by_latency`（0 条阻断项）。
- stdio 握手：官方 client 拉起 `python -m app.mcp.server`，`initialize` + `list_tools`
  （13 个）+ `call_tool` 均通过。

---

## ✅ Phase 3: 认证与授权（含审计）

**没照设计稿 `docs/MCP_SECURITY_DESIGN.md` 的 5 角色 `rbac.py` / `User` / session 走**
——那些在本仓根本不存在。复用**已落地**的 4 层 Principal RBAC
（reader < editor < reviewer < publisher，见 `app/models/principal.py`），与 REST 中间件
咬同一份判据。

### 认证（`app/mcp/auth.py`）

stdio 没有逐请求 HTTP 头：**一条会话（一个子进程）= 一个身份**，启动时解析一次。身份来自
启动 MCP 服务器的客户端在其配置 `env` 块里传入的 `ONTOMETA_MCP_TOKEN`，与 REST 的
`X-Admin-Token` / Principal Token **同价**：

- Token = `ONTOMETA_ADMIN_TOKEN` → publisher（superuser，不查库）
- Token = 某启用中 Principal → 该主体角色
- 无 Token → 匿名，角色回落 `mcp_default_role`（默认 `reader`，本地只读开箱即用；
  置空则无匿名身份）

判定逻辑不另抄一份：`app.auth.resolve_principal_token` 从 `resolve_principal(request)` 里
抽出来，HTTP 与 stdio 共用——两条入口的鉴权语义不能分叉。

### 授权（`app/mcp/server.py` 集中强制，fail-closed）

每个工具声明 `required_role`，服务器在调用**前**统一拦（工具 `execute` 不各写鉴权）：

| 工具 | 最低角色 |
|------|----------|
| `query_*` / `list_datasources` / `list_tasks` / `get_task_status` / `validate_sql` | `reader` |
| `propose_*` | `editor`（写侧起草环节；reader 不该能起草数据任务） |
| `execute_sql` | `settings.agent_run_sql_min_role`（默认 `publisher`） |
| `list_audit_logs` | `publisher` |

关键对齐：`execute_sql` 取与 Data Agent `run_sql` **同一个**配置项——否则 MCP 就成了
绕过权限模型代跑 SQL 的后门。

### 审计（`app/models/mcp_audit.py` + `app/mcp/audit.py`，append-only）

每次调用（成功 / 业务失败 / **被授权拦下** / 异常）都落一条 `McpAuditLog`：谁、什么身份、
哪个工具、成没成、denied、耗时、脱敏入参。三条纪律：

1. **绝不影响主链路**：写审计失败吞异常、rollback，不改变调用结果（照
   `chat_bi_ledger.record_decision` 先例）。
2. **脱敏**：入参里 token/password/dsn 类键落库前 redact（复用 ledger 的 `_redact`）。
3. **只追加不改写**：无 `updated_at`。回读走 publisher 门控的 `list_audit_logs`。

### 验证

- `tests/test_mcp_auth.py` 17 条：Token→角色、fail-closed 授权、`execute_sql` 与
  `run_sql` 同价、审计留痕 + 脱敏 + denied 事件。
- 迁移 `alembic/versions/e2d48fc8520a_mcp_audit_logs.py`，已 `upgrade head` 到 dev Postgres。
- stdio 端到端：匿名（reader）query 放行、propose/execute_sql/audit 被拒；admin token
  （publisher）全放行，审计真写进 dev 库并可回读。

### 仍未做（留给后续）

- 资源级权限（某主体只能看某数据域/数据源）——当前是工具级 + 角色级，未到行级。见 Phase 5。
- 远程传输（streamable HTTP）落地前不要放开——届时身份不再是「一进程一 env Token」，
  需要逐请求鉴权与来源校验。见 Phase 5。
- 设计稿里的「本地 MCP 首次 `select_identity` 选身份」未做：stdio 下身份由启动 env 给定，
  更简单也更不易被会话内提权，故不引入运行期改身份的工具。

---

## ✅ Phase 4: 限流 + 运维自省/监控

只做在 stdio 现实下**真正有依据**的部分。设计稿里的「失败 N 次锁账户 / 通知管理员 /
多租户隔离」在本环境**无机制可依**——stdio 一会话一身份、用户自己就能重启进程，锁不了
也没人可通知，故不做；只做**可观测**：把异常暴露出来，处置交给人。

### 限流（`app/mcp/rate_limit.py`）

**进程内滑动窗口**，不查审计表——stdio 一进程一会话，进程内计数即全局，不给下游 DB
平白加读负载。防的是**最现实的风险**：agent 失控循环（坏 prompt 让它每秒调几十次
execute_sql，几分钟打爆数仓）。

- 每工具每分钟上限 `mcp_rate_limit_per_minute`（默认 120，0=关闭）；`execute_sql` 直打
  数仓、单独更严 `mcp_execute_sql_rate_limit_per_minute`（默认 30）。二者是**行为参数**，
  随部署走 env（与 `agent_run_sql_min_role` / `agent_soundness` 同类，不属于「连接配置进
  Web 设置页」）。
- 滑动窗口只对**放行**的调用计数——被限流拒的不计入，否则窗口永满、永久封锁。
- 闸门放在**授权之前**：失控循环可能全是被拒的调用，只在放行后限流的话，被拒调用照样
  每次刷审计、打 DB。限流命中回 `rate_limited`（与授权的 `denied` 区分），审计做去重
  （同一工具每分钟至多一条限流审计，免得「被限流」本身把审计表刷爆）。

### 运维自省 / 监控（`app/mcp/tools/monitoring.py`）

- `server_info`（reader）：版本、工具清单与各自最低角色、**当前会话身份**、限流配置、
  审计表可达性。自查「我这条会话什么权限、某工具为什么被拒」一眼看清。
- `get_mcp_stats`（publisher）：基于审计表的使用统计——总量、成功/业务失败/被拒/被限流、
  按工具与角色分组。把异常信号（某角色被拒激增之类）暴露出来。

### 验证

- `tests/test_mcp_monitoring.py` 13 条：滑动窗口 / execute_sql 独立上限 / 被拒不占窗口 /
  审计去重 / 限流在授权前 / server_info 身份与配置 / get_mcp_stats 聚合与 publisher 门控。
- stdio 端到端：`MCP_RATE_LIMIT_PER_MINUTE=3` 连调 5 次 validate_sql，第 4/5 次被限流
  （retry≈59.8s，去重后只落 1 条限流审计）；server_info 报 16 工具 / publisher 身份 /
  env 生效的限流配置；get_mcp_stats 聚合正确。

---

## 📋 Phase 5: 能力补齐与开放（待实施）

设计稿 `docs/MCP_TOOL_DESIGN.md` 列了 20 个工具，Phase 2/4 交付了查询/SQL/任务/提案/
运维共 16 个。剩下的：

- [ ] **资源级权限**（某主体只能看某数据域/数据源）：当前是工具级 + 角色级，未到行级。
      要给 Principal 关联可访问的 domain/datasource，并在所有查询工具注入过滤——工程量
      不小、在单机可信 stdio 场景收益有限，故留到此处而非硬塞进 Phase 3/4。
- [ ] 本体建模类（`infer_ontology_from_datahub` / `classify_business_objects` /
      `infer_relationships` / `validate_ontology`）——都是写侧或长耗时任务，
      要先想清楚在 MCP 下怎么表达「异步 + 人工确认」
- [ ] `get_lineage` / `get_landing` / `get_ops_record`
- [ ] 治理规约类（`validate_against_policy` / `lint_task_spec` /
      `get_active_governance_standard`）
- [ ] 远程传输（streamable HTTP）与对外开放

- [ ] 本体建模类（`infer_ontology_from_datahub` / `classify_business_objects` /
      `infer_relationships` / `validate_ontology`）——都是写侧或长耗时任务，
      要先想清楚在 MCP 下怎么表达「异步 + 人工确认」
- [ ] `get_lineage` / `get_landing` / `get_ops_record`
- [ ] 治理规约类（`validate_against_policy` / `lint_task_spec` /
      `get_active_governance_standard`）
- [ ] 远程传输（streamable HTTP）与对外开放

---

## 📝 决策记录

### DR-1: 为什么新建模块而不是改造 Data Agent？

风险控制 + 可并行对比 + 回滚简单（删 `app/mcp/` 即可）。现有 Data Agent 未改一行。

### DR-2: 为什么 MCP 侧只给只读工具？

写侧的安全边界是「人工逐环确认」，它长在前端与 REST 流水线上。把 draft/confirm/execute
暴露成 MCP 工具，等于让通用 agent 绕过确认闸门。提案工具因此只出 `draft_payload`，
落库动作留给人。

### DR-3: 为什么提案要走 Drafter 而不是直接组 Spec？

同一个坑在表单侧犯过一次（见 memory「任务面板绕过 drafter」）：表单收上来的值被当成
Spec 直填，结果 sync 缺 source、transform 缺 ontology_id、metric 忽略
business_logic_id——任务能建、执行「成功」，却什么都没搬。提案卡上展示的必须是执行时
真会用的那份 Spec。

### DR-4: 为什么加了设计稿里没有的 `list_datasources`？

`propose_*` 要 `source_datasource_id` / `target_datasource_id` 的真实 id。没有目录工具时，
调用方唯一的拿法是「先提一个缺参提案、从报错里读候选」——那条路能走通，但不该是唯一的路，
否则模型很容易改为自己编一个 id。

---

## 📚 参考文档

- [MCP 架构设计](../../../docs/MCP_ARCHITECTURE_REDESIGN.md)
- [MCP 工具设计](../../../docs/MCP_TOOL_DESIGN.md)
- [MCP 安全设计](../../../docs/MCP_SECURITY_DESIGN.md)
- [MCP 交互能力分析](../../../docs/MCP_INTERACTION_CAPABILITY.md)
- [MCP 实施计划](../../../docs/MCP_IMPLEMENTATION_PLAN.md)
