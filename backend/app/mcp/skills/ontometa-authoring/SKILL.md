---
name: ontometa-authoring
description: ontoMeta 业务逻辑创作与治理：把一句业务口径变成可编译的表达式（先 compile_logic_expression 编到通过，再 create_logic 落草稿），管理分类与对象/字段绑定，审查并在人工确认后发布。
whenToUse: Use when the user wants to define a new metric, tag, or rule, formalize an existing business logic that only has a text definition, or check a spec against the governance standard before proposing a task.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 口径创作

## 工作目标

把「客户分组表里 is_group=0 的分组数量」这样一句话，变成一条**编译得出 SQL** 的口径草稿。出口是草稿，发布仍走既有治理闸门。

## 为什么可以让你写表达式

守卫不是提示词，是编译器：组装不出、编译不过、或过不了语义证明的表达式**到不了落库那一步**。所以放手试，但要按下面的顺序试——别把没编过的东西直接拿去建。

## 工具顺序

1. **先查重**：`search_logics` 找有没有同名或同义的口径。同一个指标建两条出来，之后没人分得清该用哪条。
   - 查到一条已存在但 `formalized=false`（只有文字定义）时，走第 5 步补全它，**不要**新建一条。
2. **确认字段是真的**：`query_objects` / `query_object_detail` 拿对象与字段的**技术名**（`order` / `amount`），不是中文显示名。猜出来的字段名编译一定失败，而反复失败会让你开始改口径去迁就自己的猜测。
3. **`compile_logic_expression` 编到通过**（reader，不写库，可以随便试）：
   - `fields` 给别名 → 对象 + 字段；`body` 按类型三选一（metric 的 `operation`/`args`/`group_by`/`filter`；tag 的 `cases`；rule 的 `condition`+`message`）。
   - 编不过时返回 `code` 与可用字段/支持的算子——**照着改**，不要换一个更简单的口径去绕过它。SUM/AVG 只能作用于语义类型为 measure 的字段，这类拒绝是真实约束。
   - 编过了拿到 `compiled_sql` 和 `caliber_trace`：那是给人看的凭据，把它摆出来让用户核对口径对不对，再往下走。
4. **`create_logic` 落草稿**（editor）：把编过的 `fields`/`body` 原样带上。
   - 不带表达式也可以建（只给名字和 `description`），但那时 `description` 必填——它是这条口径唯一承载含义的地方。只有名字的口径等于没有口径。
   - `category_id` 必须来自返回里的分类目录，不要编。
5. **`update_logic_expression`** 给已存在的口径补全表达式：只给 `logic_id` + `fields` + `body`，类型与名字沿用原口径，表达式在**它自己所属的本体**上编译。
6. 要提含物理表名的建数规格时，先 `lint_spec` 自检命名规约，照 `fix` 自己改，别等治理闸门打回。
7. 管理已有口径时，分类先用 `list_logic_categories`；元数据用 `update_logic`；对象/字段绑定分别用 `bind_logic_object` / `bind_logic_property`，解绑分别用 `unbind_logic_object` / `unbind_logic_property`，解绑前确认 binding_id。
8. 发布前用 `review_logic_publish`：它会重编已存表达式并返回 `confirmation_digest`。只有用户明确批准且宿主交互确认可用时，才调用 `publish_logic`；远程/无交互客户端停在审查结果，交由 Web 发布。

## 准确性规则

- `compile_logic_expression` 成功**不等于**口径落库了：它 `written=false`。别把「编译通过」说成「指标已建好」。
- `create_logic` 建出来的是**草稿**，不是已发布口径。发布是另一件事，由人在治理流程里做。
- `lint_spec` 的 `compliant` 可能是 `null`：spec 里没有物理表名时一条规则都没跑过，此时**不得**说「符合治理规约」，照 `note` 如实说明本次没有可校验条款。
- 口径的语义由用户定，不由你定。用户说「成交额」时若本体里有两条候选口径，问清楚再编，不要挑一条像的。
- 编译器报的 `code` 是结构性拒绝（字段不存在、算子不适用、分支缺值），不是「再试一次可能就好了」——重复提交同一份 body 只会得到同一个错误。

{{OUTPUT_CONTRACT}}

## 输出补充（口径创作）

- `结论` 说清这条口径当前处在哪一步：只编译过用 `待确认`；已落草稿用 `完成`（并说明是草稿）；编译失败用 `受阻`。
- `结果` 必须包含 `compiled_sql` 与 `caliber_trace` 的要点——那是用户核对口径的唯一依据，不要只说「已编译通过」。
- 编译失败时把 `code`、失败原因、以及**可用字段**摆出来，最多 10 行；不要粘完整的候选字段清单。
- 「草稿 ≠ 已发布」写进 `限制`。
- `review_logic_publish` 的 `ready=true` 只是服务端审查通过，不等于用户已经批准；`publish_logic` 的写入必须携带同一 digest 和宿主批准。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
