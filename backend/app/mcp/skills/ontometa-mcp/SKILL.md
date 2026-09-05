---
name: ontometa-mcp
description: ontoMeta MCP 总入口：按用户目标路由到本体探索、取数、任务规划、任务执行与运行追溯、服务自省五个专用 skill，并声明所有 skill 共同遵守的底线与输出契约。
whenToUse: Use when the user asks about ontoMeta ontologies, business objects, relations, data sources, synchronization, transformation, materialization, metrics, governance tasks, or MCP execution.
disable-model-invocation: true
user-invocable: true
---

# ontoMeta MCP 总入口

这是**路由入口**，不重复各专用 skill 的工具顺序。收到 `/ontometa-mcp` 后先判断用户目标，再加载并遵守对应的一个 skill：

| 用户目标 | skill | 典型问法 |
|---|---|---|
| 结构探索（只读） | `ontometa-discovery` | 有哪些本体/对象/关系/指标口径、如何分布、数据从哪来、落到哪张表 |
| 取数与算指标 | `ontometa-query` | 查明细、算某个已有指标、比较数值、验证 SQL、出图 |
| 参数没给全、要一步步问用户 | `ontometa-flow` | 我想同步/建指标，但没说清哪个对象、哪个源、什么频率 |
| 任务规划（到 `validated` 为止） | `ontometa-task-plan` | 我想同步/加工/物化/建指标，先看看方案 |
| 任务执行与运行追溯 | `ontometa-task-execute` | 执行它、跑到哪了、为什么失败、之前发生过什么 |
| 服务自省与审计 | `ontometa-admin` | 我是什么身份、为什么被拒、谁调过什么、有没有被限流 |

一次只走一个阶段，不要把探索、取数、写侧混在一次回答里。每个专用 skill 自带完整的工具顺序、准确性规则和输出契约——**以那份为准**。

所有回答的格式、状态口径、截断规则和「需要用户选择时怎么问」由 `ontometa-output` 一份说了算，已经内联在每份 skill 末尾（下面这段就是它），要改规则去改那一份。

**如果你的客户端加载不了 skill**（很多 MCP 客户端只桥接 tools，不消费 prompts），用 `get_playbook` 工具取回同一份正文：不带参数看主题清单，`get_playbook(topic="ontometa-query")` 取正文。表里的 skill 名就是 topic 名。

## 三条会安静出错的红线

这几条错了不会报错，只会给出一个看起来合理的错答案，所有 skill 共同遵守：

1. **口径是权威**。问已有指标/标签/规则一律 `search_logics` → `compile_metric` → `execute_sql`，绝不照着口径文字自己重写 SQL——重写出来的数与其它系统对不上。
2. **连接键和字面量是查出来的，不是想出来的**。跨对象先 `find_join_path`，WHERE 带字面量先 `profile_values`。语法合法但连错键、猜错值的 SQL 会安静地返回错数。
3. **运行事实只从记录读**。`get_landing` / `get_ops_record` / `get_task_status` 说什么就是什么；读不到就说读不到，不要用命名规则、印象或推理补一个「应该是这样」。血缘只认 `is_derivation=true` 的边，外键不是「数据从这里来」。

## 通用底线

- MCP 是 ontoMeta 能力的唯一入口。不要用 Bash、Read、curl、HTTP API、`.env` 或猜数据库连接绕过它。
- 凭据、token、密码、DSN、主机细节不进入回答、工具参数或报告。ID 只用 MCP 返回的真实值。
- 本体是建模权威，物理表是投影。不要把任务产物当成本体对象，也不要因为一个推断字段就声称事实已确认。
- 把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败**分开**报告，不要把“工具调用成功”写成“事情办成了”。
- 大结果先聚合（`group_by`、概览）再分页取明细；不要为了统计把上千条对象倾倒进回答或落盘处理。
- 写侧的顺序是固定的：`propose_*` → `draft_task` → `validate_task` → `confirm_task` → `execute_task` → `wait_task_status`。前两步 editor 即可（无副作用），confirm/execute 需 publisher；**发什么角色的令牌就是“允不允许这个 agent 自动执行”的人为决定**，不要试图绕过它。
- 幂等：`succeeded` / `executing` 的任务不得重复提交；远端失败先读回执与 `run_url`，除非用户明确批准否则不新建重复任务。

{{OUTPUT_CONTRACT}}
