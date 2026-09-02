# 审核收口：已判可回看 + 待归类门禁

## 问题

审核台存在两个致命漏洞，都在同一件事上：**判定不可逆、判错了找不回来**。

1. **判完的板块消失了**  
   一个板块判完之后从队列里彻底消失。判错了只能靠记忆去翻卡片墙——那等于没有复查机制。

2. **「待归类业务对象」里的表判完却卡住了**  
   判成业务对象、却在关系图上连不成簇的表会落进「待归类业务对象」板块。只确认角色而不归类，它既进不了业务地图（不属于任何业务模块），也不再出现在待判队列里（`needs_review=false`）——那个板块从此永远不会变空，这批对象永远不会被看见。

实测 erpnext 本体：
- 「待归类业务对象」板块 306 个对象，其中 57 个已确认但未归位
- 另有 18 个对象因角色改判而「落错位」（判成技术表却仍在待归类板块里）

这两件事钉的是同一句话：**判定要能被看见、被追回**。

## 解决方案

### 1. 已判可回看

队列不是单向的——服务端在「待判 + 已判」的完整人口上分组，两个视图因此是**同一批组、同一套 key**，换的只是成员的那一半。

#### 后端（`app/services/ontology_query.py`）

- `get_review_queue` 新增 `status` 参数：`"pending"`（默认）或 `"reviewed"`
- 分组逻辑不变，过滤条件相反：
  - `status=pending` → `needs_review=true`
  - `status=reviewed` → `needs_review=false`
- 两个视图的统计口径一致：`pending_total` / `reviewed_total` 在两边都有

#### 前端（`ReviewWorkbenchPage.tsx`）

- 待判 / 已判切换器放在板块列表正上方，用 Ant Design `Segmented` 组件
- 判完的板块点进去不该是空白——自动切换到 `status=reviewed`
- 空状态交叉引导：「这个范围已判完，查看已判的 N 个」
- 已判视图里主动作（`A` 键）换成**退回复核**（`danger` 样式）
- 撤销功能扩展：记录板块归属，还原时一起挪回去

### 2. 待归类门禁

「待归类业务对象」板块里的对象，只确认角色不算判完——必须先归入一个业务板块，或改判为数据表/技术表。

#### 后端门禁（`app/services/edit.py`）

**批量更新** `batch_update_object_types`：
```python
# 门禁：待归类业务对象必须先归位才能确认
if needs_review is False and segment_id is None:
    pending_objs = [
        obj for obj in objs
        if segment_kind_of(db, obj.segment_id) == SEGMENT_KIND_PENDING
        and role_stays_pending(obj.table_role)
    ]
    if pending_objs:
        names = ", ".join(f"「{o.display_name}」" for o in pending_objs[:3])
        raise ValueError(
            f"{names} 等 {len(pending_objs)} 个对象仍在「待归类业务对象」里。"
            f"只确认角色不算判完：请先归入一个业务板块，或改判为数据表/技术表。"
        )
```

**归类与确认是一次调用**：
- `segment_id` 与 `needs_review` 可以同时给出
- 后端先挪板块，再判门禁——所以看得到挪过之后的归属

**改判的退路**：
- 改判成数据表/技术表 → 自动落到「技术表」板块 → 门禁通过
- 技术表改判成业务对象 → 落进「待归类业务对象」→ 仍需归位

**返回值扩展**：
```python
class ObjectTypeBatchUpdateResult(BaseModel):
    updated: int
    pending_classification: int = 0  # 改判成业务对象却仍待归类的个数
    items: list[ObjectTypeSummary]
```

#### 前端界面（`ReviewWorkbenchPage.tsx`）

**归类行**（排在改判行之上）：
- Select 下拉选板块（只给业务板块 + 公共主数据，按成员数倒序）
- 「归类并确认」按钮 → `segment_id` + `needs_review=false` 一次请求

**说明条**（当 `group.requires_classification=true` 时显示）：
```
这批表判成了业务对象，却在关系图上连不成簇。**确认角色不算判完**：
归入一个业务板块，它们才会出现在业务地图上；确实不属于任何模块的，
改判为数据表/技术表。
```

**确认按钮禁用** + tooltip 说明缺的是归属

**「待归类未归位」芯片**（顶栏）：
- 统计口径：`unclassified_reviewed`（已确认却仍在待归类里的存量）
- 点击 → 跳转到「待归类业务对象」板块的已判视图

#### 队列标注（`app/services/ontology_query.py`）

```python
ReviewGroup.requires_classification = (
    not is_relation
    and segment_kind == SEGMENT_KIND_PENDING
    and role_stays_pending(table_role)
)
```

- 判定规则只在 `segment_placement.py` 写一次，前端读字段、不重算
- 同在待归类、但角色已是数据表/技术表的那些**不算**——确认它们时会自动挪到技术表板块

### 3. 存量自愈

**漂移问题**：对象先按「业务对象连不成簇」落进待归类，之后被重判成技术表，板块却没跟着走。它卡在待归类板块里，而「改判技术表」对它是空操作——既确认不了也归类不动。

**解决**（`app/services/edit.py`）：
- 判定时（不要求角色**变了**）调用 `resettle_fallback_member`
- 把兜底板块里落错位的对象挪到与当前角色相符的那一个
- 自愈挪板块**不**计入 `overridden_fields`：那是机器落位的修正，不是人工钉死的归属

