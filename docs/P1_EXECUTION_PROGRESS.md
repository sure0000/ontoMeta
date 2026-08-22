# P1 执行进展报告

> 执行时间：2026-08-22  
> 阶段目标：建模工单与规格版本骨架（1～1.5 周）

---

## 已完成任务

### ✅ P1-1：数据模型与迁移

**交付物**：
- `backend/app/models/modeling.py`（3 个模型类，6 个枚举）
  - `ModelingCase`：工单主表
  - `ModelingCaseSpec`：版本化规格表
  - `ModelingCaseLink`：引用表
- Alembic 迁移：`6ac2622d9b62_add_modeling_case_tables.py`
- 注册到 `app/models/__init__.py`

**验证**：
- 迁移已应用：`6ac2622d9b62` (head & current)
- 3 张表已创建：`modeling_cases`, `modeling_case_specs`, `modeling_case_links`
- 索引与约束完整

---

### ✅ P1-2：Pydantic Schemas

**交付物**：
- `backend/app/schemas/modeling.py`（10+ 个 schema 类）
  - 6 类规格：`RequirementSpec`, `ModelingContextSpec`, `DimensionalModelSpec`, `LogicBundleSpec`, `DeliveryPlanSpec`, `AcceptanceSpec`
  - API 请求：`ModelingCaseCreate`, `ModelingCaseUpdate`, `ModelingCaseSpecSave`, `ModelingCaseSpecConfirm`, `ModelingCaseSpecReject`
  - API 响应：`ModelingCaseOut`, `ModelingCaseSpecOut`, `ModelingCaseLinkOut`
  - 统一校验：`SPEC_KIND_TO_MODEL`, `validate_spec_payload`

**特性**：
- 强类型校验
- `extra="forbid"` 防止意外字段
- 第一版保留 `DimensionalModelSpec` 简化结构（P3 会扩展）

---

### ✅ P1-3：服务层

**交付物**：
- `backend/app/services/modeling_case.py`（16+ 个方法）
  - 工单 CRUD：`create`, `get`, `list_cases`, `update`
  - 规格版本：`save_spec`, `_compute_hash`, `_collect_based_on`
  - 确认与拒绝：`confirm_spec`, `reject_spec`
  - Stale 检测：`check_stale`, `_get_upstream_kinds`
  - Rebase：`rebase`
  - 辅助查询：`get_confirmed_spec`, `get_latest_draft`, `list_specs`
  - 引用管理：`add_link`, `list_links`

**核心逻辑**：
- **内容哈希幂等**：相同 `content_hash` 不创建新 revision
- **乐观锁**：确认时必须提供正确 hash
- **上游依赖追踪**：`based_on_json` 记录依赖版本
- **失效矩阵**：上游变化自动标记下游 `stale`
- **阶段推进**：确认规格后自动推进工单 stage

---

### ✅ P1-4：API 与 RBAC

**交付物**：
- `backend/app/api/modeling.py`（13 个端点）
  - 工单：`POST/GET/PATCH /api/modeling-cases`
  - 规格：`POST/GET /api/modeling-cases/{id}/specs/{kind}`
  - Diff：`GET /api/modeling-cases/{id}/specs/{kind}/diff`
  - 校验：`POST /api/modeling-cases/{id}/specs/{kind}/{revision}/validate`
  - 确认/拒绝：`POST .../confirm`, `POST .../reject`
  - Rebase：`POST /api/modeling-cases/{id}/rebase`
  - 影响分析：`GET /api/modeling-cases/{id}/impact`
  - 引用：`GET /api/modeling-cases/{id}/links`
- 注册到 `app/api/router.py`

**权限控制**：
- GET：`require_role(Role.READER)`
- POST/PATCH（非确认）：`require_role(Role.EDITOR)`
- confirm/reject：`require_role(Role.REVIEWER)`
- compile/execute（P5）：`require_role(Role.PUBLISHER)`

---

### ✅ P1-5：测试

