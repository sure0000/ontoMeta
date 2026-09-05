---
name: ontometa-flow
description: ontoMeta 交互式建数流程：用户想建同步/加工/物化/指标任务但参数没给全时，用 start_task_flow 与 advance_task_flow 一次问一个问题、逐环确认，直到拿到可以直接 propose 的参数。
whenToUse: Use when the user wants to create a sync, transform, materialize, or metric task but has not specified which ontology, object, datasource, load mode, or schedule to use, and you need to ask them step by step.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 交互式建数流程

## 工作目标

把一句话需求（「把客户主数据同步进数仓」）变成**用户逐项点过头**的任务参数。
你不需要自己想「该问哪几项」——`start_task_flow` 给的问题和候选，与 Web 表单里那张
六环确认表单是同一份。这个 skill 的出口是一次 `propose_*` 调用的参数，不触发任何写操作。

## 工具顺序

1. `start_task_flow(goal=用户原话)`：能确定的类型/本体一起给（`kind`、`answers`），给了就不会再问。
2. 只有 `status="ask"` 时才问用户——那是**系统定不下来**的参数（没有默认值、也没有唯一候选）。
   把 `form.fields` 一次做成一张表单：
   - 宿主有 `ask_user_question`（dsh）/ `AskUserQuestion`（Claude Code），一次调用带上全部格子；
   - 宿主没有、但用户能打开 ontoMeta 控制台：`open_task_form` 换一个链接，
     `wait_task_form(form_id)` 等回填（服务端最长等 50 秒，超时如实说，别自己轮询）；
   - 都没有就退回编号清单（见出口契约的「## 选择」）。
3. `advance_task_flow(kind, answers)` 提交答案，**之前的答案原样带上**（流程不存服务端状态）。
   `start_task_flow` 会把原始 `goal` 写入返回的 `answers.task_requirement`，即使下一环只是在选本体，也要把这项一起带回。
4. `status="review"` 是**执行审查**——整条流程唯一一次人工确认：
   - 先把 `review.plan`（这次真会执行的方案：来源、落点、装载方式、调度、引擎）和
     `review.notes`（全量会重写目标表、没配调度只手动跑一次之类）摆给用户；
   - 有 `review.blocking_issues` 就先说清楚，那是执行不了的原因，不要粉饰；
   - 他要改哪一项就改 `answers` 里对应的键再调一次，方案会重算；
   - 他确认后，把 `answers["__confirm_plan"]` 设成 `form.submit_value`（本次方案的 digest，
     原样抄）。**写 `"yes"` 不算数**——那是为了让"确认过的方案"和"执行的方案"必须是同一份。
5. `status="ready"` 时照抄 `next_call` 调 `draft_task` 落草稿，
   之后按 `ontometa-task-execute` 走 `validate_task` → `confirm_task` → `execute_task`。

候选太多时用 `search` 关键词筛（`advance_task_flow(..., search="客户")`）。

## 准确性规则

- **不该问的别问**。有默认值、唯一候选、可选项一律由系统填好，摆进执行审查里一次核对；
  只有 `status="ask"` 列出来的才需要用户拍板。别把审查里的每一格再逐个问一遍。
- **审查不是复述**。`review.plan` 是 Drafter 派生的那份 Spec（落点表名、幂等策略、分区键
  都由它定），不是把用户填的值再念一遍——要审的正是"我填的东西到了执行期会变成什么"。
- **确认绑定方案**。`__confirm_plan` 必须是那次审查返回的 digest；参数改过之后旧 digest
  自动失效（会带 `stale_confirmation` 回来），这时要让用户重新看一遍再确认。
- **`ready` 不等于办成了**。`next_call` 只是落草稿；校验、确认、执行各有各的闸门，
  不要在同一条回答里宣布任务已经建好或已经跑完。
- **候选是闭集**。填错的格子会带着 `error` 回来，把错误原话给用户看并让他重选，
  不要自己挑一个"最像的"。id 只用工具返回的真实值。
- **`status="blocked"` 是事实**：缺本体、缺数据源、Drafter 拒绝（比如这个对象没有物理源表）
  都会走到这里，如实告诉用户去补，别换个参数硬凑一个能过的提案。
- 用户中途换本体或换任务类型时，把受影响的答案从 `answers` 里删掉再调一次，
  而不是在旧答案上打补丁——本体一换，对象和数据源候选全变。

{{OUTPUT_CONTRACT}}

## 输出补充（交互流程）

- `status="ask"` 时这一轮的产出就是**一张表单**：优先用宿主交互工具渲染，没有才退回
  「## 选择」清单；两种都不写 `结论/结果/依据`。
- `status="review"` 时状态写 `待确认`，`结果` 用一张表摆 `review.plan`（业务名在前，
  不摆 id），`限制` 写 `review.notes` 与阻断项，`下一步` 只写「确认后落草稿」。
- `status="ready"` 时状态写 `进行中`：方案已确认、草稿还没落，别写成完成。
- `status="blocked"` 时状态写 `受阻`，`限制` 写清缺什么、去哪补。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
