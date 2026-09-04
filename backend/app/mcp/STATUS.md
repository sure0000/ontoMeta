# MCP 服务实施状态

**当前阶段**：Phase 5 部分完成（✅ 远程 HTTP 传输 + 前端管理页；✅ Data Agent parity P0/P1/P2 可实现项；✅ 口径三件套；✅ 血缘/落点/运行记录；✅ 取数辅助；治理规约、建模类工具、资产目录与资源级权限待做）
**更新时间**：2026-09-04

## ✅ Agent 接入控制台（2026-09-04）

完成 Agent 接入控制面的首版实现：

- 前端新增「Agent 接入」一级菜单，拆分为 MCP 配置、MCP 工具、审计监控、技能、令牌五个页面；设置页保留本机 Admin Token，
  不再重复展示 MCP/Principal 管理。
- 6 份 dsh Skill 迁入 `backend/app/mcp/skills/` 作为内置 pack；`mcp_skills` 记录只保存 DB 覆写、
  启用状态和内置摘要。`app.mcp.skills` 合并两类来源并在保存时检查 frontmatter、输出契约、通用底线
  和全部注册工具覆盖度。
- MCP Server 注册 `list_prompts` / `get_prompt`，服务器 instructions 只包含安全红线与路由表；Skill
  读取及管理 REST 写入均进入 MCP 审计。
- `/api/mcp/skills*` 提供 reader 读、publisher 写的 Skill 管理接口；令牌页通过 Principal 角色展示
  可调用工具、最近调用和远程 HTTP 配置模板，明文令牌仍仅在创建/轮换响应中出现一次。
- 技能页支持导出当前生效 Skill 或全部已启用 Skill 的 ZIP 安装包，包内按
  `<skill-name>/SKILL.md` 组织，不包含数据库元数据或凭据。
- Skill 默认以只读方式交付；必须显式点击“编辑”才进入编辑态，保存覆写仍要求 publisher。
  新增 `mcp_skill_versions` 追加式版本快照，支持查看历史和回滚；回滚会生成新的版本记录，不改写历史。
- MCP 五项运行期配置落到 `dependency_components` 的 `mcp` 连接 JSON，`SettingsService` 每次运行期
  读取；对外固定提供 HTTP 服务且必须携带令牌，匿名策略和 HTTP 开关不再作为运行期选项。
  `ONTOMETA_MCP_TOKEN` 仍是 bootstrap 身份变量（仅保留兼容 stdio 启动器）。

---

## ✅ dsh skill 集整备（6 份，工具全覆盖）

MCP 只提供工具，**什么时候用哪个、结果怎么解读、什么话不能说**全在 skill 里——少一份
指引，模型就在那一块自由发挥。随着工具从 16 涨到 29，skill 是一路打补丁堆上来的，这次
做了一次整备：

- **补齐覆盖**：新增 `ontometa-admin`（`server_info` / `list_audit_logs` / `get_mcp_stats`
  ——「我是什么身份、为什么被拒、谁调过什么」），此前这两个 publisher 工具不在任何 skill 里；
  `ontometa-task-plan` 把四个 `propose_*` 的真名写出来（原先只有 `propose_*` 通配）。
- **总入口去重**：`ontometa-mcp` 已经长成了四份专用 skill 的副本（查询路由 + 六环流程各一套），
  两处并存就一定会漂——我自己就在连续几次改动里两边各改一遍。现在它只做**路由 + 共同底线 +
  输出契约**，工具顺序以专用 skill 那份为准。
- **专用 skill 自足**：总入口是 `disable-model-invocation: true`，模型自动路由时根本不加载它。
  所以安全底线（不读 `.env`、不绕 REST、凭据不入回答）每份自带一段「通用底线」，
  而不是指望总入口兜底。

**把「skill 齐备」变成被检查的属性**（`tests/test_dsh_skill.py`，14 条）：

- `test_every_registered_tool_has_skill_guidance`：**注册表里每个工具都必须在某份 skill 里
  被提到**。加了工具不写指引，测试直接失败——已用「往注册表塞一个没写指引的工具」负向自检过，
  确实会拦下。
