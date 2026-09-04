---
name: ontometa-query
description: ontoMeta 数据查询：已有指标口径优先用 compile_metric 编译成权威 SQL；自由查询先用 find_join_path 定关联、profile_values 定字面量，再校验执行只读 SQL，识别样本与截断，必要时返回 Vega-Lite 图表预览并用证据解释结果。
whenToUse: Use when the user asks to query warehouse data, inspect rows, validate SQL, compare values, calculate a metric or KPI, look up a business caliber definition, or render a chart from execute_sql results.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 数据查询

## 工作目标

回答“查数据、算数量、比较结果、画图”类问题。只允许只读查询；不负责创建、确认或执行治理任务。

## 口径优先（先判这一条）

问题里出现的是**已定义的指标、标签或规则**（GMV、成交额、客单价、活跃客户数、客户分层、某某合规率）时，走口径通道，不要自己写 SQL：

1. `search_logics(search=关键词)` 找到口径，记下 `id` 与 `formalized`。
2. `compile_metric(logic_id, dimensions=[...], grain=..., filters=[...])` 拿到权威 SQL 与 `caliber_trace`。
3. 把返回的 `sql` 原样交给 `execute_sql` 执行。

**绝不照着 `expression_summary` 那段文字自己重写 SQL**：口径以本体为准，自己重写会算出与其它系统不一致的数，而且错得看不出来。本体里没有对应口径时才进入下面的自由查询。

## 工具顺序（自由查询）

1. 先确认本体/对象和物理落点：必要时调用 `ontometa-discovery` 对应的查询工具，使用真实名称。
2. **要关联多个对象** → 先 `find_join_path(from_object, to_object, measure_object=度量所在对象)`：
   照它给的 `sql_hint` 或每一跳的 `on` 写 JOIN，不要按字段名猜连接键。
3. **WHERE 里要写字面量**（状态、地区、类型这类枚举值）→ 先 `profile_values(object_id, property)` 取真实取值。
   本体只保证字段存在，不保证你猜的写法在库里；猜错的字面量返回 0 行且不报错。
4. 调用 `validate_sql`，确认 SQL 是单条只读 `SELECT/WITH`。
5. 用户明确要求取数且当前角色满足 `execute_sql` 最低角色时，调用 `execute_sql`。
6. 需要图表时传 `include_vega_lite=true`；Vega-Lite 只基于最多 100 行结果样本。

## 准确性规则

- `execute_sql` 返回的是样本还是全集，依据 `truncated` 和 `sample_note` 判断；截断结果不能支持全量结论。
- 先说明 SQL 实际查到的表和筛选条件，再解释数值；不要把 SQL 推测成已执行。
- `success=true` 只表示工具调用成功，不表示业务结论正确；空结果也要明确报告。
- `search_logics` 里 `formalized=false` 的口径只有文字、编译不出 SQL：如实说明“该口径尚未形式化”，不要把文字翻译成 SQL 顶上。
- `compile_metric` 失败时看 `code`：`no_expression`（尚未形式化）、`logic_not_found`（不存在或未发布）、维度不可关联、会扇出。照 `hint` 修，不要绕开口径改写 SQL。
- 用了 `compile_metric` 就把 `caliber_trace` 作为口径证据展示；出现 `fanout_note` 或 `warnings` 必须一并说明——那是“这个数可能被 JOIN 放大”的提示，不能吞掉。
- `find_join_path` 返回 `found=0` 是**结论不是失败**：本体中这两个对象无从关联，如实说明或换对象，绝不自行构造 JOIN。
- `joinable=false` / `sql_hint=null` 表示本体没记下可用的 ON 键：不要自己补一个连接条件。
- `fanout_risk` 非空表示该 JOIN 会放大行，`SUM`/`AVG` 会算大；改用 `safe_aggs` 里的聚合，或在答案里标明这个风险。
- `profile_values` 返回 `available=false`（无仓 / 投影未就绪 / 技术字段）时，说明取不到真实取值，**不得据此猜字面量**，也不要把空画像说成「库里没有数据」。
- `profile_values` 与 `execute_sql` 同一道权限闸门；被 `denied` 就如实说权限不足，不要改用别的路子去读数据。
- 遇到权限 `denied`、限流 `rate_limited` 或无默认 Doris 仓时，说明具体原因，不绕道 REST 或读取凭据。

## 输出

使用“结论 / 查询证据 / 数据范围 / 下一步”。表格保持紧凑；口径类问题在证据里给出口径显示名与 `caliber_trace`；图表结果展示 `vega_lite` 的用途和样本范围，不输出内部连接信息。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
