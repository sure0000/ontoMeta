# MCP 安全性设计

## 安全挑战

MCP 架构下的安全挑战与当前 Data Agent 有本质不同：

| 维度 | 当前 Data Agent | MCP 架构 | 挑战 |
|------|----------------|----------|------|
| **调用入口** | 单一（/api/chat-bi/ask） | 多个（每个工具） | 攻击面扩大 |
| **调用者** | 前端（已鉴权） | 通用 agent + 前端 + 外部应用 | 来源多样 |
| **权限控制** | 隐式（session） | 显式（每个工具） | 需精细控制 |
| **审计** | 集中式 | 分散式 | 需统一机制 |

---

## 安全架构

### 多层防御体系

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 层：网络层安全（Network Security）                      │
├─────────────────────────────────────────────────────────────┤
│ - TLS/HTTPS 加密                                             │
│ - API Rate Limiting                                          │
│ - DDoS 防护                                                  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 2 层：认证层（Authentication）                            │
├─────────────────────────────────────────────────────────────┤
│ - Session Token（前端）                                      │
│ - API Key（外部应用）                                        │
│ - MCP Client ID（通用 agent）                               │
│ - 身份验证失败 → 403 Forbidden                              │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 3 层：授权层（Authorization）                             │
├─────────────────────────────────────────────────────────────┤
│ - RBAC（Role-Based Access Control）                         │
│ - 资源级权限（对象所有权、本体权限）                         │
│ - 操作级权限（读 vs 写）                                     │
│ - 授权失败 → 403 Forbidden + 详细原因                       │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 4 层：业务规则层（Business Rules）                        │
├─────────────────────────────────────────────────────────────┤
│ - 参数校验（类型、范围、格式）                               │
│ - 业务规则校验（治理规约、约束条件）                         │
│ - 状态机校验（如：未确认任务不能执行）                       │
│ - 校验失败 → 400 Bad Request + violations                   │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 5 层：数据隔离层（Data Isolation）                        │
├─────────────────────────────────────────────────────────────┤
│ - 租户隔离（如果多租户）                                     │
│ - 本体隔离（用户只能看到有权限的本体）                       │
│ - 数据源隔离（用户只能访问授权的数据源）                     │
│ - 越界访问 → 404 Not Found（而非 403）                      │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 6 层：审计层（Audit）                                     │
├─────────────────────────────────────────────────────────────┤
│ - 所有工具调用记入审计日志                                   │
│ - 包含：who, what, when, where, why, result                 │
│ - 失败的尝试也记录（安全事件）                               │
│ - 审计日志不可篡改（append-only）                           │
└─────────────────────────────────────────────────────────────┘
```

---

## 具体实现

### 1. 认证层（Authentication）

#### 1.1 调用方类型

| 调用方 | 认证方式 | 凭证传递 | 用例 |
|--------|---------|---------|------|
| **前端（已登录用户）** | Session Token | Cookie / Authorization Header | 正常使用 |
| **通用 Agent（本地）** | 无需认证（localhost） | - | Claude Desktop / VS Code |
| **通用 Agent（远程）** | MCP Client ID | MCP Protocol Header | Claude Code（云端） |
| **外部应用** | API Key | Authorization Header | 第三方集成 |

#### 1.2 实现代码

**MCP 服务端**：`backend/app/mcp/auth.py`

```python
from fastapi import HTTPException, Depends, Header
from sqlalchemy.orm import Session
from typing import Optional