- 每份 skill 的关键指引断言在**自己**身上，不再是「在所有 skill 的并集里找一遍」——
  并集能过只说明标记存在于某处，不代表用得上它的那份里有。
- 专用 skill 必须带「通用底线」；总入口必须路由到全部 5 份；三条会安静出错的红线
  （口径权威 / 连接键与字面量要查 / 运行事实只从记录读）必须在总入口写明。

全量 **2265 passed, 1 skipped**。

---

## ✅ 取数辅助：find_join_path / profile_values

两个工具堵的是同一类失败——**SQL 语法完全合法、结果却是错的**，而且不报错：

- `find_join_path`（reader）：两对象间本体认可的关联路径，给每跳的 ON 键、基数链和
  `sql_hint`。三条语义原样保留自 `semantic_navigator`：**找不到路径不是错误**
  （`found=0` 是「本体中这两个对象确实无从关联」这条事实本身，报成错误模型下一步就自己编
  JOIN）；**ON 推不出就不给 `sql_hint`**（半截 SQL 比没有更坏）；`fanout_risk` 说明这条路径
  会放大度量，并给出仍然安全的 `safe_aggs`。
- `profile_values`（**与 `execute_sql` 同价**）：某字段实际存着什么值。本体只保证字段存在，
  不保证模型猜的枚举写法在库里——猜错的字面量让查询**返回 0 行而不报错**，答案就错得看不出来。

**权限不写死**：`required_role = settings.agent_run_sql_min_role`，与 `execute_sql` 取同一份
配置。一次画像等于一句 `SELECT DISTINCT`，写成 reader 就是一个绕过 SQL 权限的后门；
测试直接断言这两个工具的角色相等，防止哪天有人图省事把它降成 reader。

**顺带消掉一处即将分叉的重复**：「读真实数据前要过哪几道闸门」（割接 → 就绪 → **对账重判**
→ 落点映射）原本只存在于 `chat_bi._dispatch_profile_values` 里。MCP 再抄一份的话，松的那边
就成了实际的安全边界，于是抽成 `query_routing.prepare_object_read`，两边共用；chat_bi 已改为
调用它，`tests/test_column_profiler.py` 里既有的越权/未就绪/未知字段三条测试原样通过。
对账那一步不能省——就绪结论会因为「没人推进过对账」而陈旧（见 memory
「Readiness blocks on stale state」）。

**本体作用域从对象自己反查**，不猜「当前锚定本体」：锚错本体会找不到路径，而调用方会把它
读成「这两个对象确实无从关联」——一个看起来有值的错答案。

dsh skill 同步：`ontometa-query` 的自由查询顺序前置了「先定关联、再定字面量」，并写明
`found=0`/`joinable=false`/`fanout_risk`/`available=false` 各自该怎么说。
验证：`tests/test_mcp_query_aids.py` 11 条，全量 **2252 passed, 1 skipped**。

---

## ✅ 血缘 / 落点 / 运行记录：get_lineage / get_landing / get_ops_record

补的是**「已经发生过什么」**——此前 MCP 能查本体、能提案、能推任务，唯独读不回运行事实。
三个都是 reader，薄壳复用既有服务层（`OntologyQueryService.get_ontology_graph`、
`services/ops_records` 的 `read_landing` 与 13 族 `REGISTRY`），不新写读模型。

**顺带修掉一个一直在的静默 bug**：`OntologyQueryService.get_ontology_graph` 的两个
edge 构造点都漏填 `GraphEdge.structure_type`（字段在 schema 上，默认 None），于是
**图上的加工血缘和外键关系长得一模一样**。Data Agent 的 `get_lineage` 工具描述写着
「structure_type=derivation 的边是数据加工血缘」，可那个字段从来没有过值——血缘视角
一直读不出「数据从哪来」，只能把外键当来源。已补齐两处，`tests/test_segment_grouped_graph.py`
加了全量分支 + BFS 截断分支的回归。

