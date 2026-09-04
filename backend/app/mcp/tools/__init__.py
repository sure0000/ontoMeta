"""
MCP 工具注册表

所有 MCP 工具在这里注册。
"""
from typing import Protocol, Any, Callable
from dataclasses import dataclass
import json
from datetime import datetime


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
        """转换为 JSON 字符串"""
        return json.dumps(
            {
                "success": self.success,
                "data": self.data,
                "error": self.error,
                "metadata": self.metadata,
            },
            ensure_ascii=False,
            indent=2,
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
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.client_type = client_type
        self.client_id = client_id
        self.role = role
        self.principal_id = principal_id
        self.principal_name = principal_name

    @property
    def is_authenticated(self) -> bool:
        return self.role is not None

    @property
    def is_local_mcp(self) -> bool:
        return self.client_type == "mcp_local"

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
    query,
    overview,
    objects,
    logics,
    ops,
    query_aids,
    datasources,
    sql,
    tasks,
    proposals,
    lifecycle,
    audit,
    monitoring,
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
