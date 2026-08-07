# 数据治理规约实现设计（Governance Standard, G0–G3）

> **本文是 as-built（已建成规格），不是提案。** G0–G3 全部落地 + Data Agent 建数技能已接规约，
> 全量后端测试 **946 passed / 0 failed**。剩余为增强项（更严版本 / provenance 深化 / 表向 skill），见 §9。
>
> 已核对的现状事实（编码前提）：
> - 写侧治理**闸门本已齐全**：`agents/validation.py` 的 Validation Gate、`warehouse/adapters/base.py`
>   的 `Adapter.guard`（能力校验）、`services/ontology_formal.py` 的发布不变式、
>   `services/materialize_preflight.py` 的提交前自检。缺的不是闸门，是**闸门读什么**。
> - 制品经 `services/agent_pipeline.py` 的 `draft → validate → confirm → execute` 状态机流转；
>   `validate` 调 `validate_spec` + `is_blocking`（`agent_pipeline.py:92-95`）。
> - 物化产物由 `services/warehouse_generator.py` 从「本体 + 物化契约」编译成引擎无关的
>   `LogicalSchema`，再由 Dialect Adapter 渲染 DDL。落层在 `services/materialization_contract.py` 决定。
> - 真实 DataHub「零主键/零注释」（见记忆 `real-datahub-shape`）——决定了哪些规约条款只能先 advisory。

---

## 0. 一句话结论

把「治理标准」从**散落各闸门的硬编码 if** 升级成**一份声明式规约（Policy Pack）**，它同时：
**① 喂给 agent 当前置约束**（少撞闸门）、**② 驱动 Validation Gate 当硬闸门**（撞了必拒）、
**③ 驱动生成器/落层当产物规范**（建出来就合规）、**④ 自身落库版本化 + 存量可 re-lint**。

> 核心是把**判据从代码搬进数据**：同一份 `active_standard(db)` 既约束 agent 的提议，又守住执行的闸门，
> 两端咬同一份标准。

---

## 1. 架构

```
                        app/governance/  ── 规约即数据（G0）
                        standard.py: DEFAULT_STANDARD v1.0.0（六分区，14 条 Rule）
                        lint.py:     Violation + lint_spec / lint_logical_table
                                │
          ┌─────────────────────┼──────────────────────────┬───────────────────────┐
          ▼(G1 校验期)          ▼(G2 生成期)                ▼(G2 起草期)            ▼(G3 自治理)
  agents/validation.py   warehouse_generator.py       lint_against_standard   services/governance_standard.py
  validate_spec 读规约    落层读 standard.layering      = agent 自检工具入口     _REGISTRY + get_active/publish
  _check_standard→lint    真实物理名体检→plan.note     （待挂 S3 建数 skill）    relint 存量 + 版本戳
          │                                                                         │
          └─────────────────── 全部经 active_standard(db) 取同一份规约 ──────────────┘
```

四层强制点全部只调 `active_standard(db)`（`standard.py:339`），G3 落库后**只换该函数实现、不改任一调用点**。

---

## 2. 规约 schema（G0）—— `app/governance/standard.py`

一份 frozen dataclass 树，六个分区，每条条款是一个 `Rule`：

```python
@dataclass(frozen=True)
class Rule:
    code: str            # 稳定机器码；平移既有检查须沿用原 code（如 missing_required_field）
    description: str
    severity: Severity   # "error" | "warning"
    waivable: bool = False   # 是否允许带理由豁免
    enforced: bool = True    # 平移开关：True=复现现状硬约束；False=仅声明、先不阻断（advisory）
```

