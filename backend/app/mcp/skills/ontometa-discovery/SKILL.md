---
name: ontometa-discovery
description: ontoMeta 本体探索：查询本体、业务对象、关系、业务口径、血缘上下游、物理落点、数据源和角色/板块分布，建立真实 ID 上下文并用简洁证据回答结构问题。
whenToUse: Use for ontology overview, business object lists, object roles, segments, relations, business logic/metric definitions, lineage and upstream/downstream impact, physical landing of an object or metric, datasource discovery, and structural questions.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 本体探索

## 工作目标

回答“有哪些、多少、如何分布、对象之间是什么关系、有哪些指标口径、数据源是什么”这类结构问题。只读，不创建任务，不确认，不执行 SQL。

## 工具顺序

1. 先调用 `server_info`，记录当前身份和工具权限。
2. 用 `query_ontology` 查本体列表；不要把域名当作 `ontology_id`，必须使用返回的真实 ID。
3. 对指定本体优先调用 `get_ontology_overview`，获取元信息、角色/板块分布和业务对象精简清单。
4. 问角色或板块分布时使用 `query_objects(group_by=role|segment)`，不要拉全量明细。
5. 需要前 N 条明细时使用 `query_objects` 的 `role`、`search`、`limit`、`offset`；结果带 `total`/`truncated` 时如实说明。
6. 需要字段、关系或落点时调用 `query_object_detail` 或 `query_relations`；需要关系图时传 `include_mermaid=true`。
7. 问“有哪些指标/口径/标签/规则”时调用 `search_logics`；要某条口径的完整定义（表达式、绑定对象与字段、ADS 落点）时调用 `get_logic`。
8. 问“数据从哪来 / 被谁引用 / 改了影响谁”时调用 `get_lineage`（可传 `include_mermaid=true`）。
9. 问“这个对象/口径落到哪张表了、能不能查”时调用 `get_landing`，不要自己拼 `ods_xxx` 表名。
10. 需要数据源真实 ID 时调用 `list_datasources`，不编造连接 ID、表名或凭据。

## 准确性规则

- 区分“对象总数”“已发布对象总数”和 `business_object` 角色数量。
- `published_only` 视角与全量草稿视角不能混写；回答中标明口径。
- 角色、板块、关系是服务层返回的事实或推断证据；推断项不要写成已人工确认。
- Mermaid 只覆盖当前分页，`metadata.truncated=true` 时不要称为完整关系图。
- 口径的 `formalized` 为 false 表示只有文字定义、还不能编译成 SQL；介绍口径时要带上这一位，不要让人以为随时可取数。
- `search_logics` 的结果不带 `ontology_id`（列表读模型没有这一列）；要本体归属去 `get_logic` 取，不要拿 `domain_context_id` 冒充。
- 血缘只认 `is_derivation=true` 的边。外键/引用是业务关系，不是「数据从这里来」，两者不能混说。
- `get_lineage` 默认只看已发布：`metadata.unpublished_derivation_edges > 0` 说明草稿里还压着血缘边，此时不能说“这个对象没有上游”，要么照 `lineage_note` 传 `published_only=false`，要么说明当前是已发布视角。
- `get_landing` 报 `not_landed` 就是数仓里没有这张表：如实说“还没落地”，绝不按命名规则编一个表名。
- `get_landing` 的 keyword 定位默认跨本体，同名对象会串域（odoo 和 erpnext 各有一个「公司」）；候选带 `domain_name`，选错域比没找到更糟。

## 输出

默认中文，使用“结论 / 证据 / 下一步”。证据只列真实 ID、数量、状态和截断信息，不倾倒完整 JSON、属性列表或凭据。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 ID、不绕道 REST 或直连数据库；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、校验闸门、审计和状态机是最终权威。把 `success` / `denied`（角色不够）/ `rate_limited`（限流）/ 校验阻断 / 远端执行失败分开报告，不要把“工具调用成功”写成“事情办成了”。