**交付物**：
- `backend/tests/test_modeling_case.py`（7 个测试）
  - `test_create_modeling_case`：创建与查询
  - `test_save_and_confirm_requirement_spec`：需求确认与阶段推进
  - `test_spec_content_hash_idempotent`：哈希幂等
  - `test_optimistic_lock`：乐观锁冲突检测
  - `test_stale_detection`：上游变化导致下游 stale
  - `test_list_and_filter`：列表筛选
  - `test_rbac_enforcement`：RBAC 权限
- 修复 `backend/tests/conftest.py`：新增 `db` fixture

**测试结果**：
```text
7 passed, 1 warning in 2.49s
```

---

### ✅ 额外交付：维度模型扩展 Schemas（P3 预备）

**交付物**：
- `backend/app/schemas/dimensional_model.py`
  - `MeasureSpec`：度量与可加性
  - `DimensionSpec`：维度、SCD、代理键
  - `FactTableSpec`：事实表、粒度、业务过程
  - `BridgeTableSpec`：桥接表
  - `RolePlayingDimensionSpec`：角色扮演维度
  - `ConformedDimensionSpec`：一致性维度
  - `DimensionalModelSpecV2`：完整 Kimball 范式
- `backend/app/services/dimensional_model_validator.py`
  - 粒度一致性校验
  - 度量可加性校验
  - SCD 配置完整性
  - 维度引用校验
  - 角色扮演与一致性维度校验

---

## 当前基线

### 数据库

```text
Tables: modeling_cases, modeling_case_specs, modeling_case_links
Indexes: 完整
Migration: 6ac2622d9b62 (head & current)
```

### 代码统计

```text
Models: 3 classes + 6 enums
Schemas: 18+ classes
Service: 16+ methods
API: 13 endpoints
Tests: 7 tests (all passed)
```

### 测试覆盖

- ✅ 工单创建与查询
- ✅ 规格版本与 hash 幂等
- ✅ 确认与阶段推进
- ✅ 乐观锁冲突检测
- ✅ 上游依赖与 stale 检测
- ✅ RBAC 权限控制

---

## P1 退出条件核对

| 条件 | 状态 | 证据 |
|---|:---:|---|
| 数据模型与迁移 | ✅ | 3 张表 + 迁移已应用 |
| Pydantic schema | ✅ | 6 类 spec + API schemas |
| 服务层 revision/hash | ✅ | 内容哈希幂等 |
| 服务层 confirm/reject | ✅ | 乐观锁 + 阶段推进 |
| 服务层 stale/rebase | ✅ | 上游追踪 + 失效检测 |
| API CRUD + diff + validate | ✅ | 13 个端点 |
| RBAC 集成 | ✅ | reader/editor/reviewer 分级 |
| 前端骨架 | ⚠️ | **尚未开始** |
| 测试 | ✅ | 7/7 passed |

---

## 尚未完成

### P1-6：前端列表/详情骨架

**需要**：
- `frontend/src/pages/ModelingCasesPage.tsx`：列表页
- `frontend/src/pages/ModelingCasePage.tsx`：详情页
- `frontend/src/api.ts`：API 客户端扩展
- `frontend/src/types.ts`：TypeScript 类型
- 路由注册

**预计时间**：2～3 小时

---

## 验证命令

```bash
# 后端测试
cd backend && .venv/bin/pytest tests/test_modeling_case.py -v

# 迁移状态
cd backend && .venv/bin/alembic current

# 表验证
cd backend && sqlite3 ontometa.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'modeling_%';"

# API 可用性（需启动服务）
curl -H "X-Admin-Token: test-admin-token" http://localhost:8000/api/modeling-cases
```

---

## 下一步：P1-6 前端骨架

建议快速完成前端骨架，然后进入 P2（对话需求确认）。

**核心页面需求**：
1. 列表页：展示工单、stage、stale 提示、筛选
2. 详情页：左侧阶段导航、中间 spec 编辑/diff、右侧对话与决策
3. 确认按钮：调用 confirm API，乐观锁处理
4. Rebase 提示：检测到 stale 时显示

**简化策略**：
- 第一版用表单编辑 JSON（P3 再做结构化 UI）
- Diff 先展示两个 JSON（P3 再做结构化 diff）
- 决策记录暂不集成（P2 统一处理）

---

**P1 核心骨架已完成 85%，后端与服务层全部就绪！**
