---
name: ontometa-modeling
description: ontoMeta 建模工单：为一次完整的分析/报表需求开工单，逐份写入并确认需求、上下文、维度模型规格（每份都版本化、带乐观锁），并设计星型/雪花维度模型。
whenToUse: Use when the user wants a complete analysis, report, or data application that needs requirements clarified and tracked over multiple turns, or wants to design a dimensional model (fact and dimension tables) on top of a confirmed ontology.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 建模工单

## 工作目标

把一次「我要做个分析」变成一串**可确认、可版本化**的规格：需求 → 上下文 → 维度模型 → 口径包 → 交付。每一步定稿了才走下一步。

**一次性问数不要开工单**——那用 `ontometa-query` 就够了。工单是给「需要跨多轮澄清、之后还要回来接着做」的需求准备的。

## 工具顺序

1. `create_modeling_case` 开单（editor）。标题一句话说清要做什么。
2. `save_modeling_spec(kind="requirement")` 逐步补全需求（editor，默认在最新版本上合并）：
   - 字段名是固定的：`business_goal`（必填）、`business_processes`、`subjects`、`questions`、`time_scope`、`grain_expectation`、`metrics`、`tags`、`refresh_sla`、`delivery`、`acceptance_criteria`、`open_questions`。
   - **不要自造字段名**：payload 按 kind 强类型校验（`extra: forbid`），写错的字段会被当场拒绝，而不是默默存下来。
   - 每次保存返回 `revision` 与 `content_hash`，记下来。
3. 关键信息明确、且用户表示确认后，`confirm_modeling_spec`（reviewer）：
   - 必须带上第 2 步返回的 `revision` 与 `content_hash`。那是**乐观锁**：保证被确认的就是被审查过的那一版；中途有人改过，确认会失败而不是悄悄确认了新内容。
   - 确认后阶段推进，规格锁定，后续修改要开新版本。
4. `get_modeling_case` 随时回读当前阶段与各类规格的最新版本/确认状态；不给 `case_id` 就列最近若干张工单。
5. 本体与数据都定下来之后，`create_dimensional_model`（editor）设计维度模型：
   - **先定粒度**：`grain` 要一句话说清事实表一行代表什么。粒度说不清的维度模型，事实表迟早重建。
   - 事实表带度量与维度键，维度表带代理键、自然键、属性与 SCD 策略。
   - 建完自动跑校验：`validation.has_errors=true` 时工具报 `success=false`，那是**模型不能用**，不是网络抖动，照 issues 改完重建。

## 准确性规则

- 需求没澄清就往下走是这条链上最贵的错。业务目标、主体对象、粒度三样缺一样，就停下来问，不要"先建个模型看看"。
- 写入规格 ≠ 定稿。`save_modeling_spec` 之后规格是 `draft`，只有 `confirm_modeling_spec` 才推进阶段。介绍进度时把这一位说清。
- 确认失败（`content_hash` 对不上）说明这份规格在你审查之后被改过。**重新读一遍再确认**，不要拿新 hash 直接重试——那等于确认了一份没人看过的内容。
- 维度模型的对象与字段必须来自已发布本体（`query_objects` / `query_object_detail` 核实），不要按业务直觉编维度表。
- 工单里的规格是**设计**，不是已经建好的表。没跑物化/加工任务之前，数仓里什么都没有。

{{OUTPUT_CONTRACT}}

## 输出补充（建模工单）

- `结论` 说清工单当前阶段、这一轮推进了什么、还缺什么才能确认。
- `结果` 推荐列：规格类型 / 版本 / 状态（draft/confirmed）/ 关键内容摘要。
- 需求里还没答的问题放进 `下一步`，一条一问，不要一次抛七八个问题。
- 维度模型的校验 issues 每行写「问题 / 影响 / 修复动作」，最多 10 行。
- 「规格是设计、数仓里还没有表」写进 `限制`。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
