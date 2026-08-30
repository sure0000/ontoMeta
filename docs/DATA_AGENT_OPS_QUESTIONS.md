# Data Agent 运营问题生产验收集

> V6 P3 离线验收集。机器可执行源为
> `backend/tests/fixtures/ops_questions.py`，回归入口为
> `backend/tests/test_chat_bi_ops_eval.py`。

## 验收口径

- 原方案按 11 个业务问题族描述能力；实现层实际拆成 13 个权威 reader。为了让“正确
  family”可直接断言，本集按 13 个 reader 每族 8 题，共 104 题，覆盖范围高于原定约 90 题。
- 可答 = 顶层意图为 `operational`、自动技能为 `ops`、命中正确 tool/family，并且 reader
  信封包含 `family`、`observed_at`、`source` 和 `as_of` 键。
- `as_of` 允许为 `null`：没有发生过执行、发布或拨测事件时，不能用本次读取时间冒充事实时点。
- 未调用权威 reader 的运营回答必须进入 grounding refusal；分析型平局必须留在 `query`，
  明确写请求必须留在 `task`/`onboard`，不能进入只读车道。

## 实测结果

| 指标 | P3 结果 | 门槛 |
| --- | ---: | ---: |
| 正确 intent + skill + tool/family | 104 / 104（100%） | >= 80% |
| 权威 reader 信封 | 13 / 13（100%） | 100% |
| 分析平局与写意图护栏 | 8 / 8（100%） | 100% |
| 端到端运营 golden | 3 / 3；正常回答平均 2.0 次 LLM | 不编造；正常回答 <= 2.6 次 LLM |

## 生产问题矩阵

### `landing`：A 落点/物化

| ID | 问题 | 期望 |
| --- | --- | --- |
| `landing-01` | 采购订单落到哪张物理表了？ | `get_landing` / `landing` |
| `landing-02` | 客户对象的物理落点是什么？ | `get_landing` / `landing` |
| `landing-03` | 销售订单对应的表建了吗？ | `get_landing` / `landing` |
| `landing-04` | 库存对象同步到哪张表了？ | `get_landing` / `landing` |
| `landing-05` | 订单总额口径物化在哪张表？ | `get_landing` / `landing` |
| `landing-06` | 这个对象现在能不能查？ | `get_landing` / `landing` |
| `landing-07` | 客户数据是否落地？ | `get_landing` / `landing` |
| `landing-08` | 销售额口径的物理 ADS 表是哪张？ | `get_landing` / `landing` |

### `task_run`：B 任务执行

| ID | 问题 | 期望 |
| --- | --- | --- |
| `task_run-01` | 订单同步任务跑完了吗？ | `get_ops_record(task_run)` |
| `task_run-02` | 最近一个物化任务的执行状态是什么？ | `get_ops_record(task_run)` |
| `task_run-03` | 上次数据任务为什么失败？ | `get_ops_record(task_run)` |
| `task_run-04` | 这个任务卡在哪一步？ | `get_ops_record(task_run)` |
| `task_run-05` | 给我看最近一次执行记录。 | `get_ops_record(task_run)` |
| `task_run-06` | 订单加工任务进度怎么样？ | `get_ops_record(task_run)` |
| `task_run-07` | 最近失败任务的失败原因是什么？ | `get_ops_record(task_run)` |
| `task_run-08` | 客户同步任务跑到哪了？ | `get_ops_record(task_run)` |

### `pipeline`：B 任务执行

| ID | 问题 | 期望 |
| --- | --- | --- |
| `pipeline-01` | 整条订单任务链做到哪了？ | `get_ops_record(pipeline)` |
| `pipeline-02` | 客户加工任务链状态怎么样？ | `get_ops_record(pipeline)` |
| `pipeline-03` | 这条流水线进度到哪一步？ | `get_ops_record(pipeline)` |
| `pipeline-04` | 订单任务链的 DAG 编译了吗？ | `get_ops_record(pipeline)` |
| `pipeline-05` | 整条链卡在哪个步骤？ | `get_ops_record(pipeline)` |
| `pipeline-06` | 列出任务链每一步的逐步状态。 | `get_ops_record(pipeline)` |
| `pipeline-07` | 数据流水线状态和下一步是什么？ | `get_ops_record(pipeline)` |
| `pipeline-08` | 任务链进度为什么被阻塞？ | `get_ops_record(pipeline)` |

### `decision`：F 决策/审计