class AuthContext:
    """认证上下文"""
    def __init__(
        self,
        user_id: str | None,
        session_id: str | None,
        client_type: str,  # "frontend" | "mcp_local" | "mcp_remote" | "api"
        client_id: str | None,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.client_type = client_type
        self.client_id = client_id
    
    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None
    
    @property
    def is_local_mcp(self) -> bool:
        return self.client_type == "mcp_local"

async def authenticate_request(
    authorization: Optional[str] = Header(None),
    cookie: Optional[str] = Header(None),
    x_mcp_client: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    认证请求，返回认证上下文
    
    支持多种认证方式：
    1. Session Token（Cookie）- 前端用户
    2. API Key（Authorization Header）- 外部应用
    3. MCP Client ID（X-MCP-Client Header）- 远程 agent
    4. Localhost（无需认证）- 本地 agent
    """
    
    # 1. 前端用户（Session）
    if cookie:
        session = validate_session_cookie(cookie, db)
        if session:
            return AuthContext(
                user_id=session.user_id,
                session_id=session.id,
                client_type="frontend",
                client_id=None,
            )
    
    # 2. 外部应用（API Key）
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
        app = validate_api_key(api_key, db)
        if app:
            return AuthContext(
                user_id=app.owner_id,
                session_id=None,
                client_type="api",
                client_id=app.id,
            )
    
    # 3. 远程 MCP Agent（Client ID）
    if x_mcp_client:
        client = validate_mcp_client(x_mcp_client, db)
        if client:
            return AuthContext(
                user_id=client.user_id,
                session_id=None,
                client_type="mcp_remote",
                client_id=client.id,
            )
    
    # 4. 本地 MCP Agent（Localhost，无需认证）
    # TODO: 检查请求是否来自 localhost
    # 如果来自 localhost，信任它（本地 Claude Desktop / VS Code）
    if is_localhost_request():
        # 本地 MCP 需要用户在首次使用时选择身份
        # 这里返回未认证上下文，具体工具可以要求身份
        return AuthContext(
            user_id=None,
            session_id=None,
            client_type="mcp_local",
            client_id=None,
        )
    
    # 未认证
    raise HTTPException(
        status_code=401,
        detail="Authentication required"
    )
```

#### 1.3 本地 MCP 的身份选择

**问题**：本地 MCP（Claude Desktop）如何知道"我是谁"？

**方案**：首次使用时，通过特殊工具选择身份

```python
@register_tool
class SelectIdentityTool:
    """
    本地 MCP 专用：选择操作身份
    
    当 client_type=mcp_local 且 user_id=None 时，
    其他工具会返回 401，提示先调用此工具。
    """
    name = "select_identity"
    description = "选择操作身份（仅本地 MCP）"
    input_schema = {
        "type": "object",
        "properties": {
            "user_email": {
                "type": "string",
                "description": "用户邮箱"
            }
        },
        "required": ["user_email"]
    }
    
    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        if not auth.is_local_mcp:
            return ToolResult(
                success=False,
                error="This tool is only for local MCP clients"
            )
        
        # 查找用户
        user = db.query(User).filter_by(email=arguments["user_email"]).first()
        if not user:
            return ToolResult(
                success=False,
                error=f"User not found: {arguments['user_email']}"
            )
        
        # 生成临时 session token，存储在本地
        # 后续请求携带此 token
        token = generate_local_mcp_token(user.id)
        
        return ToolResult(
            success=True,
            data={
                "user_id": user.id,
                "user_name": user.name,
                "token": token,
                "message": "Identity selected. Please include this token in future requests."
            }
        )
```

**用户体验**：
```
用户（在 Claude Desktop）: "查询所有本体"
Claude: "我需要先知道你的身份。请提供你的邮箱。"
用户: "user@example.com"
Claude: [调用 select_identity 工具] "身份确认为 张三。现在查询所有本体..."
```

### 2. 授权层（Authorization）

#### 2.1 RBAC 模型

**角色定义**：

```python
# backend/app/models/rbac.py

class Role(Enum):
    ADMIN = "admin"              # 系统管理员
    DOMAIN_OWNER = "domain_owner"  # 域所有者
    MODELER = "modeler"          # 建模师
    ANALYST = "analyst"          # 分析师
    VIEWER = "viewer"            # 只读用户

class Permission(Enum):
    # 本体权限
    ONTOLOGY_CREATE = "ontology:create"
    ONTOLOGY_READ = "ontology:read"
    ONTOLOGY_UPDATE = "ontology:update"
    ONTOLOGY_DELETE = "ontology:delete"
    ONTOLOGY_PUBLISH = "ontology:publish"
    
    # 任务权限
    TASK_CREATE = "task:create"
    TASK_READ = "task:read"
    TASK_UPDATE = "task:update"
    TASK_DELETE = "task:delete"
    TASK_EXECUTE = "task:execute"
    
    # 数据源权限
    DATASOURCE_READ = "datasource:read"
    DATASOURCE_CONNECT = "datasource:connect"
    DATASOURCE_QUERY = "datasource:query"
    
    # 治理权限
    GOVERNANCE_READ = "governance:read"
    GOVERNANCE_UPDATE = "governance:update"

# 角色 → 权限映射
ROLE_PERMISSIONS = {
    Role.ADMIN: [p for p in Permission],  # 所有权限
    Role.DOMAIN_OWNER: [
        Permission.ONTOLOGY_CREATE,
        Permission.ONTOLOGY_READ,
        Permission.ONTOLOGY_UPDATE,
        Permission.ONTOLOGY_PUBLISH,
        Permission.TASK_CREATE,
        Permission.TASK_READ,
        Permission.TASK_UPDATE,
        Permission.TASK_EXECUTE,
        # ...
    ],
    Role.MODELER: [
        Permission.ONTOLOGY_READ,
        Permission.ONTOLOGY_UPDATE,
        Permission.TASK_CREATE,
        Permission.TASK_READ,
        # ...
    ],
    Role.ANALYST: [
        Permission.ONTOLOGY_READ,
        Permission.TASK_READ,
        Permission.DATASOURCE_QUERY,
        # ...
    ],
    Role.VIEWER: [
        Permission.ONTOLOGY_READ,
        Permission.TASK_READ,
        Permission.DATASOURCE_READ,
    ],
}
```

#### 2.2 工具级权限检查

**每个 MCP 工具声明所需权限**：

```python
@register_tool
class ProposeSync TaskTool:
    name = "propose_sync_task"
    description = "提议同步任务"
    
    # 声明所需权限
    required_permissions = [Permission.TASK_CREATE]
    
    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        # 1. 认证检查
        if not auth.is_authenticated:
            return ToolResult(
                success=False,
                error="Authentication required",
                metadata={"hint": "Call select_identity first (local MCP)"}
            )
        
        # 2. 授权检查
        if not has_permissions(auth.user_id, self.required_permissions):
            return ToolResult(
                success=False,
                error="Permission denied",
                metadata={
                    "required_permissions": [p.value for p in self.required_permissions],
                    "user_role": get_user_role(auth.user_id),
                }
            )
        
        # 3. 资源级权限检查
        target_datasource_id = arguments.get("target_datasource_id")
        if not can_access_datasource(auth.user_id, target_datasource_id):
            return ToolResult(
                success=False,
                error=f"No access to datasource: {target_datasource_id}"
            )
        
        # 4. 执行业务逻辑
        # ...
```

#### 2.3 资源级权限

**本体权限表**：

```python
# backend/app/models/permissions.py

class OntologyPermission(Base):
    """本体权限表"""
    __tablename__ = "ontology_permissions"
    
    ontology_id = Column(String, ForeignKey("ontologies.id"), primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    role = Column(Enum(Role))
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String, ForeignKey("users.id"))

class DataSourcePermission(Base):
    """数据源权限表"""
    __tablename__ = "datasource_permissions"
    
    datasource_id = Column(String, ForeignKey("data_sources.id"), primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    can_read = Column(Boolean, default=True)
    can_connect = Column(Boolean, default=False)
    can_query = Column(Boolean, default=False)
```

**权限检查函数**：

```python
def can_access_ontology(
    user_id: str,
    ontology_id: str,
    permission: Permission,
    db: Session
) -> bool:
    """检查用户是否有权限访问本体"""
    
    # 1. 检查用户全局角色
    user_role = get_user_role(user_id, db)
    if permission in ROLE_PERMISSIONS.get(user_role, []):
        return True
    
    # 2. 检查本体级权限
    ont_perm = db.query(OntologyPermission).filter_by(
        ontology_id=ontology_id,
        user_id=user_id
    ).first()
    
    if ont_perm:
        role_perms = ROLE_PERMISSIONS.get(ont_perm.role, [])
        if permission in role_perms:
            return True
    
    # 3. 检查继承权限（如：域所有者自动拥有该域下所有本体权限）
    ontology = db.query(Ontology).get(ontology_id)
    if ontology and ontology.domain_id:
        domain = db.query(Domain).get(ontology.domain_id)
        if domain and domain.owner_id == user_id:
            return True
    
    return False
```

### 3. 审计层（Audit）

#### 3.1 审计日志模型

```python
# backend/app/models/audit.py

class AuditLog(Base):
    """审计日志（不可变）"""
    __tablename__ = "audit_logs"
    
    id = Column(String, primary_key=True, default=generate_id)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Who
    user_id = Column(String, ForeignKey("users.id"), index=True)
    client_type = Column(String)  # "frontend" | "mcp_local" | "mcp_remote" | "api"
    client_id = Column(String)
    ip_address = Column(String)
    
    # What
    tool_name = Column(String, nullable=False, index=True)
    arguments = Column(JSON)
    
    # Result
    success = Column(Boolean, nullable=False, index=True)
    result_summary = Column(String)  # 简短摘要
    error = Column(String)
    
    # Context
    session_id = Column(String)
    conversation_id = Column(String)
    
    # Resources affected
    affected_resources = Column(JSON)  # [{"type": "ontology", "id": "xxx"}, ...]
    
    # Performance
    duration_ms = Column(Integer)
    
    # Security events
    is_security_event = Column(Boolean, default=False, index=True)
    security_reason = Column(String)
```

#### 3.2 自动审计

**所有工具调用自动记录**：

```python
# backend/app/mcp/server.py

@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any],
    auth: AuthContext = Depends(authenticate_request),
) -> list[types.TextContent]:
    """执行工具调用（带审计）"""
    
    start_time = time.time()
    success = False
    error = None
    result = None
    affected_resources = []
    is_security_event = False
    security_reason = None
    
    try:
        # 获取工具
        tool = TOOL_REGISTRY.get(name)
        if not tool:
            raise ValueError(f"Unknown tool: {name}")
        
        # 执行工具
        result = await tool.execute(arguments, auth)
        success = result.success
        error = result.error
        affected_resources = result.metadata.get("affected_resources", [])
        
        # 检查是否是安全事件
        if not success and result.metadata.get("is_security_event"):
            is_security_event = True
            security_reason = result.error
        
        return [types.TextContent(type="text", text=result.to_json())]
        
    except Exception as e:
        success = False
        error = str(e)
        is_security_event = True
        security_reason = "Unexpected exception"
        raise
        
    finally:
        # 记录审计日志（无论成功失败）
        duration_ms = int((time.time() - start_time) * 1000)
        
        audit_log = AuditLog(
            user_id=auth.user_id,
            client_type=auth.client_type,
            client_id=auth.client_id,
            tool_name=name,
            arguments=sanitize_arguments(arguments),  # 脱敏
            success=success,
            result_summary=result.data if success else None,
            error=error,
            session_id=auth.session_id,
            affected_resources=affected_resources,
            duration_ms=duration_ms,
            is_security_event=is_security_event,
            security_reason=security_reason,
        )
        
        db.add(audit_log)
        db.commit()
```

#### 3.3 安全事件监控

**实时监控异常行为**：

```python
# backend/app/services/security_monitor.py

class SecurityMonitor:
    """安全事件监控"""
    
    @staticmethod
    async def check_suspicious_activity(user_id: str, db: Session):
        """检查可疑活动"""
        
        # 最近 5 分钟的失败尝试
        recent_failures = db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.success == False,
            AuditLog.timestamp > datetime.utcnow() - timedelta(minutes=5)
        ).count()
        
        # 超过 10 次失败 → 锁定账户
        if recent_failures > 10:
            await lock_user_account(user_id, reason="Too many failed attempts")
            await notify_admin(f"User {user_id} locked due to suspicious activity")
        
        # 异常时间段（如：凌晨 2-5 点）
        if is_unusual_hour():
            await alert_admin(f"User {user_id} active at unusual hour")
        
        # 异常 IP（如：从未见过的 IP）
        # ...
```

### 4. 数据隔离

#### 4.1 查询级隔离

**所有查询自动加权限过滤**：

```python
# backend/app/services/ontology_query.py

class OntologyQueryService:
    def list_ontologies(
        self,
        user_id: str,
        include_unpublished: bool = False
    ) -> list[Ontology]:
        """查询本体列表（自动权限过滤）"""
        
        query = self.db.query(Ontology)
        
        # 1. 只返回用户有权限的本体
        if not is_admin(user_id):
            # 方案 A：JOIN 权限表
            query = query.join(OntologyPermission).filter(
                OntologyPermission.user_id == user_id
            )
            
            # 或方案 B：子查询
            accessible_ids = self._get_accessible_ontology_ids(user_id)
            query = query.filter(Ontology.id.in_(accessible_ids))
        
        # 2. 未发布本体需要额外权限
        if not include_unpublished:
            query = query.filter(Ontology.published == True)
        
        return query.all()
    
    def _get_accessible_ontology_ids(self, user_id: str) -> list[str]:
        """获取用户可访问的本体 ID 列表"""
        # 包含：
        # 1. 用户创建的
        # 2. 明确授权的
        # 3. 用户是域所有者的
        # ...
```

#### 4.2 多租户隔离（可选）

**如果支持多租户（SaaS 模式）**：

```python
# backend/app/models/tenant.py

class Tenant(Base):
    """租户"""
    __tablename__ = "tenants"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    plan = Column(String)  # "free" | "pro" | "enterprise"
    
    # 资源配额
    max_ontologies = Column(Integer)
    max_users = Column(Integer)
    max_storage_gb = Column(Integer)

# 所有表加租户字段
class Ontology(Base):
    # ...
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

# 所有查询自动加租户过滤
def get_current_tenant_id(auth: AuthContext) -> str:
    user = db.query(User).get(auth.user_id)
    return user.tenant_id

query = db.query(Ontology).filter_by(tenant_id=get_current_tenant_id(auth))
```

---

## 安全最佳实践

### 1. 敏感数据处理

```python
# 审计日志脱敏
def sanitize_arguments(arguments: dict) -> dict:
    """脱敏敏感参数"""
    sanitized = arguments.copy()
    
    # 脱敏字段
    sensitive_keys = ["password", "api_key", "secret", "token"]
    
    for key in sensitive_keys:
        if key in sanitized:
            sanitized[key] = "***REDACTED***"
    
    # 脱敏 SQL 中的敏感信息
    if "sql" in sanitized:
        sanitized["sql"] = redact_sql(sanitized["sql"])
    
    return sanitized
```

### 2. SQL 注入防护

```python
# execute_sql 工具的 SQL 白名单校验
def validate_sql(sql: str) -> tuple[bool, str | None]:
    """
    SQL 白名单校验
    
    Returns:
        (is_valid, error_message)
    """
    sql_lower = sql.lower().strip()
    
    # 1. 只允许 SELECT
    if not sql_lower.startswith("select"):
        return False, "Only SELECT statements are allowed"
    
    # 2. 禁止关键词
    forbidden = ["drop", "delete", "insert", "update", "alter", "create", "truncate"]
    for keyword in forbidden:
        if keyword in sql_lower:
            return False, f"Forbidden keyword: {keyword}"
    
    # 3. 禁止访问系统表
    forbidden_schemas = ["information_schema", "pg_catalog", "mysql", "sys"]
    for schema in forbidden_schemas:
        if schema in sql_lower:
            return False, f"Forbidden schema: {schema}"
    
    # 4. 使用 sqlparse 解析，确保语法正确
    try:
        parsed = sqlparse.parse(sql)
        if len(parsed) != 1:
            return False, "Multiple statements not allowed"
    except Exception as e:
        return False, f"SQL parse error: {e}"
    
    return True, None
```

### 3. Rate Limiting

```python
# backend/app/mcp/rate_limit.py

class RateLimiter:
    """工具调用速率限制"""
    
    @staticmethod
    async def check_rate_limit(
        user_id: str,
        tool_name: str,
        db: Session
    ) -> tuple[bool, str | None]:
        """
        检查速率限制
        
        限制策略：
        - 查询工具：100 次/分钟
        - 写工具：10 次/分钟
        - 本体建模工具：5 次/分钟
        """
        
        # 最近 1 分钟的调用次数
        recent_calls = db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.tool_name == tool_name,
            AuditLog.timestamp > datetime.utcnow() - timedelta(minutes=1)
        ).count()
        
        # 获取限制
        limit = get_tool_rate_limit(tool_name)
        
        if recent_calls >= limit:
            return False, f"Rate limit exceeded: {limit} calls/minute"
        
        return True, None
```

---

## 与当前 Data Agent 的对比

| 安全维度 | 当前 Data Agent | MCP 架构 | 改进 |
|---------|----------------|----------|------|
| **认证** | Session（单一） | 多方式（Session/API Key/MCP Client） | ✅ 更灵活 |
| **授权** | 隐式（session 级） | 显式（工具 + 资源级） | ✅ 更精细 |
| **审计** | 部分记录 | 全量自动记录 | ✅ 更完整 |
| **SQL 注入** | 有防护 | 增强防护 | ✅ 更安全 |
| **数据隔离** | 基于 session | 基于权限表 + 查询级过滤 | ✅ 更严格 |
| **监控** | 无主动监控 | 实时安全事件监控 | ✅ 更主动 |

---

## 实施优先级

### Phase 1（必须）
- ✅ 认证层（多方式认证）
- ✅ 授权层（RBAC + 资源级权限）
- ✅ 审计层（自动记录）
- ✅ SQL 白名单

### Phase 2（重要）
- ⬜ 安全事件监控
- ⬜ Rate Limiting
- ⬜ 敏感数据脱敏
- ⬜ 查询级数据隔离

### Phase 3（可选）
- ⬜ 多租户隔离
- ⬜ 高级监控和告警
- ⬜ 安全审计报告

---

**结论**：MCP 架构的安全性可以做到**比当前 Data Agent 更好**，关键是：
1. 多层防御
2. 显式权限
3. 全量审计
4. 工具内约束

安全性不是阻碍 MCP 改造的理由，反而是改进的机会。