**三条不可替代的判断，都做成了显式信号而不是沉默**：

1. **空的已发布血缘图 ≠ 没有血缘**。发布只提升业务对象，derivation 边常年停在草稿——
   真库实测 erpnext 的 57 条加工血缘**全部**是 `suggested`。已发布视图下 0 条时，
   `metadata.unpublished_derivation_edges` 报出草稿里压着多少条并给出
   `published_only=false` 的出口；不这么做，调用方会把空图读成「这个对象没有上游」。
2. **没有落点登记就是没落地**。`get_landing` 报 `not_landed` 时 note 明说不要按命名
   规则推表名。keyword 定位默认跨本体（odoo 和 erpnext 各有一个「公司」，实测就撞上了），
   候选带 `domain_name` 消歧，主体不唯一时返回候选而不是猜一个。
3. **失败却没有失败原因，要说破**。`_receipt_failure` 只读投递回执自陈；远端
   Airflow/Flink 跑挂时回执是「投递成功 / state=queued」，终态 failed 来自对账。
   此时 `metadata.failed_without_reason` 点名这些任务并指向 `get_task_status` 的
   `run_url`——真库上正好命中 2026-09-04 那三条 dsh sync。

**MCP 无会话，故明确拒绝两类请求**而不是塞假 id：`decision` 族按会话组织、
`task_run` 的 `scope=conversation` 同理；`landing` 族也从 `get_ops_record` 的 enum 里
拿掉，指向会先解析主体的 `get_landing`。

dsh skill 同步：`ontometa-discovery` 加血缘/落点路由与「外键不是数据来源」的口径，
`ontometa-task-execute` 扩为「执行 + 运行追溯」（含 `as_of` vs `observed_at` 之分）。
验证：`tests/test_mcp_ops.py` 19 条 + 图谱回归 1 条，全量 **2241 passed, 1 skipped**。

---

## ✅ 口径三件套：search_logics / get_logic / compile_metric

**补的是一条断路**：`propose_metric` 必填 `business_logic_id`，而在此之前 MCP 侧没有
任何办法查到口径 id——只能靠 `query_object_detail` 里的 `business_logics` 碰运气反查。

- 三个工具都薄壳复用既有服务层：检索/详情走 `OntologyQueryService.list_business_logics`
  与 `get_business_logic`，编译走 `services/metric_compiler.compile_metric`（同一个
  编译器 Data Agent 也在用，不另写一份）。
- **口径优先于自写 SQL**。`compile_metric` 把「模型照着口径文字重写 SQL」换成「选口径 +
  选维度」，幻觉面从整个 SQL 语法空间坍缩到一组受控枚举；产物带 `caliber_trace`
  （口径展开轨迹）与语义证书，`fanout_note`/`warnings` 一路透传，不在工具层吞掉。
- **三者都是 `reader`**：只编译、不连数仓，产物是一段 SQL 文本。要执行仍得过
  `execute_sql` 的 `agent_run_sql_min_role` 闸门——权限边界还在原处，没被这条路绕开。
- **详情必须精简投影**：`BusinessLogicDetail` 带着给编辑界面下拉框用的
  `available_object_types` / `available_properties`，在 erpnext 本体上实测约 1.5 MB /
  5 MB。原样 dump 一次就能把 MCP 会话打爆，故只投影摘要 + 表达式 + 关联对象/字段引用，
  并有测试钉住这几个键不得出现。
- **不给填 None 的键**：列表读模型 `BusinessLogicOut` 没有 `ontology_id`，检索结果就
  **不带**这个键，而不是一律填 `null`——填 `null` 读起来是「这条口径不属于任何本体」，
  一个看起来有值的错答案比缺字段难发现得多。本体归属去 `get_logic` 取。
- 编译失败带 `code` + `hint` 回灌（`no_expression` / `logic_not_found` / 维度不可关联 /
  会扇出），与 `propose_*` 的缺参回灌同一取向：给可执行的下一步，不降级成猜一段 SQL。
