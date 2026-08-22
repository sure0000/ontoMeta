# P2 执行计划：对话需求确认与 Agent 集成

> 目标：让 Agent 在对话中创建建模工单、澄清需求、保存规格、推进阶段  
> 预计时间：3～4 天  
> 前置条件：P1 核心后端完成 ✅

---

## P2-1：扩展 Chat BI 工具集（0.5 天）

### 新增工具

```python
# backend/app/services/chat_bi_tool_schemas.py

propose_modeling_case = {
    "name": "propose_modeling_case",
    "description": "提议创建建模工单，记录用户需求和建模目标",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "工单标题"},
            "business_goal": {"type": "string", "description": "业务目标"},
            "analysis_scope": {"type": "string", "description": "分析范围"},
            "primary_subject": {"type": "string", "description": "主体对象"},
            "time_range": {"type": "string", "description": "时间范围"},
            "domain_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "business_goal"],
    },
}

update_requirement_spec = {
    "name": "update_requirement_spec",
    "description": "更新建模工单的需求规格",
    "input_schema": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "business_goal": {"type": "string"},
            "analysis_scope": {"type": "string"},
            "primary_subject": {"type": "string"},
            "grain": {"type": "string"},
            "time_range": {"type": "string"},
            "metrics": {"type": "array", "items": {"type": "object"}},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "deliverables": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["case_id"],
    },
}

confirm_requirement = {
    "name": "confirm_requirement",
    "description": "确认需求规格，推进到本体确认阶段",
    "input_schema": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "confirmed_by": {"type": "string"},
        },
        "required": ["case_id", "confirmed_by"],
    },
}

propose_ontology_selection = {
    "name": "propose_ontology_selection",
    "description": "提议本次建模使用的本体和数据对象",
    "input_schema": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "domain_ids": {"type": "array", "items": {"type": "string"}},
            "object_refs": {"type": "array", "items": {"type": "object"}},
            "relation_refs": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["case_id"],
    },
}
```

### 工具实现

```python
# backend/app/services/chat_bi.py

async def _dispatch_propose_modeling_case(self, args: dict[str, Any]) -> dict:
    """创建建模工单。"""
    from app.services.modeling_case import ModelingCaseService
    from app.schemas.modeling import ModelingCaseCreate
    
    case_input = ModelingCaseCreate(
        title=args["title"],
        conversation_id=self.conversation_id,
        primary_domain_id=args.get("primary_domain_id"),
        domain_ids=args.get("domain_ids", []),
    )
    
    case = ModelingCaseService.create(self.db, case_input)
    
    # 立即保存初始需求
    if args.get("business_goal"):
        requirement_payload = {
            "business_goal": args["business_goal"],
            "analysis_scope": args.get("analysis_scope"),
            "primary_subject": args.get("primary_subject"),
            "time_range": args.get("time_range"),
        }
        
        ModelingCaseService.save_spec(
            self.db,
            case.id,
            "requirement",
            requirement_payload,
        )
    
    return {
        "case_id": case.id,
        "title": case.title,
        "stage": case.stage,
        "message": f"已创建建模工单：{case.title}",
    }

async def _dispatch_update_requirement_spec(self, args: dict[str, Any]) -> dict:
    """更新需求规格。"""
    from app.services.modeling_case import ModelingCaseService
    
    case_id = args.pop("case_id")
    
    # 获取当前草稿或创建新的
    current = ModelingCaseService.get_latest_draft(self.db, case_id, "requirement")
    payload = current.payload if current else {}
    
    # 合并更新
    payload.update({k: v for k, v in args.items() if v is not None})
    
    spec = ModelingCaseService.save_spec(
        self.db,
        case_id,
        "requirement",
        payload,
    )
    
    return {
        "case_id": case_id,
        "revision": spec.revision,
        "status": spec.status,
        "message": "需求规格已更新",
    }

async def _dispatch_confirm_requirement(self, args: dict[str, Any]) -> dict:
    """确认需求规格。"""
    from app.services.modeling_case import ModelingCaseService
    
    case_id = args["case_id"]
    confirmed_by = args["confirmed_by"]
    
    # 获取最新草稿
    draft = ModelingCaseService.get_latest_draft(self.db, case_id, "requirement")
    if not draft:
        raise ValueError("没有待确认的需求规格")
    
    # 确认
    spec = ModelingCaseService.confirm_spec(
        self.db,
        case_id,
        "requirement",
        draft.revision,
        confirmed_by,
        draft.content_hash,
    )
    
    # 获取更新后的工单
    case = ModelingCaseService.get(self.db, case_id)
    
    return {
        "case_id": case_id,
        "revision": spec.revision,
        "stage": case.stage,
        "message": f"需求已确认，进入 {case.stage} 阶段",
    }
```

---

## P2-2：会话上下文绑定（0.5 天）

### 目标
在 ChatBiService 初始化时检测是否有关联的建模工单，并在整个会话中维护工单上下文。

### 实现