| ID | 问题 | 期望 |
| --- | --- | --- |
| `decision-01` | 当前会话的六环进度到哪一环？ | `get_ops_record(decision)` |
| `decision-02` | 这个方案当初是谁批的？ | `get_ops_record(decision)` |
| `decision-03` | 需求和计划分别由谁确认？ | `get_ops_record(decision)` |
| `decision-04` | 给我看本会话的确认记录。 | `get_ops_record(decision)` |
| `decision-05` | 最近有哪些决策记录？ | `get_ops_record(decision)` |
| `decision-06` | 当前有没有确认后未执行的悬挂确认？ | `get_ops_record(decision)` |
| `decision-07` | 这次任务是谁拍板的？ | `get_ops_record(decision)` |
| `decision-08` | 六环闭环还缺哪些环节？ | `get_ops_record(decision)` |

### `ontology_version`：D 本体版本/发布

| ID | 问题 | 期望 |
| --- | --- | --- |
| `ontology_version-01` | 当前本体版本是多少？ | `get_ops_record(ontology_version)` |
| `ontology_version-02` | 这个域的本体发布到第几版了？ | `get_ops_record(ontology_version)` |
| `ontology_version-03` | 最新发布版本和上一版有什么差异？ | `get_ops_record(ontology_version)` |
| `ontology_version-04` | 列出本体版本历史。 | `get_ops_record(ontology_version)` |
| `ontology_version-05` | 本体第几版是当前生效版本？ | `get_ops_record(ontology_version)` |
| `ontology_version-06` | 最近一次版本变更改了什么？ | `get_ops_record(ontology_version)` |
| `ontology_version-07` | 给我看历次发布记录。 | `get_ops_record(ontology_version)` |
| `ontology_version-08` | 第 3 个发布版本的版本差异是什么？ | `get_ops_record(ontology_version)` |

### `standard`：H 规约合规

| ID | 问题 | 期望 |
| --- | --- | --- |
| `standard-01` | 当前规约是哪一版？ | `get_ops_record(standard)` |
| `standard-02` | 现在生效规约包含什么？ | `get_ops_record(standard)` |
| `standard-03` | 列出治理规约的强制条款。 | `get_ops_record(standard)` |
| `standard-04` | 治理标准最近发布的是哪个版本？ | `get_ops_record(standard)` |
| `standard-05` | 当前有哪些合规规则？ | `get_ops_record(standard)` |
| `standard-06` | 规约版本历史是什么？ | `get_ops_record(standard)` |
| `standard-07` | 现行标准版本何时生效？ | `get_ops_record(standard)` |
| `standard-08` | 当前治理规约和历史版本有哪些？ | `get_ops_record(standard)` |

### `draft_run`：E 草稿/复核

| ID | 问题 | 期望 |
| --- | --- | --- |
| `draft_run-01` | 最近一次草稿生成状态怎么样？ | `get_ops_record(draft_run)` |
| `draft_run-02` | 本体生成进度到多少了？ | `get_ops_record(draft_run)` |
| `draft_run-03` | 上次生成为什么失败？ | `get_ops_record(draft_run)` |
| `draft_run-04` | 草稿生成任务进度怎么样？ | `get_ops_record(draft_run)` |
| `draft_run-05` | 查看最近的生成记录。 | `get_ops_record(draft_run)` |
| `draft_run-06` | 本体生成状态和错误摘要是什么？ | `get_ops_record(draft_run)` |
| `draft_run-07` | 上次草稿生成了多少证据？ | `get_ops_record(draft_run)` |
| `draft_run-08` | 草稿生成失败原因是什么？ | `get_ops_record(draft_run)` |

### `merge_report`：E 草稿/复核

| ID | 问题 | 期望 |
| --- | --- | --- |
| `merge_report-01` | 查看最近一次合并报告。 | `get_ops_record(merge_report)` |
| `merge_report-02` | 重新生成改了什么？ | `get_ops_record(merge_report)` |
| `merge_report-03` | 这次合并结果新增和更新了哪些对象？ | `get_ops_record(merge_report)` |
| `merge_report-04` | 重新生成的变化有哪些？ | `get_ops_record(merge_report)` |
| `merge_report-05` | 本体草稿的生成差异是什么？ | `get_ops_record(merge_report)` |
| `merge_report-06` | 合并时保留了什么？ | `get_ops_record(merge_report)` |
| `merge_report-07` | 给我看最近的合并摘要。 | `get_ops_record(merge_report)` |
| `merge_report-08` | 这次重新生成的合并报告有多少变化？ | `get_ops_record(merge_report)` |

### `conflict`：E 草稿/复核

