---
name: ontometa-task-execute
description: ontoMeta 任务执行与运行追溯：在校验通过且用户明确授权后确认治理任务、异步触发执行、轮询到 Airflow/Doris 终态，并用运行记录回读任务、任务链、组件和数据源的既有事实。
whenToUse: Use when the user explicitly authorizes confirmation or execution of an already validated ontoMeta task, asks to track an executing task to its terminal state, or asks what already happened - task run history, why a task failed, pipeline progress, component deployment, datasource probe results.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 任务执行

## 工作目标

推进一个已有 `task_id` 的已校验任务，并把真实终态回读给用户；以及回读**已经发生过**的运行记录。该 skill 不负责重新设计任务，也不创建重复任务。

## 前置检查

1. 调用 `get_task_status`，确认任务存在、`status=validated`、`validation.blocking_count=0`。
2. 如果不是 `validated`，停止并报告当前状态；不要绕过状态机。
3. 只有用户明确授权执行时，才调用 `confirm_task`；publisher 角色是服务端最终门控。

## 执行顺序

1. `confirm_task(task_id)`：记录真实确认人，确认结果必须是 `confirmed`。
2. `execute_task(task_id)`：只接受 `accepted=true` / `status=executing` 作为“已受理”，不称为成功。
3. 用 `wait_task_status(task_id)` 服务端长轮询到 `succeeded` 或 `failed`；返回 `timed_out=true` 时稍后再次等待，
   不用 Bash/sleep，不用高频重复 `get_task_status`。长时间无变化时最多用一次 `list_tasks` 交叉核对，不重复触发执行。
4. 终态以 Airflow/Doris 对账结果为准。只有终态 `succeeded` 才能说执行成功；`failed` 必须说明失败阶段和 `run_url`。

启用本机宿主交互确认时，步骤 1 前必须先用 dsh 原生 `ask_user_question` 展示方案并得到批准；
把 `get_task_status` 返回的 `interactive_approval.digest` 组成 `host_confirmation`，同时传给步骤 1 和步骤 2。
没有宿主交互能力时不要自行填 `approved=true`，改走任务详情逐条授权或停止在 `待确认`。

## 运行追溯（问“已经发生了什么”时走这条）

用 `get_ops_record` 按族读权威记录，不要凭印象复述：

- `task_run` 任务跑完没有、失败没有；`pipeline` 整条任务链卡在哪一步；
- `component` 依赖组件（airflow/datahub/llm）部署与连通状态；`datasource` 数据源上次拨测结果；
- `ontology_version` / `draft_run` / `merge_report` / `conflict` / `standard` / `data_app` / `migration` 各自的历史。
- 按本体组织的族要传 `ontology_id`；`decision` 族按会话组织，MCP 无会话，读不到也不该读。

三条准确性底线：

1. `as_of` 是记录自身的权威时点（上次成功搬数 / 执行完成），`observed_at` 是这次读取的时刻。不要把后者说成前者，更不能用读取时间兜底把“从没跑成功过”说成“刚刚还好的”。
2. `metadata.failed_without_reason` 非空时，说明失败发生在远端 Airflow/Flink，投递回执自陈的是“投递成功”——**这里给不出原因**。改用 `get_task_status` 拿 `run_url` 指向远端日志，不要推测失败原因。
3. `note` 是服务端给的空结果说明，可直接引用；`empty=true` 就如实说没有记录，不要拿别的族的数据凑。

## 幂等与安全

- `succeeded` 或 `executing` 重复请求不得再次提交。
- `denied` 是权限结果；`rate_limited` 是限流结果；远端 Airflow 失败不是 MCP 调用失败。
- 失败后先报告回执和日志入口，不自动新建或重跑任务。
- 不读取 Admin Token、`.env`、Airflow 凭据或内部日志中的密码。

{{OUTPUT_CONTRACT}}

## 输出补充（任务执行与追溯）

- 状态映射是硬的：`accepted=true` 只能写 `进行中`；终态 `succeeded` 才是 `完成`；
  终态 `failed` 是 `失败`；代执行授权未放行是 `待确认`；权限或限流是 `受阻`。
- `结果` 按业务阶段展示（校验 / 确认 / 执行），不逐条复述工具调用。
- 运行追溯必须在 `依据` 给出记录族、`as_of` 与 `observed_at`；时间统一用同一时区的
  `YYYY-MM-DD HH:mm:ss`。
- 未到终态时必须保留 `限制`，写明"尚不能判定成功"；`failed_without_reason` 时只给
  `run_url`，禁止猜测失败原因。

## 两条容易踩的默认值

- `list_tasks` **默认不对账 Airflow**（快得多），返回的 `status` 是制品自身记录、可能落后于远端；
  要终态就对**单条**用 `get_task_status`，别为了拿终态给整批传 `reconcile=true`。
- `confirm_task` / `execute_task` 除角色外还有一道**代执行授权闸**（逐条由人在任务详情里放行）。
  被它拦下时 `metadata.gate=agent_execution_approval`——这不是权限不足，换令牌没用，
  如实告诉用户去哪里放行，不要重试。
- dsh Web 若管理员显式开启“本机宿主交互确认”，先用宿主的 `ask_user_question` 展示任务方案并取得用户批准，
  再把 `get_task_status` 返回的 `interactive_approval.digest` 作为 `host_confirmation` 同时传给
  `confirm_task` 与 `execute_task`。digest 不一致表示方案已变，必须重新展示确认；远程 HTTP、匿名和 Admin bootstrap token 不可使用。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
