"""
MCP 工具注册表

所有 MCP 工具在这里注册。
"""
import json
from typing import Any, Protocol


class ToolResult:
    """工具执行结果的统一信封"""

    def __init__(
        self,
        success: bool,
        data: Any = None,
        error: str | None = None,
        metadata: dict | None = None,
    ):
        self.success = success
        self.data = data
        self.error = error
        self.metadata = metadata or {}

    def to_json(self) -> str:
        """转换为面向 Agent 的紧凑 JSON 字符串。"""
        return json.dumps(
            {
                "success": self.success,
                "data": self.data,
                "error": self.error,
                "metadata": self.metadata,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,  # 处理日期等特殊类型
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }


class AuthContext:
    """认证上下文。

    ``role`` 是四层角色（reader < editor < reviewer < publisher）之一，或 None（无身份）。
    授权判定统一走 ``has_role``——它委托 ``app.models.principal.role_satisfies``，与 REST
    中间件咬同一份判据，MCP 侧不另立一套角色排序。
    """

    def __init__(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        client_type: str = "unknown",  # "frontend" | "mcp_local" | "mcp_remote" | "api"
        client_id: str | None = None,
        role: str | None = None,
        principal_id: str | None = None,
        principal_name: str | None = None,
        anonymous: bool = False,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.client_type = client_type
        self.client_id = client_id
        self.role = role
        self.principal_id = principal_id
        self.principal_name = principal_name
        # 有角色但不是任何一个具体主体：匿名回落（mcp_default_role）。与共享 Admin
        # Token 同样是 principal_id=None，但两者必须分得开——限流按调用方分桶，把匿名
        # 与管理员算作同一个调用方，等于让任何匿名请求都能挤占管理员的额度。
        self.anonymous = anonymous

    @property
    def is_authenticated(self) -> bool:
        return self.role is not None

    @property
    def is_local_mcp(self) -> bool:
        return self.client_type == "mcp_local"

    @property
    def rate_limit_key(self) -> str:
        """限流窗口的归属键——「这次调用算在谁头上」。

        限流是**每调用方**的配额，不是全服务器一个总闸：stdio 下一进程一身份，两者
        等价；但远程 HTTP 传输把多个主体放进同一个进程后，全局窗口意味着任何一个失控
        的 agent 打满窗口就会把其他所有主体一起拒掉——那是拿别人的可用性替自己兜底。

        匿名与共享 Admin Token 各自独立成桶：前者是不可区分的一群人，本就该共担一份
        配额；后者是运维自己的通道，不该被匿名流量挤掉。
        """
        if self.principal_id:
            return f"principal:{self.principal_id}"
        if self.anonymous:
            return "anonymous"
        if self.role:
            return "admin"
        return "unauthenticated"

    def has_role(self, minimum: str | None) -> bool:
        """当前身份是否满足 ``minimum`` 最低角色。``minimum`` 为空表示无需角色（公开）。"""
        if not minimum:
            return True
        from app.models.principal import role_satisfies

        return role_satisfies(self.role, minimum)


class McpTool(Protocol):
    """MCP 工具接口。

    ``required_role`` 声明调用该工具所需的最低角色（reader/editor/reviewer/publisher），
    由服务器在调用前统一强制（工具的 ``execute`` 自身不再各写一遍鉴权）。缺省 reader：
    Phase 2 全是只读工具。写侧/代跑 SQL 的工具必须显式抬高——见各工具的注释。
    """

    name: str
    description: str
    input_schema: dict
    required_role: str

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        """执行工具逻辑"""
        ...


def tool_required_role(tool: "McpTool") -> str:
    """工具声明的最低角色；未声明按 reader（只读）处理。"""
    return getattr(tool, "required_role", "reader") or "reader"


# 工具注册表
TOOL_REGISTRY: dict[str, McpTool] = {}


def register_tool(tool_class):
    """
    注册一个 MCP 工具（装饰器）

    用法:
        @register_tool
        class MyTool:
            name = "my_tool"
            description = "..."
            input_schema = {...}

            async def execute(self, arguments, auth):
                ...
    """
    tool = tool_class()
    TOOL_REGISTRY[tool.name] = tool
    return tool_class


def get_tool(name: str) -> McpTool | None:
    """获取工具"""
    return TOOL_REGISTRY.get(name)


def list_tools() -> list[McpTool]:
    """列出所有工具"""
    return list(TOOL_REGISTRY.values())


# 导入所有工具模块（触发 @register_tool 装饰器）
from . import (  # noqa: E402,F401
    analysis,
    audit,
    authoring,
    datasets,
    datasources,
    flow,
    lifecycle,
    lineage,
    logic_management,
    logics,
    modeling,
    monitoring,
    objects,
    onboarding,
    ops,
    overview,
    playbook,
    proposals,
    query,
    query_aids,
    resolve,
    sql,
    tasks,
)

__all__ = [
    "TOOL_REGISTRY",
    "register_tool",
    "get_tool",
    "list_tools",
    "tool_required_role",
    "ToolResult",
    "AuthContext",
    "McpTool",
]
