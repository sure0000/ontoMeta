"""MCP 工具调用限流（进程内滑动窗口）。

**为什么进程内、不查审计表**：stdio 一个子进程就是一条会话、一个身份，进程内计数即
全局——不需要跨进程一致性，也就不必像设计稿那样每次调用去 count 审计表（那是给下游
DB 平白加读负载）。窗口就是内存里一串时间戳。

**为什么要限流**：MCP 面向通用 agent，最现实的风险不是恶意攻击，而是 **agent 失控
循环**——一个坏 prompt 让它每秒调几十次 execute_sql，几分钟就能打爆数仓。限流是这条
面上唯一能自我保护的闸。

**语义**：滑动窗口只对**放行**的调用计数；被限流拒绝的调用**不**计入窗口，否则窗口
永远填满、永久封锁（惩罚式限流）。execute_sql 直打数仓，单独设更低的上限。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_WINDOW_SECONDS = 60.0

# 限流命中的审计去重窗口：疯狂调用时，同一工具每分钟最多写一条 rate_limited 审计，
# 免得「被限流」本身把审计表刷爆（限流是为了少打下游，审计写库也是下游）。
_AUDIT_DEDUP_SECONDS = 60.0


class RateLimiter:
    """每工具独立的滑动窗口限流器。线程安全（工具可能被 offload 到线程池）。"""

    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._last_audit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _limit_for(self, tool_name: str) -> int:
        from app.database import SessionLocal
        from app.services.settings_service import SettingsService
        with SessionLocal() as db:
            runtime = SettingsService().get_mcp_runtime(db)
        if tool_name == "execute_sql":
            specific = runtime.mcp_execute_sql_rate_limit_per_minute
            if specific and specific > 0:
                return specific
        return runtime.mcp_rate_limit_per_minute

    def check(self, tool_name: str, *, now: float | None = None) -> dict:
        """记录一次调用意图并判定是否放行。

        返回 ``{"allowed", "limit", "retry_after", "should_audit"}``。
        - ``allowed``：本次是否放行（放行才计入窗口）。
        - ``should_audit``：仅在被拒且距上次该工具的限流审计超过去重窗口时为 True，
          让 server 只在限流「首次/间歇」时写审计，不逐次刷库。
        """
        now = time.monotonic() if now is None else now
        limit = self._limit_for(tool_name)
        if not limit or limit <= 0:
            return {"allowed": True, "limit": 0, "retry_after": 0.0, "should_audit": False}

        with self._lock:
            window = self._calls[tool_name]
            cutoff = now - _WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()

            if len(window) < limit:
                window.append(now)
                return {
                    "allowed": True,
                    "limit": limit,
                    "retry_after": 0.0,
                    "should_audit": False,
                }

            # 超限：不计入窗口。retry_after = 最早那次调用滑出窗口还要多久。
            retry_after = max(0.0, _WINDOW_SECONDS - (now - window[0]))
            last = self._last_audit.get(tool_name)
            # 首次命中（last is None）总记一条；之后同一工具在去重窗口内静默。
            should_audit = last is None or (now - last) >= _AUDIT_DEDUP_SECONDS
            if should_audit:
                self._last_audit[tool_name] = now
            return {
                "allowed": False,
                "limit": limit,
                "retry_after": round(retry_after, 1),
                "should_audit": should_audit,
            }

    def reset(self) -> None:
        """清空所有窗口（仅供测试）。"""
        with self._lock:
            self._calls.clear()
            self._last_audit.clear()


# 进程级单例：stdio 一个进程一条会话，限流状态就该是进程全局的。
_limiter = RateLimiter()


def check_rate_limit(tool_name: str, *, now: float | None = None) -> dict:
    return _limiter.check(tool_name, now=now)


def reset_rate_limit() -> None:
    _limiter.reset()