```python
# backend/app/services/chat_bi.py

class ChatBiService:
    def __init__(self, db: Session, conversation_id: str, user_subject_id: str):
        self.db = db
        self.conversation_id = conversation_id
        self.user_subject_id = user_subject_id
        
        # 查找关联的建模工单
        self.modeling_case = self._find_modeling_case()
    
    def _find_modeling_case(self):
        """查找当前会话关联的建模工单。"""
        from app.services.modeling_case import ModelingCaseService
        
        cases = ModelingCaseService.list_cases(
            self.db,
            conversation_id=self.conversation_id,
            limit=1,
        )
        
        return cases[0] if cases else None
    
    def _get_system_context(self) -> str:
        """构建系统上下文，包含工单状态。"""
        base_context = super()._get_system_context()
        
        if self.modeling_case:
            case_context = f"""
当前建模工单：
- 标题：{self.modeling_case.title}
- 阶段：{self.modeling_case.stage}
- 版本：{self.modeling_case.current_revision}
"""
            
            # 获取已确认的规格
            confirmed = ModelingCaseService.get_confirmed_spec(
                self.db,
                self.modeling_case.id,
                "requirement",
            )
            
            if confirmed:
                case_context += f"\n已确认需求：\n{json.dumps(confirmed.payload, indent=2, ensure_ascii=False)}\n"
            
            return base_context + "\n" + case_context
        
        return base_context
```

---

## P2-3：需求澄清增强（1 天）

### 目标
Agent 在创建工单后，主动澄清关键信息：
- 业务目标（必须）
- 分析粒度（必须）
- 主体对象（必须）
- 时间范围
- 度量需求
- 维度需求
- 交付形式

### 实现策略

```python
# backend/app/agents/requirement_clarifier.py

class RequirementClarifier:
    """需求澄清助手。"""
    
    REQUIRED_FIELDS = [
        "business_goal",
        "primary_subject",
        "grain",
    ]
    
    OPTIONAL_FIELDS = [
        "analysis_scope",
        "time_range",
        "metrics",
        "dimensions",
        "deliverables",
    ]
    
    def check_completeness(self, requirement: dict) -> list[str]:
        """检查需求完整性，返回缺失字段。"""
        missing = []
        
        for field in self.REQUIRED_FIELDS:
            if not requirement.get(field):
                missing.append(field)
        
        return missing
    
    def generate_clarification_prompt(self, missing: list[str]) -> str:
        """生成澄清提示。"""
        field_prompts = {
            "business_goal": "请说明业务目标：您希望通过这次分析解决什么问题？",
            "primary_subject": "请明确分析主体：是订单、客户、商品，还是其他？",
            "grain": "请明确分析粒度：每条记录代表什么？（如：每笔订单、每个客户每天、每个SKU每月）",
            "time_range": "请说明时间范围：分析多久的数据？",
            "metrics": "请列举关键指标：您需要看哪些度量？（如：销售额、数量、增长率）",
            "dimensions": "请列举分析维度：您需要按什么维度切片？（如：地区、渠道、类目）",
        }
        
        prompts = [field_prompts.get(f, f"请补充：{f}") for f in missing]
        return "\n".join(prompts)
```

---

## P2-4：决策账本集成（0.5 天）

### 目标
将建模工单的关键决策记录到 `chat_bi_ledger`。

### 实现

```python
# backend/app/services/chat_bi.py

async def _dispatch_propose_modeling_case(self, args: dict[str, Any]) -> dict:
    # ... 创建工单 ...
    
    # 记录决策
    self._log_decision(
        "create_modeling_case",
        {
            "case_id": case.id,
            "title": case.title,
        },
        "工单已创建，等待需求确认",
    )
    
    return result

async def _dispatch_confirm_requirement(self, args: dict[str, Any]) -> dict:
    # ... 确认需求 ...
    
    # 记录决策
    self._log_decision(
        "confirm_requirement",
        {
            "case_id": case_id,
            "revision": spec.revision,
            "confirmed_by": confirmed_by,
        },
        f"需求已确认，进入 {case.stage} 阶段",
    )
    
    return result
```

---

## P2-5：前端快速集成（1 天）

### 在对话界面显示工单提案

```typescript
// frontend/src/pages/chat-bi/ChatBiReferences.tsx

interface ModelingCaseProposal {
  type: "modeling_case";
  case_id: string;
  title: string;
  stage: string;
  requirement?: any;
}

function ModelingCaseCard({ proposal }: { proposal: ModelingCaseProposal }) {
  return (
    <div className="border rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <h4 className="font-medium">建模工单</h4>
        <Badge>{proposal.stage}</Badge>
      </div>
      
      <p className="text-sm text-gray-600 mb-3">{proposal.title}</p>
      
      {proposal.requirement && (
        <div className="text-xs bg-gray-50 p-2 rounded mb-3">
          <pre>{JSON.stringify(proposal.requirement, null, 2)}</pre>
        </div>
      )}
      
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => {
            // 调用确认 API
            api.confirmRequirement(proposal.case_id, currentUser)
              .then(() => toast.success("需求已确认"))
          }}
        >
          确认需求
        </Button>
        
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            window.open(`/modeling-cases/${proposal.case_id}`, "_blank")
          }}
        >
          查看详情
        </Button>
      </div>
    </div>
  );
}
```

---

## P2 退出条件

- [ ] Agent 可在对话中创建建模工单
- [ ] Agent 可澄清并收集完整需求
- [ ] Agent 可保存需求规格
- [ ] 用户可在对话中确认需求
- [ ] 确认后工单阶段自动推进到 `ontology_confirmation`
- [ ] 决策记录到账本
- [ ] 对话界面展示工单提案卡片
- [ ] 测试覆盖完整流程

---

## 预计时间

- P2-1：0.5 天
- P2-2：0.5 天
- P2-3：1 天
- P2-4：0.5 天
- P2-5：1 天
- 测试与修复：0.5 天

**总计：4 天**

---

**开始执行 P2-1：扩展 Chat BI 工具集**
