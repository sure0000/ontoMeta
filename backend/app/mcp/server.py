"""
MCP 服务器入口

提供 ontoMeta 的核心能力为 MCP 工具。
"""
import asyncio
import logging
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from .tools import TOOL_REGISTRY, AuthContext

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def handle_list_tools(context, params) -> types.ListToolsResult:
    """列出所有可用工具"""
    logger.info("Listing tools...")

    tools = [
        types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.input_schema,
        )
        for tool in TOOL_REGISTRY.values()
    ]

    logger.info(f"Found {len(tools)} tools")
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(context, params: types.CallToolRequestParams):
    """执行工具调用"""
    name = params.name
    arguments = params.arguments or {}

    logger.info(f"Calling tool: {name} with arguments: {arguments}")

    # 获取工具
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        error_msg = f"Unknown tool: {name}"
        logger.error(error_msg)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f'{{"success": false, "error": "{error_msg}"}}',
                )
            ]
        )

    # TODO: 实现认证
    # 目前使用默认的匿名上下文（本地开发）
    auth = AuthContext(
        user_id=None,
        client_type="mcp_local",
    )

    try:
        # 调用工具实现
        result = await tool.execute(arguments, auth)

        logger.info(f"Tool {name} executed: success={result.success}")

        # 返回结果
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=result.to_json(),
                )
            ]
        )

    except Exception as e:
        error_msg = f"Tool execution failed: {str(e)}"
        logger.exception(error_msg)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f'{{"success": false, "error": "{error_msg}"}}',
                )
            ]
        )


async def main():
    """启动 MCP 服务器"""
    logger.info("Starting ontoMeta MCP server...")

    # 导入所有工具（触发注册）
    from .tools import query

    logger.info(f"Registered {len(TOOL_REGISTRY)} tools")

    # 创建 MCP 服务器实例，配置回调
    server = Server(
        "ontometa",
        version="1.0.0",
        description="ontoMeta 本体工程和数据治理能力",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )

    # 启动 stdio 服务器
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
