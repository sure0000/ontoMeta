# dsh ontoMeta Skill

dsh 通过后端包内 `backend/app/mcp/skills/` 提供按用户目标拆分的 ontoMeta MCP skills；MCP
客户端也可通过 `list_prompts` / `get_prompt` 从服务端获取同一份指引，不再依赖仓库根目录。

**为什么 skill 是必需的而不是锦上添花**：MCP 只提供工具，「什么时候用哪个、结果怎么解读、
什么话不能说」全在这几份 SKILL.md 里。少一份指引，模型就会在那一块自由发挥——而这个领域里
最危险的错误恰恰不报错（照口径文字重写 SQL、按字段名猜 JOIN、按命名规则拼表名）。

## Skill 划分

| Skill | 适用目标 | 写侧边界 |
|---|---|---|
| `ontometa-output` | **出口契约总控**：所有回答的格式、状态口径、截断与 ID 规则、需要用户选择时怎么提问 | 不调工具 |
| `ontometa-flow` | 用户想建任务但参数没给全：一问一答、逐环确认，直到拿到可提案的参数 | 只出提案参数，不写库 |
| `ontometa-discovery` | 本体、对象、关系、业务口径、血缘上下游、物理落点、数据源、角色/板块分布 | 只读 |
| `ontometa-query` | 指标口径编译取数、关联路径与字段画像、SQL 校验执行、Vega-Lite 结果预览 | 只读，取数需满足 `agent_run_sql_min_role` |
| `ontometa-task-plan` | 四类任务提案、落草稿、校验 | 最多到 `validated`；editor 即可（无副作用） |
| `ontometa-task-execute` | 确认、异步执行、终态轮询、运行记录追溯 | 写侧需 publisher；运行追溯只读 |
| `ontometa-admin` | 会话身份与工具权限自查、调用审计、使用与限流统计 | 只读；审计/统计需 publisher |
| `ontometa-mcp` | 总入口：按目标路由 + 声明共同底线与输出契约 | 不作为模型自动路由 skill |

总入口是 `disable-model-invocation: true`，模型自动路由时**不会**加载它——所以专用
skill 各自自带完整的工具顺序、准确性规则、输出契约和安全底线，而不是靠总入口兜底。

**出口契约只有一份**：`ontometa-output` 是唯一的出口规定，其余 skill 正文里写
`{{OUTPUT_CONTRACT}}` 占位符，下发（MCP prompt、`get_playbook`、导出 ZIP、安装到目录，
全是同一条正文）时替换成它的正文。所以每份 skill 单独加载也带着完整契约，而改格式、
改状态口径、改"需要用户选择时怎么问"只需要改一处。技能页会标出哪份是总控、哪份把契约
固化在了自己正文里（不再跟随更新）。
`backend/tests/test_dsh_skill.py` 把这件事钉死：每份 skill 的关键指引在自己身上，且
**注册表里每个 MCP 工具都必须在某份 skill 里被提到**——加了工具不写指引，测试直接失败。

## 三条会安静出错的红线

所有 skill 共同遵守。这几条错了不会报错，只会给出一个看起来合理的错答案：

1. **口径是权威**：问已有指标/标签/规则一律 `search_logics` → `compile_metric` → `execute_sql`，
   绝不照着口径文字自己重写 SQL。
2. **连接键和字面量是查出来的**：跨对象先 `find_join_path`，WHERE 带字面量先 `profile_values`。
3. **运行事实只从记录读**：`get_landing` / `get_ops_record` / `get_task_status` 说什么就是什么；
   血缘只认 `is_derivation=true` 的边，外键不是「数据从这里来」。

## 参数没给全时：一问一答，不要替用户猜

Web 里的 Data Agent 有 `request_form` 弹表单；dsh 这类纯文本客户端没有这个出口，于是模型
要么一次问八个参数，要么自己挑一个 id 往下走（挑错也不报错，执行时才炸）。

`ontometa-flow` 把同一张六环表单拆成一次一个问题：

```text
start_task_flow(goal="把客户主数据同步进数仓")
  → status=ask，返回候选（任务类型 / 本体 / 对象 / 源库 / 装载方式 …）
  → 摆出编号候选，停下等用户回答
advance_task_flow(kind, answers)   # answers 要累计带上（含 start 返回的 task_requirement），服务端不存状态
  → 前三环逐环确认（需求 / 本体 / 数据），系统预填的项标成 auto 让人重点核对
  → status=ready 时给出可以照抄的 propose_* 参数
```

