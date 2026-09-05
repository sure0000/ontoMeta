---
name: ontometa-task-plan
description: ontoMeta 任务规划：为 sync/transform/materialize/metric 生成真实提案，使用 draft_payload 落治理草稿并校验，但不确认、不执行数仓变更。
whenToUse: Use when the user wants to design, preview, draft, validate, or review a data synchronization, transformation, materialization, or metric task without executing it.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 任务规划

## 工作目标

把用户意图变成可审查的治理任务草稿。这个 skill 的出口是 `validated` 或带阻断项的 `drafted`，不触发数仓副作用。

## 六环范围

本 skill 负责：需求 → 本体 → 数据 → 执行方案 → 草稿 → 校验。

0. **参数没给全就别猜**：用户没说清本体、对象、数据源、装载方式或调度时，先走 `ontometa-flow`
   （`start_task_flow` → `advance_task_flow`）把这些逐环问出来，拿到 `ready` 再回到这里。
1. 明确 `kind`：`sync`、`transform`、`materialize` 或 `metric`。
2. 用 `query_ontology`、`get_ontology_overview`、`query_objects`、`query_object_detail` 和 `list_datasources` 获取真实上下文。
   `metric` 任务另需真实的 `business_logic_id`：用 `search_logics` 找、`get_logic` 核实，确认 `status=published` 且 `formalized=true`——只有文字口径的那条建不出指标任务。
3. 先调用对应的提案工具：`propose_sync`（源库表 → 数仓 ODS）、`propose_transform`（ODS → 加工结果表）、
   `propose_materialize`（本体对象 → 物理表，只出建表 DDL）、`propose_metric`（已发布口径 → ADS 结果表）。
   提案只读、不写库；检查 `missing`、候选项、Spec 和 `validation.blocking_count`。
   四类任务都必须给 `target_datasource_id`——缺了它，任务要么起草就失败，要么“成功”却只渲染配置不搬数。
4. 只有提案符合目标时，才把 `draft_payload` 原样传给 `draft_task`；不要手写 Spec、ODS 落点、引擎或表名。
5. 调用 `validate_task` 重跑校验，确认 `status=validated` 且 `validation.blocking_count=0`。

## 停止条件

- 缺少必要上下文：报告 `missing` 和真实候选，不猜 ID。
- 有校验阻断项：停止在规划阶段，报告 issue 和修复方向。
- `draft_task` 已写入但校验异常：保留并报告 `task_id`，不要假装未创建。
- 不调用 `confirm_task`、`execute_task`，除非用户明确切换到执行阶段并改用 `ontometa-task-execute`。

{{OUTPUT_CONTRACT}}

## 输出补充（任务规划）

- `结论` 说明规划到哪一步、能否进入确认：`validated` 且零阻断用 `待确认`；
  存在阻断项用 `受阻`；只生成提案尚未落草稿用 `进行中`。
- `结果` 只给业务可读的任务类型、来源、目标、模式和 dry-run 摘要；
  禁止倾倒 `draft_payload` 或完整 Spec。
- 阻断项每行写「问题 / 影响 / 修复动作」，最多 10 行，不要粘内部堆栈。
- 失败或受阻时必须明确说清**有没有已经创建任务**，不要含糊写成"未完成"。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
