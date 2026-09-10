---
name: ontometa-viz
description: ontoMeta 数据可视化：图表与看板一律建在 Apache Superset 里，ontoMeta 只提供口径——先用 ensure_superset_dataset 把已落地（source_ready）的落点登记成数据集，再用受限的图表规格建图、拼看板，最后回链接。呈现上的空缺自己按默认定，不退回去问。
whenToUse: Use when the user asks to build a chart, graph, dashboard, or BI report, to visualize a metric or table, or to list/update charts that were previously created in Superset.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 数据可视化（Superset）

## 工作目标

把「我想看某个数」变成 Superset 里一张能打开的图或一个看板，并把链接交回给用户。
ontoMeta 本身不画图、不存图——Superset 才是呈现层，这里只负责**口径**与**登记**。

## 主线：三步，不要跳步

1. **找落点** —— `list_datasets` 找到要用的物理落点，记下 `ref`（形如 `obj:<id>@serving`）。
   `source_ready=false` 表示表还没建出来/还没搬数，**这样的候选先剔掉**（见「落点：先筛可用的」）。
   （想看 Superset 那边已经有哪些数据集，用 `list_superset_datasets`：`from_ontometa=true`
   的那些才是从落点登记过去、口径可追溯的。）
2. **建数据集** —— `ensure_superset_dataset(dataset_ref=...)`。幂等，已存在就复用。
   返回的 `columns` 是**这张图能用的全部字段**，`metrics` 是数据集上已保存的度量。
3. **建图** —— `create_superset_chart(...)`，把返回的 url 交给用户。
   多张图要拼一起时再 `create_superset_dashboard(chart_ids=[...])`。

要改已有的图用 `update_superset_chart`；要看建过什么用 `list_superset_assets`。

## 不要绕开落点

数据集**只能**从 `dataset_ref` 建。不要在 Superset 里凭空建虚拟数据集、也不要手写一段
SQL 去作图——那样图表口径就脱离了本体治理，业务看到的数与平台里的口径会各说各话。
落点还没建出来时，正确的回答是「这个对象还没落地，要先跑同步/物化任务」，
而不是换一张能查的表顶上。

## 列名从哪来

维度、度量、过滤里写的都是**数据集的物理列名**，只能来自第 2 步返回的 `columns`。
不要拿本体的属性名或中文显示名去填——那些是展示用的 `verbose_name`，不是列名。
拿不准就先 `ensure_superset_dataset` 看一眼列清单。

## 图表规格是受限的

`viz` 只有五种：`table`（表格）、`bar`（柱状）、`line`（折线）、`pie`（饼图）、`kpi`（单值指标卡）。
形状约束由服务端强制，写错会被当场拒绝并说明原因：

- `kpi`：恰好 1 个度量，**不能有维度**（它只显示一个数）。
- `pie`：恰好 1 个维度 + 1 个度量。
- `bar` / `line`：至少 1 个度量，且必须有 x 轴——给 `dimensions` 或 `time_column` 其一。
- `metrics` 里 `COUNT` 可以不带字段（即 `COUNT(*)`），`SUM`/`AVG`/`MIN`/`MAX`/`COUNT_DISTINCT` 必须带字段。
- 过滤用 `in` / `not_in` 时值必须是数组。

被拒绝是**规格不对**，照错误改一处再提交即可，不要换个工具绕过去。

## 呈现决策自己定，不要退回去问（本领域例外）

出口契约里那条「不许替用户选」是给**要写库、要人签字**的建数任务表单定的。建图不是那种事：
图是可整体替换的呈现物，`update_superset_chart` 一句话就改回来了，建错的代价远低于
把人晾在那儿等回答。所以本领域例外——

**只要用户说得清「要看哪个数」，就把图建出来。** 形态、维度、度量上的空缺与冲突按下表
自己定，改了什么写进 `限制`，末尾给一句「要换成 X 我改」。不要为这些反问。

