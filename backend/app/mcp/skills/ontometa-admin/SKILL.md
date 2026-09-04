---
name: ontometa-admin
description: ontoMeta MCP 服务自省与审计：回读当前会话身份与工具权限、按工具/角色查调用审计留痕、看使用与限流统计，用于排查“我能不能调”“谁动过什么”“是不是被限流了”。
whenToUse: Use when the user asks about the MCP service itself - what identity or role this session has, which tools are available and at what minimum role, why a call was denied or rate limited, who called which tool and when, or usage statistics from the audit log.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta MCP 自省与审计

## 工作目标

回答关于**这个 MCP 服务自身**的问题：我是谁、我能调什么、某次调用为什么被拒、谁在什么时候调过什么。只读，不碰本体、不碰数据、不碰任务。

业务问题不走这里：本体结构问 `ontometa-discovery`，取数问 `ontometa-query`，任务问 `ontometa-task-plan` / `ontometa-task-execute`。

## 工具顺序

1. `server_info`（reader）：版本、传输方式、**工具清单与各自最低角色**、当前会话身份、限流配置、审计表可达性。
   排查“我能不能调 X”只需要这一个——先看自己的 `role`，再看 X 的 `required_role`。
2. `list_audit_logs`（publisher）：谁、什么身份、调了哪个工具、成没成、是否被授权拦下。按时间倒序，可按工具名、成败过滤。
3. `get_mcp_stats`（publisher）：总调用量、成功/失败/被拒/被限流数，按工具与角色分组。

## 准确性规则

- **三种“没成功”不是一回事**，报告时必须分开：`denied` 是角色不够（服务器在调用前拦下）、
  `rate_limited` 是触发限流（等 `retry_after_seconds` 再来）、`success=false` 才是工具自己执行失败。
- 当前会话角色不够时，如实说“需要 X 角色，当前是 Y”，并说明这是**发令牌时的人为决定**——
  不要建议换 token、读 `.env`、找 Admin Token，也不要绕道 REST。
- `list_audit_logs` / `get_mcp_stats` 本身要 publisher。被拒就说被拒，不要用 `server_info` 的
  片段拼一个“审计摘要”冒充。
- 审计是 append-only 的留痕，入参已脱敏：不要期待里面有完整参数或凭据，也不要据此推断参数内容。
- 统计口径来自审计表。审计表不可达时（`server_info` 会说）统计就是不可信的，如实说明而不是给一个数。

## 输出

使用“结论 / 证据 / 下一步”。身份类问题一句话给出角色与关键工具的门槛；审计类问题给紧凑表格（时间、工具、身份、结果），不倾倒完整日志、不输出令牌或连接串。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。