- dsh skill 同步：`ontometa-query` 新增「口径优先」路由，`ontometa-discovery` 把口径
  纳入结构探索，`ontometa-task-plan` 要求指标任务先核实 `formalized=true`。
  `tests/test_dsh_skill.py` 增加口径通道的必含标记，防止这条路由被悄悄改没。
- 验证：`tests/test_mcp_logics.py` 13 条（检索/详情/编译/失败分层/授权门控），
  全量 **2221 passed, 1 skipped**。真库上 `活跃客户分组数` 编译出
  `SELECT COUNT(*) ... WHERE is_group = 0`，口径自带过滤未丢。

---

## ✅ Data Agent parity P0/P1：生命周期与聚合

- 新增 `draft_task` / `validate_task` / `confirm_task` / `execute_task`，复用
  `AgentPipelineService` 的治理状态机、校验闸门、dry-run、回执和幂等语义。
- `execute_task` 先以条件更新原子抢占 `confirmed → executing`，再派发独立的
  `app.jobs.artifact_execution_worker`；MCP 调用立即返回，终态通过 `get_task_status` 回读。
  worker 使用独立数据库 session，派发失败会释放执行占位，避免重复投递和请求超时。
- `query_objects` 新增 `group_by=role|segment`，统计路径复用 `OntologyQueryService` 的
  完整过滤条件，不加载对象派生明细。
- 新增 `get_ontology_overview`（reader）压缩本体元信息、角色/板块分布和业务对象精简清单；
  `query_relations` 可选返回 Mermaid，`execute_sql` 可选返回 Vega-Lite 结果预览 spec。
- 外部 agent 建议使用最小权限 Principal：editor 只到 draft/validate，publisher 才能
  confirm/execute；不要把 `ONTOMETA_ADMIN_TOKEN` 放进 agent 环境或可读文件范围。
- `GovernanceArtifact` 的最终执行成功/失败仍以回执和 Airflow 对账为准；异步工具审计记录
  的是“已受理”，不是替代终态回执。
- dsh 增加项目级 `ontometa-mcp` skill：固化真实 ID 查询、大结果聚合、六环任务顺序、
  publisher 门控、异步轮询、失败分层和“结论/证据/执行进度/下一步”输出。Skill pack 现位于
  `backend/app/mcp/skills/`，dsh 通过自定义根挂载；确定性 MCP 任务推荐显式 `/ontometa-mcp`。

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

## ✅ Phase 5（部分）：远程 HTTP 传输 + 前端管理页

**动机**：agent 与本项目不在同一台机器时，stdio（本地子进程 + venv 路径）根本连不上。

### 远程 HTTP 传输（当前唯一对外服务）

- MCP 挂到主 FastAPI 的 `/mcp` 路由（`app/mcp/http_app.py` + `main.py`），Streamable HTTP
  传输，**`json_response=True` + stateless**——绕开主后端 `AdminAuthMiddleware`
  （`BaseHTTPMiddleware`）会缓冲、挂住 SSE 长连的坑。异地 agent 用「服务地址 + 令牌」连。
- **身份逐请求解析**：`server._auth_for` 从 `context.request`（HTTP transport 挂上的原始
  请求）读 `Authorization: Bearer`，走 `resolve_http_auth` → `resolve_principal_token`
  （与 REST/stdio 同一份）。绝不复用 stdio 的进程级会话身份。
- **始终要求令牌**：`_AnonymousGuardASGI` 把无令牌请求直接 401（连 initialize 都不给）。
- HTTP 服务固定启用且不允许匿名；旧的 `mcp_http_enabled` / `mcp_http_allow_anonymous` 字段仅作
  数据兼容，不再改变运行行为。
- 端点规范用 **`/mcp/`（带尾斜杠）**——Starlette Mount 对 `/mcp` 无斜杠会 307。

### 前端管理页（Agent 接入 → MCP 配置/MCP 工具/审计监控）

- `frontend/src/components/McpPanel.tsx`，四块：连接与状态 / 功能清单（工具+最低角色）/
  审计日志 / 使用统计。
