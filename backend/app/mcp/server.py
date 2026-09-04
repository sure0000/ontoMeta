"""MCP 服务器入口。

把 ontoMeta 的本体/任务/查询能力暴露为 MCP 工具，供通用 agent（Claude Desktop、
Claude Code、Cursor…）直接调用。

查询、提案和任务生命周期能力均通过 MCP 暴露：生命周期工具只写治理制品，仍受
校验、确认和角色闸门约束；数仓执行由已确认任务的异步 worker 完成。

传输为 stdio——客户端以子进程方式拉起本模块，凭据不过网络。

**鉴权（Phase 3）**：一条 stdio 会话就是一个身份，在启动时解析一次（见 mcp.auth）。
每次调用前按工具声明的 ``required_role`` 集中强制（fail-closed），并把每一次调用
——成功、业务失败、被授权拦下、异常——都记进 append-only 审计（见 mcp.audit）。
"""

from __future__ import annotations

import asyncio
import logging
import time

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from app.database import SessionLocal

from .audit import record_call
from .auth import resolve_auth_context, resolve_http_auth
from .rate_limit import check_rate_limit
from .skills import get_skill, list_skills
from .tools import TOOL_REGISTRY, AuthContext, ToolResult, tool_required_role

# stdout 是 MCP 协议通道，日志一律走 stderr——print/日志落到 stdout 会撑破 JSON-RPC 帧。
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_NAME = "ontometa"
SERVER_VERSION = "1.0.0"

# Keep initialization guidance deliberately small.  Full operating guidance is
# available through MCP prompts, while these lines cover the failure modes that
# otherwise produce plausible but incorrect answers.
SERVER_INSTRUCTIONS = """ontoMeta MCP 使用底线：
1. 业务口径以本体和已登记业务逻辑为准，先用 search_logics/compile_metric，不要凭文字重写 SQL。
2. 连接键和字段字面量必须先用 find_join_path/profile_values 核实，不要猜表名、JOIN 或枚举值。
3. 运行事实只从 get_task_status/get_landing/get_ops_record 读取，不要根据命名规则推断已落地或已成功。

Skill 路由：
- ontometa-discovery：用 query_ontology 探索本体、对象、关系、口径、血缘和落点
- ontometa-query：核实口径、关联和字段取值，编译并执行查询
- ontometa-task-plan：起草并校验任务方案
- ontometa-task-execute：确认、执行和追踪任务运行
- ontometa-admin：查看服务身份、审计和统计
"""

# 会话身份：stdio 一进程一身份，启动时解析一次并缓存。``None`` 表示尚未解析
# （惰性解析，让测试可先设好 env / monkeypatch 再触发）。
_session_auth: AuthContext | None = None


def session_auth() -> AuthContext:
    global _session_auth
    if _session_auth is None:
        _session_auth = resolve_auth_context()
        logger.info(
            "mcp session identity: role=%s principal=%s",
            _session_auth.role,
            _session_auth.principal_name or _session_auth.principal_id or "(anonymous)",
        )
    return _session_auth


def _reset_session_auth() -> None:
    """清掉缓存的会话身份（仅供测试在改 env 后重新解析）。"""
    global _session_auth
    _session_auth = None


def _auth_for(context) -> AuthContext:
    """本次调用的身份。

    远程 HTTP 传输会把原始请求挂在 ``context.request`` 上（stdio 下为 None）——据此**逐请求**
    解析身份；只有 stdio 才用进程级会话身份。绝不能让 HTTP 请求共用 stdio 的单例身份。
    """
    request = getattr(context, "request", None) if context is not None else None
    if request is not None:
        return resolve_http_auth(request)
    return session_auth()


