---
name: ontometa-viz
description: ontoMeta 数据可视化：图表与看板一律建在 Apache Superset 里，ontoMeta 只提供口径——先用 ensure_superset_dataset 把已发布本体的落点登记成数据集，再用受限的图表规格建图、拼看板，最后回链接。
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
   `source_ready=false` 表示表还没建出来/还没搬数，此时如实说明，不要往下走。
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

## 准确性规则

- `success=true` 只表示图建出来了，**不代表图里有数**。不要断言"数据显示……"——
  你没有看过这张图的结果；要看数请走 `ontometa-query` 的 `execute_sql`。
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
- `依据` 写清这张图建在哪个落点上（`dataset_ref` 与物理表名）、用了哪些维度与度量。
- 落点没就绪、嵌入不可用、规格被拒后做了什么调整，都进 `限制`。
- 不要把图表里的数值写进回答——你没有查过它。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份的顺序。