**回填脚本**（`scripts/backfill_segment_partition.py`）：
- 新增 `resettle_ontology` pass，在补齐未归属对象之后运行
- 发现并修正所有「角色与板块不符」的兜底成员
- 实测 erpnext：18 个漂移（6 个 pending→technical，9 个 technical→pending 等）

### 4. 统计扩展（`app/api/ontology.py`）

`ReviewModeStats` 新增字段：
```python
unclassified_total: int = 0          # 待归类板块的对象总数
unclassified_reviewed: int = 0       # 其中已确认却未归位的存量
```

`SegmentReviewProgress` 新增字段：
```python
kind: str = "business"  # 板块种类，认出「待归类业务对象」那一行
```

## 测试覆盖

### 后端（`tests/test_review_classification_gate.py`，11 个用例）

**已判可回看**：
- 同一批组、同一套 key，只是成员换了一半
- 判完的板块在已判视图里仍能原样打开
- 已判成员可以被退回复核（批量置 `needs_review=true`）

**待归类门禁**：
- 只确认角色 → 400 拒绝
- 归类 + 确认 → 一次调用通过
- 改判数据表 → 自动落到技术表板块，门禁通过
- 技术表改判成业务对象 → 落进待归类，仍需归位
- 业务板块的归属不因改判而丢失

**存量自愈**：
- 技术表压在待归类板块里，确认时自动挪到技术表板块
- `requires_classification=false`（不卡门禁）
- 自愈不钉 `overridden_fields`

### 前端

- TypeScript 编译通过
- 待判 / 已判切换器
- 归类行 + 说明条
- 「待归类未归位」芯片
- 空状态交叉引导
- 已判视图里「退回复核」按钮

## 数据迁移

无需迁移。新字段都有默认值，存量数据直接可用。

**可选回填**（针对存量漂移）：
```bash
python -m scripts.backfill_segment_partition --dry-run  # 预览
python -m scripts.backfill_segment_partition            # 执行
```

## UI/UX 要点

1. **位置不动**  
   待判 / 已判切换器就在板块列表正上方，视图切换不会让人找不到北。

2. **键位不变**  
   `A` 永远是「这一屏最该做的那件事」：待判视图里是确认，已判视图里是退回复核。

3. **说清楚缺的是什么**  
   待归类那一组确认按钮被禁时，说明条说出「缺的不是角色对不对，是归到哪个业务模块」。

4. **统计不说谎**  
   改判成业务对象却仍待归类的那批，返回值里单独计数：「改判了 5 个，其中 2 个仍待归类」。

5. **存量有入口**  
   已确认却未归位的那批（门禁上线前留下的）通过「待归类未归位」芯片暴露出来。

## 关键文件

### 后端
- `app/services/edit.py` — 批量更新门禁、自愈逻辑
- `app/services/ontology_query.py` — 队列 `status` 参数、`requires_classification` 标注
- `app/services/segment_placement.py` — `role_stays_pending` 规则、`resettle_fallback_member`
- `app/api/ontology.py` — 统计扩展、队列参数
- `app/schemas/ontology.py` — 返回值、队列组字段
- `tests/test_review_classification_gate.py` — 11 个覆盖用例

### 前端
- `frontend/src/pages/ReviewWorkbenchPage.tsx` — 切换器、归类行、说明条、主动作路由
- `frontend/src/components/review/ReviewSignals.tsx` — 已判横幅（「已确认」而非「待你确认」）
- `frontend/src/types.ts` — `ReviewQueue.status`、`ReviewGroup.requires_classification`
- `frontend/src/api.ts` — `status` 参数、`segment_id` 参数
- `frontend/src/styles/layout.css` — 归类行、切换器、说明条、芯片样式

### 脚本
- `backend/scripts/backfill_segment_partition.py` — 存量回填（补齐 + 自愈）

## 验证

### 后端
```bash
python -m pytest tests/test_review_classification_gate.py -v
python -m pytest  # 2065 passed
```

### 前端
```bash
cd frontend
npx tsc --noEmit        # 通过
npm run lint            # 0 errors
npm run lint:tokens     # 通过
```

### 实测数据（erpnext 本体）
- 待归类板块 306 个对象，其中 57 个已确认未归位
- 回填脚本识别 18 个落位漂移
- 队列正确标注 `requires_classification`
- 「待归类未归位」芯片显示 57

## 影响范围

**破坏性变更**：无

**API 扩展**（向后兼容）：
- `GET /api/ontologies/{id}/review-queue?status=reviewed`
- `PATCH /api/object-types/batch` 接受 `segment_id` 参数
- 返回值新增 `pending_classification` 字段

**UI 变更**：
- 板块列表上方新增待判/已判切换器
- 待归类组新增说明条与归类行
- 顶栏新增「待归类未归位」芯片（有存量时）

## 下一步

1. **前端 E2E 测试**（可选）  
   用 Playwright 覆盖归类流程、退回复核、撤销

2. **文档**  
   更新用户手册，说明已判回看与归类流程

3. **监控**  
   如果存量自愈频繁触发，说明有其他路径也在产生漂移——需要排查