def _text_result(result: ToolResult) -> types.CallToolResult:
    """ToolResult → MCP 回包。

    ``is_error`` 必须跟着 ``success`` 走：只把错误写进 JSON 正文的话，客户端看到的是
    一次「成功」调用，模型得自己从文本里读出失败——重试与降级策略就都失灵了。
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.to_json())],
        structured_content=result.to_dict(),
        is_error=not result.success,
    )


def _error_result(message: str) -> types.CallToolResult:
    return _text_result(ToolResult(success=False, error=message))


async def handle_list_tools(context, params) -> types.ListToolsResult:
    """列出所有已注册工具。"""
    tools = [
        types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.input_schema,
        )
        for tool in TOOL_REGISTRY.values()
    ]
    logger.info("list_tools -> %d tools", len(tools))
    return types.ListToolsResult(tools=tools)


async def handle_list_prompts(context, params) -> types.ListPromptsResult:
    """列出已启用的 Skill prompts。"""
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            skills = list_skills(db)
        prompts = [
            types.Prompt(
                name=skill.name,
                description=str(skill.frontmatter.get("description") or ""),
                title=str(skill.frontmatter.get("whenToUse") or "")[:160] or None,
            )
            for skill in skills
            if skill.enabled
        ]
        record_call(
            auth=_auth_for(context),
            tool_name="prompt:list",
            arguments={},
            success=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return types.ListPromptsResult(prompts=prompts)
    except Exception as exc:  # noqa: BLE001 - prompt discovery must not crash session
        record_call(
            auth=_auth_for(context),
            tool_name="prompt:list",
            arguments={},
            success=False,
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise


async def handle_get_prompt(context, params: types.GetPromptRequestParams) -> types.GetPromptResult:
    """Return one Skill body as a user prompt; unknown names are protocol errors."""
    started = time.monotonic()
    name = params.name
    auth = _auth_for(context)
    try:
        with SessionLocal() as db:
            skill = get_skill(db, name)
        if skill is None or not skill.enabled:
            raise ValueError(f"未知或已停用的 Skill prompt：{name}")
        record_call(
            auth=auth,
            tool_name=f"prompt:{name}",
            arguments={"name": name},
            success=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return types.GetPromptResult(
            description=str(skill.frontmatter.get("description") or ""),
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=skill.body),
                )
            ],
        )
    except Exception as exc:  # noqa: BLE001 - let SDK turn it into JSON-RPC error
        record_call(
            auth=auth,
            tool_name=f"prompt:{name}"[:100],
            arguments={"name": name},
            success=False,
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise


async def handle_call_tool(
    context, params: types.CallToolRequestParams
) -> types.CallToolResult:
    """执行一次工具调用：鉴权 → 授权 → 执行，全程审计。"""
    name = params.name
    arguments = params.arguments or {}
    logger.info("call_tool %s args=%s", name, sorted(arguments))

    tool = TOOL_REGISTRY.get(name)
    if not tool:
        # 未知工具不记审计（没有工具、也无副作用），只回错误。
        return _error_result(
            f"未知工具：{name}（可用：{'、'.join(sorted(TOOL_REGISTRY))}）"
        )

    auth = _auth_for(context)
    minimum = tool_required_role(tool)
    started = time.monotonic()

    # ---- 限流（在授权之前）----
    # 放在授权前：失控循环可能全是被拒的调用，若只在放行后限流，被拒调用照样每次刷审计、
    # 打 DB。限流命中的审计做去重（每工具每分钟至多一条），不逐次刷库。
    verdict = check_rate_limit(name)
    if not verdict["allowed"]:
        elapsed = int((time.monotonic() - started) * 1000)
        result = ToolResult(
            success=False,
            error=(
                f"调用过于频繁：{name} 已达每分钟 {verdict['limit']} 次上限，"
                f"请 {verdict['retry_after']} 秒后重试"
            ),
            metadata={
                "rate_limited": True,
                "limit_per_minute": verdict["limit"],
                "retry_after_seconds": verdict["retry_after"],
            },
        )
        if verdict["should_audit"]:
            record_call(
                auth=auth,
                tool_name=name,
                arguments=arguments,
                success=False,
                denied=False,
                error=f"RATE_LIMITED: {result.error}",
                duration_ms=elapsed,
            )
        logger.info("call_tool %s RATE_LIMITED (limit %s/min)", name, verdict["limit"])
        return _text_result(result)

    # ---- 授权闸门（fail-closed）----
    if not auth.has_role(minimum):
        elapsed = int((time.monotonic() - started) * 1000)
        current = auth.role or "无身份"
        result = ToolResult(
            success=False,
            error=f"权限不足：{name} 需要 {minimum} 角色，当前为 {current}",
            metadata={
                "denied": True,
                "required_role": minimum,
                "current_role": auth.role,
                "hint": (
                    f"在客户端配置的 env 里传入 ONTOMETA_MCP_TOKEN（{minimum} 及以上的"
                    "主体 Token）后重启 MCP 服务器。不要把 Admin Token 交给外部 agent。"
                ),
            },
        )
        record_call(
            auth=auth,
            tool_name=name,
            arguments=arguments,
            success=False,
            denied=True,
            error=result.error,
            duration_ms=elapsed,
        )
        logger.info("call_tool %s DENIED (need %s, have %s)", name, minimum, auth.role)
        return _text_result(result)

    # ---- 执行 ----
    error: str | None = None
    try:
        result = await tool.execute(arguments, auth)
    except Exception as exc:  # noqa: BLE001 —— 工具异常不能掀翻 stdio 会话
        logger.exception("tool %s failed", name)
        error = f"工具执行失败：{exc}"
        result = ToolResult(success=False, error=error)

    elapsed = int((time.monotonic() - started) * 1000)
    record_call(
        auth=auth,
        tool_name=name,
        arguments=arguments,
        success=result.success,
        denied=False,
        error=result.error,
        duration_ms=elapsed,
    )
    logger.info("call_tool %s -> success=%s", name, result.success)
    return _text_result(result)


def build_server() -> Server:
    """装配 MCP 服务器实例（不启动传输），供测试直接调用回调。"""
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        description="ontoMeta 本体工程与数据治理能力",
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
        on_list_prompts=handle_list_prompts,
        on_get_prompt=handle_get_prompt,
    )


async def main() -> None:
    """以 stdio 传输启动 MCP 服务器。"""
    auth = session_auth()
    logger.info(
        "starting ontoMeta MCP server (%d tools, identity role=%s)",
        len(TOOL_REGISTRY),
        auth.role or "(anonymous)",
    )
    server = build_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
