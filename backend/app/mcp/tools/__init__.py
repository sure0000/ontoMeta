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
    """认证上下文"""

    def __init__(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        client_type: str = "unknown",  # "frontend" | "mcp_local" | "mcp_remote" | "api"
        client_id: str | None = None,
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


class McpTool(Protocol):
    """MCP 工具接口"""

    name: str
    description: str
    input_schema: dict

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        """执行工具逻辑"""
        ...


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