- 数据走 `GET /api/mcp/{info,stats,audit}`（`app/api/mcp.py`）——info 只读（reader），
  audit/stats 需 publisher。聚合/目录逻辑集中在 `app/mcp/introspection.py`，与 MCP 工具
  `server_info`/`get_mcp_stats`/`list_audit_logs` 共用一份（不写两遍）。

### 验证

- `tests/test_mcp_http.py` 12 条：逐请求 HTTP 鉴权、匿名拦截 401、REST info/stats/audit +
  publisher 门控。全量 2182 passed。
- stdio-over-HTTP 端到端：真 MCP client 连 `/mcp/`，admin(publisher) 全放行、reader 的
  execute_sql 被 denied（**逐请求身份隔离**）、无令牌 401。
- **通用 agent × MCP 端到端**：本机 LLM（GLM-5.2，OpenAI 兼容，DB 配置）经 stdio 拿到 16 个
  工具，自主链式调用（query_ontology → query_objects；list_tasks）准确回答本体/对象/任务问题，
  无编造——MCP 改造「通用 agent + 工具 = 专用 agent 能力」的论点得证。

## 📋 Phase 5（剩余）/ Phase 6：待做

- [ ] **资源级权限**（某主体只能看某数据域/数据源）：当前工具级 + 角色级，未到行级。
      要给 Principal 关联可访问 domain/datasource 并在查询工具注入过滤——工程量不小、
      单机可信场景收益有限；远程 HTTP 暴露后其价值上升，可作下一步。
- [ ] 本体建模类（`infer_ontology_from_datahub` / `classify_business_objects` /
      `infer_relationships` / `validate_ontology`）——写侧或长耗时，要先想清 MCP 下
      「异步 + 人工确认」怎么表达。
- [x] ~~`get_lineage` / `get_landing` / `get_ops_record`~~——已做（见上）。仍未解决的是
      **远端失败原因本身读不到**：ontoMeta 的回执只记录投递，Flink 作业为什么挂要去
      Airflow 日志。要真答上这个问题，得让执行器把远端 task 日志摘要回写进回执。
- [x] ~~取数辅助：`find_join_path` / `profile_values`~~——已做（见上）。
- [ ] 资产目录 `list_datasets`（数仓落点目录，与 get_landing 互补：一个查单主体，一个列全域）。
- [ ] 治理规约类（`validate_against_policy` / `lint_task_spec` /
      `get_active_governance_standard`）。
- [ ] 远程传输的生产加固：来源校验 / TLS 终止 / 速率与并发（当前限流是进程内滑动窗口）。

---

## 📝 决策记录

### DR-1: 为什么新建模块而不是改造 Data Agent？

风险控制 + 可并行对比 + 回滚简单（删 `app/mcp/` 即可）。现有 Data Agent 未改一行。

### DR-2: 为什么 MCP 侧只给只读工具？——**已被 parity P0 推翻，保留过程**

原判断：写侧的安全边界是「人工逐环确认」，它长在前端与 REST 流水线上；把
draft/confirm/execute 暴露成 MCP 工具，等于让通用 agent 绕过确认闸门。所以提案工具
只出 `draft_payload`，落库动作留给人。

**为什么改**：真机 dsh 会话证明这条边界拦不住人——MCP 没有写侧工具，agent 就去读源码、
从 `.env` 抓 `ONTOMETA_ADMIN_TOKEN`、用 curl 直打 `/api/agents/*`，反而以 publisher
身份绕过了 MCP 的全部门控和审计。**不给工具不等于给了安全，只是把绕道逼到看不见的地方。**
现在的做法是把 draft/validate/confirm/execute 做成工具，用角色门控替代人工逐环确认：
editor 只到 draft/validate（看清将发生什么），confirm/execute 要 publisher，且每次调用
都进 `mcp_audit_logs`。「是否允许这个 agent 自动执行」的决定权，落在「给它发什么角色的
令牌」上。

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