| 分区 | 内容 | 平移锚点 |
| --- | --- | --- |
| `naming` | 库名 `层[_前缀]`、snake_case、保留字 | `warehouse_generator.TargetNaming` |
| `layering` | 角色/结构 → 层（`business_object→dim`、`bridge/fact→dwd`、指标`→ads`）；依赖方向 | `materialization_contract` 落层逻辑 |
| `required_metadata` | 各制品必填字段 + 表级 comment/owner/pk/partition | `validation._REQUIRED_FIELDS`（已删，判据移入规约） |
| `types` | 语义类型 → 物理类型（金额 decimal，禁浮点） | 各 Adapter `map_type` |
| `security` | Spec 禁明文凭据，只放 `*_ref`/`*_alias` | `validation` 凭据检查 |
| `tasks` | 全量走 staging+原子切换、批 ≤50、缺省 full | M15 / M16 |

- **内置默认**：`DEFAULT_STANDARD = GovernanceStandard(version="1.0.0")`（`standard.py:336`）。版本号语义化，major 变更 = 收紧了某条 enforced 规则（可能拒存量），需配套 re-lint。
- **agent 约束卡**：`compile_prompt_card()`（`standard.py:310`）编译成 <800 字符的人读要点，喂 prompt 用；不倾倒整份 JSON（对齐记忆 `chatbi-sends-full-ontology-413` 的裁剪教训）。
- **取规约入口**：`active_standard(db)`（`standard.py:339`）—— 无 db 回 `DEFAULT_STANDARD`；有 db 委托 G3 的 service。

---

## 3. 校验期硬闸门（G1）—— `app/agents/validation.py`

`validate_spec` 起手取 `standard = active_standard(db)`，判据全部来自它：

- **必填字段 / 凭据**（enforced）：`_check_required_metadata` 读 `standard.required_metadata.per_artifact`、`standard.security.forbidden_tokens`/`allowed_ref_suffixes`。`_REQUIRED_FIELDS` 常量已删——闸门不再自持字面值。**沿用原 code**（`missing_required_field`、`credential_in_spec`）护住拒绝码分布。
- **命名**（advisory）：`_check_standard`（`validation.py:230`）委托 `lint_spec`（见 §4），把 `Violation` 投影成 `ValidationIssue`。
- **`is_blocking` 数据驱动**（`validation.py:45`）：

```python
def is_blocking(issue):
    return issue.code not in _WARNING_CODES and issue.code not in _standard_warning_codes()
# _standard_warning_codes() = 规约里 severity=="warning" 的条款码，自动不阻断
```

> 加一条 advisory 规约条款，`is_blocking` 无需改动就把它判为不阻断——这是「判据即数据」的直接红利。

---

## 4. 生成期产物合规 + agent 自检（G2）—— `app/governance/lint.py`

命名规则**一处定义、三处复用**（`_SNAKE_RE` 唯一住 lint.py）：

```python
@dataclass(frozen=True)
class Violation:
    code, severity, message, fix, entity_type, entity_name    # fix = 可照做的下一步
```

| 入口 | 载体 | 谁调 |
| --- | --- | --- |
| `lint_spec(kind, spec, std)` (`lint.py:79`) | Spec 的 `target_table` | Validation Gate（§3）+ agent 自检 |
| `lint_logical_table(table, std)` (`lint.py:94`) | **真实物理表**（名/comment/pk） | `warehouse_generator` + `relint` |
| `lint_against_standard(kind, spec, db)` (`lint.py:132`) | Spec，返回 JSON | agent 工具入口（待挂 S3） |

**生成期体检**（`warehouse_generator.py:258`）：`build_logical_schema` 编完 `LogicalSchema` 后，对每张**真实物理名**跑 `lint_logical_table`，违规记进既有 `plan.note` 通道。只 surface 命名类：

```python
_GENERATOR_SURFACED_CODES = frozenset({"naming_snake_case", "naming_reserved_word"})  # warehouse_generator.py:45
```

> comment/pk advisory 不逐表刷——真实 DataHub「零注释零主键」，逐表 note 会淹没有效信号，留给 G3 的 `relint` 聚合呈现。