| ID | 问题 | 期望 |
| --- | --- | --- |
| `conflict-01` | 当前有哪些待复核冲突？ | `get_ops_record(conflict)` |
| `conflict-02` | 列出本体里的合并冲突。 | `get_ops_record(conflict)` |
| `conflict-03` | 还有哪些冲突字段没处理？ | `get_ops_record(conflict)` |
| `conflict-04` | 查看字段冲突清单。 | `get_ops_record(conflict)` |
| `conflict-05` | 这些冲突的机器值和人工值分别是什么？ | `get_ops_record(conflict)` |
| `conflict-06` | 当前冲突三元组有哪些？ | `get_ops_record(conflict)` |
| `conflict-07` | 待复核冲突一共涉及哪些字段？ | `get_ops_record(conflict)` |
| `conflict-08` | 显示所有字段冲突的 base、ours 和 theirs。 | `get_ops_record(conflict)` |

### `datasource`：C 数据源/连通

| ID | 问题 | 期望 |
| --- | --- | --- |
| `datasource-01` | ERP 数据源状态怎么样？ | `get_ops_record(datasource)` |
| `datasource-02` | 业务库上次测通是什么时候？ | `get_ops_record(datasource)` |
| `datasource-03` | 查看所有数据源连接状态。 | `get_ops_record(datasource)` |
| `datasource-04` | Odoo 数据源连得上吗？ | `get_ops_record(datasource)` |
| `datasource-05` | 数据库连通的拨测结果是什么？ | `get_ops_record(datasource)` |
| `datasource-06` | 哪些数据源是否可用？ | `get_ops_record(datasource)` |
| `datasource-07` | ERP 库的连接测试结果是什么？ | `get_ops_record(datasource)` |
| `datasource-08` | 最近一次数据源连接检查成功了吗？ | `get_ops_record(datasource)` |

### `data_app`：J 数据应用

| ID | 问题 | 期望 |
| --- | --- | --- |
| `data_app-01` | 经营看板版本是多少？ | `get_ops_record(data_app)` |
| `data_app-02` | 这个看板发布了几版？ | `get_ops_record(data_app)` |
| `data_app-03` | 列出数据应用的发布记录。 | `get_ops_record(data_app)` |
| `data_app-04` | 销售大屏版本和发布时间是什么？ | `get_ops_record(data_app)` |
| `data_app-05` | 当前应用版本是否已经发布？ | `get_ops_record(data_app)` |
| `data_app-06` | 经营面板版本历史有哪些？ | `get_ops_record(data_app)` |
| `data_app-07` | 最新一次应用发布是谁操作的？ | `get_ops_record(data_app)` |
| `data_app-08` | 数据应用当前编辑版和发布版分别是多少？ | `get_ops_record(data_app)` |

### `component`：C 数据源/连通

| ID | 问题 | 期望 |
| --- | --- | --- |
| `component-01` | Airflow 组件部署状态是什么？ | `get_ops_record(component)` |
| `component-02` | DataHub 依赖组件是否可用？ | `get_ops_record(component)` |
| `component-03` | Doris 组件部署失败原因是什么？ | `get_ops_record(component)` |
| `component-04` | 列出所有依赖组件状态。 | `get_ops_record(component)` |
| `component-05` | LLM 组件上次部署结果怎么样？ | `get_ops_record(component)` |
| `component-06` | 哪个组件部署失败了？ | `get_ops_record(component)` |
| `component-07` | Airflow 的组件状态和部署方式是什么？ | `get_ops_record(component)` |
| `component-08` | 查看依赖组件的部署结果。 | `get_ops_record(component)` |

### `migration`：K 生产割接

| ID | 问题 | 期望 |
| --- | --- | --- |
| `migration-01` | 生产割接状态怎么样？ | `get_ops_record(migration)` |
| `migration-02` | 当前割接进度到哪一步？ | `get_ops_record(migration)` |
| `migration-03` | 这次生产切换是谁批准的？ | `get_ops_record(migration)` |
| `migration-04` | 割接观察窗什么时候结束？ | `get_ops_record(migration)` |
| `migration-05` | 最近迁移批次为什么被阻塞？ | `get_ops_record(migration)` |
| `migration-06` | 生产割接的回滚责任人是谁？ | `get_ops_record(migration)` |
| `migration-07` | 影子校验通过了吗？ | `get_ops_record(migration)` |
| `migration-08` | 查看割接批次的不可变证据时间线。 | `get_ops_record(migration)` |

## 平局与写意图护栏

以下问题不计入 104 题分母，但必须保持路由不越界：

| 问题 | 期望技能 |
| --- | --- |
| 近 30 天任务失败次数是多少？ | `query` |
| 统计各数据源连接失败数量。 | `query` |
| 本月组件部署成功率是多少？ | `query` |
| 对比每个版本的对象数量。 | `query` |
| 创建一个订单同步任务。 | `task` |
| 帮我重新执行失败任务。 | `task` |
| 立即执行生产割接。 | `task` |
| 帮我配置一个数据源。 | `onboard` |
