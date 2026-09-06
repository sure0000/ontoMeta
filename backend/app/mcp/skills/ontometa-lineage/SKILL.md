---
name: ontometa-lineage
description: ontoMeta 血缘补录：读取 DataHub 家底与孤岛表，预览人工边或 SQL 代码包解析结果，确认真实表/字段映射后再由人批准上报 DataHub。
whenToUse: Use when the user wants to inspect lineage coverage, find isolated tables, supplement table or column lineage, scan SQL lineage, or review/apply a lineage package.
disable-model-invocation: false
user-invocable: true
---

# ontoMeta 血缘补录

## 工作目标

把 DataHub 里缺失的表级血缘补回来。MCP 负责读取家底、解析和预览；写入 DataHub 是单独的人工确认动作。
不要把外键、命名相似或模型猜测当成血缘事实。

## 工具顺序

1. 先调 `get_lineage_inventory(domain_id)`，确认域、表名、上下游数量和 `isolated` 孤岛数。表名必须从这份家底来。
2. 需要字段时调 `get_lineage_columns(domain_id, table=...)`，字段名必须从 DataHub 返回值来，不要按列名猜。
3. 人工画布补录走 `preview_lineage_supplement`：传真实的 source_table、target_table 和可核实的 `join_keys`。预览会返回每条边的 URN、状态、阻断原因和 `preview_digest`，不写库。
4. SQL 代码包不能直接作为 MCP 文件附件传入时，把代码文本交给 `preview_sql_lineage`。它只识别 INSERT INTO / CTAS / VIEW 的落点；裸 SELECT、UPDATE 没有可推的落点，不要硬报成血缘。
5. 代码包若已由 Web 上传扫描，用 `list_lineage_packages` → `get_lineage_package` 看解析失败、blocked 边和可上报边；不要把 `failed` 当成“没有血缘”。
6. 只有用户明确批准上报，并且预览摘要仍一致时，才调用 `apply_lineage_supplement` 或 `apply_lineage_package`。这两条要求本机 stdio 宿主 `ask_user_question` 的 `approved=true` 和同一 digest；远程/无交互客户端停在预览，交由 Web 确认。

## 准确性规则

- `state=blocked` 是事实：表未在 DataHub、字段不存在或不在本域。修正输入后重新预览，不要换一个相似表名。
- `state=skipped` 表示表在其它域，不要把它上报到当前域。
- 没有 `join_keys` 仍可形成表级边，但必须说明“没有关联键证据”，不能声称已补齐关系推断证据。
- `preview_digest` 绑定当前边和 URN；任何表、字段或边变化都必须重新预览。不要复用旧摘要。
- 上报回执中的 `failed` 是部分写入失败，不是“全部未写入”；先报告已写入数和失败边，不自动重试或删除 DataHub 边。
- 删除本地代码包记录不会撤销 DataHub 已写入的边；不要把删除包当成撤边。

{{OUTPUT_CONTRACT}}

## 输出补充（血缘补录）

- 读取家底时给出域名、总表数、覆盖数、孤岛数；列表截断时标明总数和展示条数。
- 预览时用“上游 → 落点 | 关联键 | 状态”表，最多 10 条；阻断边写清原因。
- `apply_*` 返回 `written=true` 只表示已向 DataHub 发起并记录写入；仍要报告 `applied` / `failed` / `resolved`。
- 不能确认宿主身份或摘要时状态写 `待确认`，不要把角色足够说成用户已经批准。

## 通用底线

MCP 是 ontoMeta 能力的唯一入口：不读 `.env`、不猜 URN/字段、不绕道 REST 或直连 DataHub；凭据、token、DSN 不进入回答、工具参数或报告。服务端 RBAC、预览摘要、审计和宿主确认是最终权威。
换个阶段就换一份指引：其它主题用 `get_playbook(topic="ontometa-…")` 取回，不要凭印象套用本份顺序。