**落层读规约**（`materialization_contract.py:78`）：`derive` 的层字面值改读 `active_standard(db).layering`（`role_to_layer`/`structure_to_layer`/`business_logic_layer`）；「是否物化 / derivation_reason」仍是本服务的派生语义。`test_materialization_contract` 的 `dim/dwd/ads` 断言全过 → 平移忠实。

每条违规带 `fix`（如 `DimCustomer` → 「改为 snake_case，如 dim_customer」），延续 `materialize_preflight`「每项失败给下一步」的哲学，让 agent 能自己改对而非只被拒。

---

## 5. 落库自治理 + 版本戳 + 存量 re-lint（G3）

### 5.1 版本由代码定义

> **为什么 DB 不存可执行 JSON**：规约条款的**判定逻辑**住在 `lint.py`/`validation.py`——一条 linter 不会执行的纯数据规则是死规则。故「版本」是在已登记的**代码常量**之间选一个生效；DB 只记「激活哪版 + 审计历史」。这一刀避开了脆弱的全树反序列化。

- **模型** `models/governance.py::GovernanceStandardRecord`：`version` / `status`(draft|published|superseded) / `payload_json`（只读快照，供审计/diff，运行时不读） / `activated_at`。
- **注册表** `services/governance_standard.py:24`：`_REGISTRY = {version: 代码定义的规约常量}`。新版本上线时 `register_standard(std)`。

### 5.2 自治理 API 语义

`GovernanceStandardService`：

| 方法 | 语义（draft→confirm→publish 的映射） |
| --- | --- |
| `available_versions()` | 候选版本（draft 面） |
| `publish(db, version, note)` | 确认+发布；旧 published 降级 superseded，任一时刻至多一个 published |
| `get_active(db)` | 最近发布记录 → 回注册表取代码常量；**无记录回落 `DEFAULT_STANDARD`**（零配置也能跑） |
| `history(db)` | 审计轨迹 |
| `relint(db, ontology_id)` (`governance_standard.py:90`) | 用 `WarehouseGenerator` 编译产物 + `lint_logical_table` 找存量不合规表 |

`active_standard(db)`（`standard.py:339`）现委托 `service.get_active(db)`（延迟 import 破 standard↔service 环）。

### 5.3 版本戳

`agent_pipeline.validate` 把 `active_standard(db).version` 写进 `validation_report_json.standard_version`（`agent_pipeline.py:117`）——审计「本制品在哪版规约下过闸」，规约升级后据此判是否需 re-lint。

### 5.4 规约升级流程

```
加新版本常量（standard.py）→ register_standard → publish(new_version) → relint(ontology) 找存量不合规
```

---

## 6. 不变式（护住零回归）

1. **平移忠实性**：只有 **2 条 enforced**（`missing_required_field`、`credential_in_spec`），复现现状硬约束、沿用原 code；其余 **12 条全 advisory**（`enforced=False`，呈现不阻断）。测试 `test_governance_standard.py::test_advisory_rules_not_enforced_in_g0` 钉死「enforced 集合恰为这两条」。
2. **判据单一来源**：命名规则只在 `lint.py` 定义一次，Gate 与 agent 自检共用；必填/凭据/落层字面值移入规约后，各闸门不得再自持副本。
3. **enforced 行为逐字节不变**：接规约后拒绝码分布、`is_blocking` 结果与接线前一致（`test_governance_gate.py` 行为断言）。
4. **建表遵循走既有骨架**：Data Agent 不新增写侧出口；建数只产提案，写库仍由 `agent_pipeline` 的 confirm→execute 执行（对齐 V3 S3「agent 只出提案、写在用户点击」）。

---

## 7. API —— `app/api/governance.py`