用户回序号、中文名或 id 都行，服务端会对回真实候选；对不上就再问一遍，绝不含糊落一个假值。
用户要改已经定下的项：把 `answers["__confirm_<环>"]` 设成 `"field:<字段名>"`，那一项会被真正重问。

## 使用方式

在 dsh 中显式输入：

```text
/ontometa-discovery 查询 erpnext 本体概览
```

模型也可以按 `whenToUse` 自动选择专用 skill，但 dsh 的路由是模型决策而非硬性分类器。
要求确定执行路径时，显式用 `/ontometa-mcp` 走总入口再分流；普通非 ontoMeta 任务不会加载它们。

共同的行为约定：先用真实 MCP 工具和 ID 建立上下文；大结果先聚合（`group_by`、概览）再分页；
写侧按 `propose_* → draft_task → validate_task → confirm_task → execute_task → wait_task_status`
推进；把工具受理、权限拒绝（`denied`）、限流（`rate_limited`）、校验阻断和远端执行失败分开报告；
默认输出「结论 / 证据 / 执行进度 / 下一步」，不倾倒完整 JSON 或凭据。

dsh 的执行确认分两种：默认由任务详情的逐条「允许 Agent 代执行」开关放行；管理员显式开启「本机宿主交互确认」后，
dsh 先用宿主 `ask_user_question` 展示任务方案，用户选择批准，再把 `get_task_status` 返回的
`interactive_approval.digest` 作为 `host_confirmation` 同时传给 `confirm_task` 和 `execute_task`。digest
绑定任务 ID、Spec 和校验报告，任务内容变化会使确认失效；远程 HTTP 不接受这条旁路。

## 推荐的数据任务闭环

1. **规划与执行分轮**：第一轮用 `ontometa-task-plan` 到 `validated` 为止；第二轮重新
   `get_task_status`，只在零阻断时展示方案并请求批准。不要一句话从模糊意图直接跑到 Airflow。
2. **dsh Web 承担交互执行**：用原生 `ask_user_question` 展示任务名、源表、目标表、模式、
   `blocking_count`、非阻断风险和 digest；人选择批准后才传 `host_confirmation`。dsh headless
   没有人类 answerer，只适合查询、规划和后续状态监控，不应伪装成已交互确认。
3. **执行后用长轮询**：`execute_task` 的 `accepted=true` 只表示受理，随后调用
   `wait_task_status(timeout_seconds=50, poll_interval_seconds=5)`。超时是“仍在运行”，不是失败；
   下一轮继续等待，不要用 Bash/sleep，也不要密集重复 `get_task_status`。
4. **授权按接入面分开**：本机可信 dsh stdio 可显式启用宿主交互确认；远程 HTTP 继续使用任务详情
   的逐条授权开关。两条都必须是 publisher Principal，不能用匿名默认角色或 Admin bootstrap token。
5. **终态只认执行事实**：只有 `succeeded` 且 Airflow/Doris 对账通过才报告完成；`failed` 只报告
   工具可见的阶段、回执和 `run_url`，没有远端原因就不猜，也不自动创建重复任务。

2026-09-05 真机验收覆盖了两条 ERPNext company 同步：headless 在关闭逐条闸门的隔离验证中能完成
confirm/execute，但因无 sleep 退化为高频轮询；改用上述 Web 交互 + digest + `wait_task_status` 后，
任务从 `validated` 经 `stdio_host_interactive` 确认、异步受理，最终回读到 Airflow `success` 与
Doris 验证通过。该路径不读取 `.env`、不走 REST、不重复提交。

## 启用配置

`/Users/me/.dsh/profiles/web/cordis.patch.yml` 已启用 `skill-filesystem` 和 `tool-skill`，
并挂载 `/Users/me/Documents/ontoMeta/backend/app/mcp/skills` 为自定义 skill 根。修改 skill
正文后 dsh watcher 会刷新目录；修改 profile 配置后需重启 dsh。

直接挂载仓库目录拿到的是**未合成**的原文（正文里还留着 `{{OUTPUT_CONTRACT}}` 占位符），
也吃不到技能页上的部署级覆写。要拿到与 MCP 下发一致的那份，用「Agent 接入 → 技能 →
部署 Skill」填一个目录点「安装到目录」（后端主机上的绝对路径，服务端写盘，先给预检计划
再写；只写 `<skill-name>/SKILL.md`，目录里其它文件不动），或用 `POST /api/mcp/skills/install`
（publisher）。旧的下载 ZIP 再解压那条路仍在。
