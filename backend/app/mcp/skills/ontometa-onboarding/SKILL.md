---
name: ontometa-onboarding
description: ontoMeta 接数据：查接数目录（DataHub 状态、数据域、已登记数据源），登记新的源库连接骨架，为某个数据域启动本体草稿生成，并把"哪些步骤只能由人完成"说清楚。
whenToUse: Use when the user wants to connect a new database or data source to ontoMeta, register a datasource, check which domains and sources already exist, or generate an ontology draft for a domain.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 接数据

## 工作目标

把一个新库接进 ontoMeta，直到该数据域有一份可复核的本体草稿。这条路上有几步**只能由人完成**，把它们说清楚比替用户做完更有价值。

## 这条链上谁做什么

| 步骤 | 谁做 | 说明 |
|---|---|---|
| 元数据采集 | **DataHub 侧** | ontoMeta 只读取已采集的域与表。库还没被 DataHub 采集时，本系统触发不了采集，只能告诉用户去 DataHub 配 |
| 登记源库连接 | 你建骨架 + **人填凭据** | `create_datasource` 只写名称/类型/catalog |
| 生成本体草稿 | `start_ontology_draft` | 异步跑 LLM |
| 复核与发布 | **人** | 草稿里的对象/关系要人在工作区确认后才发布 |

## 工具顺序

1. **先调 `list_onboarding_targets`**。它一次给全：DataHub 配没配、有哪些数据域（各自草稿/发布状态与对象数）、已登记哪些数据源。没有它，域 id 和源名只能靠编。
2. 要新登记源库时先在上一步的 `data_sources` 里查重，再 `create_datasource`。
   - **凭据不由你提供**：主机、库、用户名、口令由人在 ontoMeta 设置页 → 数据源里填。入参里写了 `dsn`/`password`/`username` 会被丢弃并在 `dropped_args` 里如实回报——看到它就说明你不该那样传。
   - 建出来的源 `connection_configured=false`，在有人填完连接信息之前**不能**拿去建同步任务。必须把「请去设置页填连接信息并测试连接」这句话交给用户。
3. 该域已被 DataHub 采集、需要建模时用 `start_ontology_draft`：
   - `domain_id` 必须来自第 1 步，**不得自己编**。
   - `scope`：`draft`=对象+关系全量（首次用它）、`objects`=只补业务对象、`relations`=只补业务关系（需已有含对象的草稿）。
   - 该域**已有发布本体**时工具会拒绝并要求 `acknowledge_republish=true`：那不是一个可以顺手补上的参数。先把「重跑会产生新草稿并进入合并流程，不是原地覆盖；复核标记会被重新灌满、部分发布的门闸会被打回」告诉用户，得到同意再重试。
4. 生成是**异步**的。拿到 `task_id` 后用 `get_ops_record(family="draft_run")` 回读进度，不要反复调 `start_ontology_draft`——那会排队起第二次生成。
5. 草稿跑完后用 `ontometa-discovery` 的工具看结果；发布仍是人在工作区做的事。

## 准确性规则

- `datahub_configured=false` 就是没配 DataHub：如实说，别把「本系统读不到表」写成「这个库是空的」。
- `connection_configured=false` 的数据源不是「已接入」，只是占了个名字。介绍进度时要把这一位说出来。
- `published_object_count` 是**已发布本体**的对象数；草稿里的对象不在其中，两个口径不能混说。
- 一次草稿生成要烧掉可观的 token 并会把复核队列重新灌满。用户只是想「看看有什么表」时，用 `list_onboarding_targets` 与 discovery 的工具回答，不要顺手起一次生成。
- 工具返回 `accepted=true` 只表示**已受理**，不代表草稿已经生成好。终态从 `get_ops_record` 读。

{{OUTPUT_CONTRACT}}

## 输出补充（接数据）

- `结论` 说清这次推进到哪一步，以及**下一步该谁做**（在 DataHub 配采集 / 去设置页填凭据 / 等生成 / 去复核）。
- `结果` 推荐列：域或源名称 / 类型 / 当前状态 / 是否可用。
- 「只能由人完成」的步骤写进 `下一步`，用祈使句写清在哪个页面做什么，不要含糊成「需要进一步配置」。
- 已受理但未完成的生成写 `进行中`，并给出回读进度的方式；不要写成 `完成`。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
