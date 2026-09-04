# dsh ontoMeta Skill

dsh 通过后端包内 `backend/app/mcp/skills/` 提供按用户目标拆分的 ontoMeta MCP skills；MCP
客户端也可通过 `list_prompts` / `get_prompt` 从服务端获取同一份指引，不再依赖仓库根目录。

**为什么 skill 是必需的而不是锦上添花**：MCP 只提供工具，「什么时候用哪个、结果怎么解读、
什么话不能说」全在这几份 SKILL.md 里。少一份指引，模型就会在那一块自由发挥——而这个领域里
最危险的错误恰恰不报错（照口径文字重写 SQL、按字段名猜 JOIN、按命名规则拼表名）。

## Skill 划分

| Skill | 适用目标 | 写侧边界 |
|---|---|---|
| `ontometa-discovery` | 本体、对象、关系、业务口径、血缘上下游、物理落点、数据源、角色/板块分布 | 只读 |
| `ontometa-query` | 指标口径编译取数、关联路径与字段画像、SQL 校验执行、Vega-Lite 结果预览 | 只读，取数需满足 `agent_run_sql_min_role` |
| `ontometa-task-plan` | 四类任务提案、落草稿、校验 | 最多到 `validated`；editor 即可（无副作用） |
| `ontometa-task-execute` | 确认、异步执行、终态轮询、运行记录追溯 | 写侧需 publisher；运行追溯只读 |
| `ontometa-admin` | 会话身份与工具权限自查、调用审计、使用与限流统计 | 只读；审计/统计需 publisher |
| `ontometa-mcp` | 总入口：按目标路由 + 声明共同底线与输出契约 | 不作为模型自动路由 skill |

总入口是 `disable-model-invocation: true`，模型自动路由时**不会**加载它——所以五个专用
skill 各自自带完整的工具顺序、准确性规则、输出契约和安全底线，而不是靠总入口兜底。
`backend/tests/test_dsh_skill.py` 把这件事钉死：每份 skill 的关键指引在自己身上，且
**注册表里每个 MCP 工具都必须在某份 skill 里被提到**——加了工具不写指引，测试直接失败。

## 三条会安静出错的红线

所有 skill 共同遵守。这几条错了不会报错，只会给出一个看起来合理的错答案：

1. **口径是权威**：问已有指标/标签/规则一律 `search_logics` → `compile_metric` → `execute_sql`，
   绝不照着口径文字自己重写 SQL。
2. **连接键和字面量是查出来的**：跨对象先 `find_join_path`，WHERE 带字面量先 `profile_values`。
3. **运行事实只从记录读**：`get_landing` / `get_ops_record` / `get_task_status` 说什么就是什么；
   血缘只认 `is_derivation=true` 的边，外键不是「数据从这里来」。

## 使用方式

在 dsh 中显式输入：

```text
/ontometa-discovery 查询 erpnext 本体概览
```

模型也可以按 `whenToUse` 自动选择专用 skill，但 dsh 的路由是模型决策而非硬性分类器。
要求确定执行路径时，显式用 `/ontometa-mcp` 走总入口再分流；普通非 ontoMeta 任务不会加载它们。

共同的行为约定：先用真实 MCP 工具和 ID 建立上下文；大结果先聚合（`group_by`、概览）再分页；
写侧按 `propose_* → draft_task → validate_task → confirm_task → execute_task → get_task_status`
推进；把工具受理、权限拒绝（`denied`）、限流（`rate_limited`）、校验阻断和远端执行失败分开报告；
默认输出「结论 / 证据 / 执行进度 / 下一步」，不倾倒完整 JSON 或凭据。

## 启用配置

`/Users/me/.dsh/profiles/web/cordis.patch.yml` 已启用 `skill-filesystem` 和 `tool-skill`，
并挂载 `/Users/me/Documents/ontoMeta/backend/app/mcp/skills` 为自定义 skill 根。修改 skill
正文后 dsh watcher 会刷新目录；修改 profile 配置后需重启 dsh。