| 情形 | 自己怎么定 |
|---|---|
| 形态与维度冲突（如「按 X 拆开的单值卡」） | 以**用户想看的数**为准换形态：要拆分就建 `bar`，不建 `kpi` |
| `pie` 给了多个维度 | 用第一个维度建 `pie`，其余维度降为过滤或不用 |
| 只说了「统计/多少」没说聚合 | 用 `COUNT(*)`；说了「金额/总额/求和」才用 `SUM` |
| `SUM`/`AVG` 没指字段 | 列清单里**有业务含义的数值列只剩一个**就用它；`idx`、`row_no` 这类序号列不算候选 |
| 没说时间粒度 | 按跨度给一个（跨年 `P1M`、跨月 `P1D`），写进 `限制` |
| 用户报的是中文属性名 | 拿 `columns` 里的 `label` 匹配回物理列名，别反问「你说的是哪一列」 |

**只有这两种情况才真的要问**：

- 连「要看什么数」都定不下来——没说度量，列清单里也没有唯一像样的数值列；
- 有**多个都可用**的落点或对象，选错就是另一套口径（同名跨域且两边都 `source_ready`）。

真要问时按契约的 `## 选择` 出口走，但**候选压到 3 条以内**，不要把五种形态铺开让人挑。

## 落点：先筛可用的，再谈选哪个

顺序不能反。**先把 `source_ready=false` 的候选剔掉**，再看还剩几个：

- 剩 0 个 —— 直接 `受阻`，把各候选的状态（`failed` / `syncing`）摆出来，说清要先跑同步或物化。
  **不要**先问用户选哪个域：两个都用不了时，这一问无论怎么答都到不了终点，白费一轮。
- 剩 1 个 —— 直接用，在 `限制` 里一句话交代另一个为什么没选。
- 剩 2 个及以上 —— 这才轮到用户拍板。

## 准确性规则

- `success=true` 只表示图建出来了，**不代表图里有数**。不要断言"数据显示……"——
  你没有看过这张图的结果；要看数请走 `ontometa-query` 的 `execute_sql`。
- **建图与取数的门槛不一样**：建图只看落点 `source_ready`，**不看对象发没发布**；
  而 `execute_sql` / `profile_values` 只认**已发布**对象。所以对着未发布对象的落点
  去核对字段真实取值是走不通的——别去试，连撞三次也换不来答案。这时按字段语义
  给一个合理的过滤值（如 ERPNext 的复选框列存的是字符串 `"0"` / `"1"`），
  并在 `限制` 写明「取值未核对，图若为空需回来调这一处」。
- `update_superset_chart` 是**整体替换**：没传的部分会被清掉，必须把完整规格再给一遍。
- `list_superset_assets` 的 `state` 是对账结果：`unknown` = 还没对过账（不等于不存在），
  `missing` = 上次对账时 Superset 那边已经没有了。要刷新就传 `reconcile=true`。
- 建看板返回 `embed_error` 时，看板本身是好的，只是不能在 ontoMeta 内嵌预览
  （Superset 没开 `EMBEDDED_SUPERSET`）；照实说明，别说成建失败。
- 报错带 `gate=superset_not_configured` 时，这是**配置问题不是网络问题**：
  请用户去设置页「基础设施」填 Superset 连接并拨测通过，不要重试或换路子。
- 链接原样给出，不要自己拼 Superset 的地址——后端访问地址与用户访问地址可能不同。

{{OUTPUT_CONTRACT}}

## 输出补充（可视化）

- `结论` 写建了什么（图/看板、几张、什么形态），并**把链接放在第一屏**。
- `结果` 用「名称 | 类型 | 链接」表；建了多张时逐行列出。
- `依据` **压到三行以内**：落点（`dataset_ref` 或物理表名）、维度、度量。数据集 id、
  chart_id、本体 id 这些只在用户接着要改图或要对账时才写。
- `限制` 只写**这一次真发生的事**：自动降级了什么、过滤值没核对、落点没就绪、嵌入不可用。
  「success=true 不代表图里有数」这类通用提醒**不要每次复读**——只在确实可能为空
  （刚加了过滤、落点刚建好、取值没核对）时补一句。
- 同名跨域的取舍**说一次就够**，不要每次把整个比较过程重述一遍。
- 不要把图表里的数值写进回答——你没有查过它。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