全局 `AdminAuthMiddleware` 已对 `/api` 鉴权，故不重复守卫。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/governance/standard` | 生效规约 + 可发布版本 + `prompt_card` |
| GET | `/api/governance/standard/history` | 发布审计历史 |
| POST | `/api/governance/standard/publish` | 发布某版本（未登记版本 → 400） |
| GET | `/api/ontologies/{id}/governance/relint` | 按生效规约体检该本体物理产物 |

---

## 8. 测试

| 文件 | 数 | 覆盖 |
| --- | --- | --- |
| `test_governance_standard.py` | 10 | 平移忠实性（必填/层/凭据字面值对齐 source-of-truth）、enforced=2、prompt_card 简短 |
| `test_governance_gate.py` | 11 | enforced 阻断（必填/凭据）行为不变；advisory 命名呈现不阻断 |
| `test_governance_lint.py` | 11 | lint_spec / lint_logical_table / lint_against_standard 三入口 + fix 可照做 |
| `test_governance_persistence.py` | 8 | get_active 回落、publish 顶替、未知版本拒绝、relint 形状、API 冒烟 |
| `test_agent_pipeline.py` | +1 | 版本戳写入 `validation_report.standard_version` |

**测试库建表坑**：`active_standard(db)` G3 后查 `governance_standard_records` 表；直连 `SessionLocal` 且早于 app 启动的用例会 `no such table`。已在 `tests/conftest.py:25` import 期 `Base.metadata.create_all` 兜底（对齐 app 启动 `init_db` 建表；生产由 `run_migrations` 保证，无此问题）。

---

## 9. 未接 / 后续

- **S3 建数 skill 挂接（已交付）**：V3 create 技能（口径提案）现已接规约——`chat_bi_skills.Skill.attach_governance=True` 使选中建数技能时把 `compile_prompt_card()` 并入 prompt overlay（`chat_bi.py` 的 `governance_card` 在循环外由 `active_standard(db)` 备好、`_apply_select_skill` 按标志并入，scoped 不污染取数/概览）；并解锁 `lint_against_standard` 自检工具（`_dispatch_lint`）——agent 提含物理表名的规格前可自检、据 `fix` 自改而非等治理闸门打回。
  > 注：create 技能是**口径（中文名）提案**、非建表 spec，故规约的「表向」条款对它多为背景意识；`lint_against_standard` 对无 `target_table` 的口径提案返回空（合规）。真正的表向自检待未来「建表 skill」产出带 `target_table` 的 spec 时才满咬合。
- **更严版本 + provenance 深化**：目前版本戳落在 `validation_report_json`；后续可进 `provenance_service` 做制品级不可变溯源。上线 v1.1.0（把某条 advisory 收紧为 enforced）时，须先 `relint` 全量存量、评估影响面再 publish。
- **G2 未覆盖的规约条款**：`types`（金额 decimal）、`layering` 依赖方向、表级 owner 目前仍为 advisory 且无物理载体校验，待相应元数据在本体/契约上成形后接入 `lint_logical_table`。

---

## 附录：规约条款清单（v1.0.0，14 条）

| # | code | 分区 | severity | enforced |
| --- | --- | --- | --- | --- |
| 1 | `missing_required_field` | required_metadata | error | ✅ |
| 2 | `credential_in_spec` | security | error | ✅ |
| 3 | `naming_layer_prefix` | naming | error | — |
| 4 | `naming_snake_case` | naming | warning | — |
| 5 | `naming_reserved_word` | naming | warning | — |
| 6 | `layering_role_layer` | layering | error | — |
| 7 | `layering_dep_direction` | layering | warning | — |
| 8 | `table_comment_missing` | required_metadata | warning | — |
| 9 | `table_owner_missing` | required_metadata | warning | — |
| 10 | `primary_key_missing` | required_metadata | warning | — |
| 11 | `partition_missing` | required_metadata | warning | — |
| 12 | `type_semantic_mismatch` | types | warning | — |
| 13 | `task_batch_size` | tasks | warning | — |
| 14 | `task_full_load_staging` | tasks | warning | — |

> `enforced=—` = 现状未强制、本次仅声明（advisory），G1 起呈现不阻断。收紧任一条为 enforced 须走 §9 的版本升级 + re-lint 流程。
