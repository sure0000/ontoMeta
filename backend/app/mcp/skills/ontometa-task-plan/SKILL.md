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

## 输出

使用“结论 / 任务方案 / 校验证据 / 下一步”。展示 `task_id`、状态、目标、模式、`blocking_count` 和 dry-run 摘要；不输出完整 Spec 或凭据。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
