# Data Agent V7：跨轮 Run / Artifact 持久化

> 状态：✅ 已落地并完成本地回归
> 日期：2026-08-29
> 承接：[Data Agent V6 运行记录可问答](./DATA_AGENT_V6_OPERATIONAL_RECALL_PLAN.md)

## 1. 目标

页面刷新、服务进程重启或客户端不再携带 `history` 后，Data Agent 仍能恢复上一轮的：

- 已验证 SQL；
- 查询结果的列、可见行数与截断状态；
- 权威运行记录及其 `source/as_of/observed_at`；
- 已引用的本体对象、业务口径；
- 任务状态、提案与血缘等结构化输出索引。

恢复历史只用于延续上下文。运行状态、物理落点等动态事实仍必须重新调用 V6 权威 reader，
不能把历史快照冒充当前状态。

## 2. 存储模型

不新增运行表。一个 assistant message 就是一轮持久 Agent run：

- `ChatBiMessage.id` 同时作为稳定 `run_id`；
- `ChatBiMessage.content` 保存用户已经看到的回答；
- `ChatBiMessage.payload.agent_run` 保存状态、问题、intent、skill、接地状态和起止时间；
- `ChatBiMessage.payload.agent_artifacts` 保存本轮结构化输出的安全索引；
- `GovernanceArtifact` 仍是可执行任务制品的唯一权威，run 只引用、不复制其状态机。

run 状态统一为：`succeeded`、`refused`、`waiting_input`、`failed`、`cancelled`。
同步与 SSE 两条入口使用同一信封；失败和客户端取消也会落一条 run，避免刷新后只剩孤立的用户问题。

## 3. 跨轮恢复

续聊时服务端从会话消息加载权威历史，不再采用客户端提交的临时 `history`。旧消息仍按原正文进入
compaction；新 run 额外附带最多 4000 字符的 artifact 安全投影：

- SQL 可完整回带，支持“沿刚才口径继续”；
- 数据结果只回带列名、可见行数、截断状态，不回带行值；
- 运行记录回带 reader 信封，并明确要求动态事实重新核实；
- API Key、token、password、secret、DSN 等字段递归脱敏。

`agent_result_store` 的全量查询结果仍保持 run-local，没有改成数据库持久化。这避免大结果、敏感行值
和临时句柄进入长期存储；消息 payload 原本已经展示给用户的结果仍按既有行为保存。

## 4. 查询接口

- `GET /api/chat-bi/conversations/{conversation_id}/runs?limit=20`
- `GET /api/chat-bi/conversations/{conversation_id}/runs/{run_id}`

前端 API 客户端已补齐对应类型和调用方法。旧消息没有 `agent_run` 时保持兼容，不会伪造历史 run。

## 5. 验收

- 成功、失败和 SSE run 均可在消息落库后查询；
- `run_id == assistant message id`；
- 服务端持久历史优先，客户端伪造 history 不进入续聊上下文；
- artifact manifest 不包含结果行或测试凭据；
- 既有会话域绑定、问答 golden、SQL/RBAC、六环任务流程不回归；
- 本地后端：`1965 passed, 3 skipped`；
- 前端：`tsc -b && vite build` 通过，仅保留既有 chunk 体积告警。

