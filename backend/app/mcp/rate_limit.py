"""MCP 工具调用限流（进程内滑动窗口，按调用方 × 工具分桶）。

**为什么进程内、不查审计表**：不需要跨进程强一致——限流是自我保护的粗闸，不是计费。
每次调用去 count 审计表会给下游 DB 平白加读负载。窗口就是内存里一串时间戳。

**为什么按调用方分桶**：最初只按工具名计数，理由是「stdio 一个子进程就是一条会话、
一个身份，进程内计数即全局」。Phase 5 的远程 HTTP 传输把多个主体放进了同一个进程，
这个前提就不成立了：全局窗口下，一个失控 agent 打满 execute_sql 的额度会连带把其他
所有主体一起拒掉。桶键因此是 ``(调用方, 工具)``，调用方由 ``AuthContext.rate_limit_key``
给出。代价是键空间不再封顶，故被拒路径上顺带回收空窗口（见 ``_evict_idle``）。

⚠ **多 worker 部署**：每个 worker 各持一份窗口，实际上限 = 配置值 × worker 数。
要精确的全局配额得换成共享存储（Redis）；当前定位是「防 agent 失控循环」的粗闸，
按 worker 放大若干倍仍在可接受范围内——但把它当硬配额用就会失望。

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
# Settings are database-backed, but reading them for every tool call adds a
# session/SELECT to the hot path.  Cache only the two numeric limits briefly;
# SettingsService invalidates this cache after a write, so normal updates stay
# immediate while concurrent calls avoid duplicate reads.
_CONFIG_TTL_SECONDS = 1.0


# 不带身份的调用（内部直调、测试）归入的共享桶名。用一个不可能与 principal id
# 相撞的字面量，避免「某个主体恰好叫这个名字」时与它共用配额。
_SHARED_BUCKET = "\x00shared"


class RateLimiter:
    """每（调用方, 工具）独立的滑动窗口限流器。线程安全（工具可能被 offload 到线程池）。"""

    def __init__(self) -> None:
        self._calls: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._last_audit: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._config_cache: tuple[float, int, int] | None = None

    def _limits(self) -> tuple[int, int]:
        loaded_at = time.monotonic()
        with self._config_lock:
            cached = self._config_cache
            if cached and loaded_at - cached[0] < _CONFIG_TTL_SECONDS:
                return cached[1], cached[2]

        from app.database import SessionLocal
        from app.services.settings_service import SettingsService

        with SessionLocal() as db:
            runtime = SettingsService().get_mcp_runtime(db)
        limits = (
            int(runtime.mcp_rate_limit_per_minute or 0),
            int(runtime.mcp_execute_sql_rate_limit_per_minute or 0),
        )
        with self._config_lock:
            self._config_cache = (loaded_at, *limits)
        return limits

    def _limit_for(self, tool_name: str) -> int:
        default_limit, execute_sql_limit = self._limits()
        if tool_name == "execute_sql":
            if execute_sql_limit > 0:
                return execute_sql_limit
        return default_limit

    def check(
        self,
        tool_name: str,
        *,
        principal: str | None = None,
        now: float | None = None,
    ) -> dict:
        """记录一次调用意图并判定是否放行。

        ``principal`` 是调用方的归属键（见 ``AuthContext.rate_limit_key``）。窗口按
        **（调用方, 工具）** 分桶：配额是每个调用方各一份，一个失控 agent 打满自己的
        额度不牵连其他主体。缺省 None 归入共享桶，供不带身份的内部调用与测试使用。

        返回 ``{"allowed", "limit", "retry_after", "should_audit"}``。
        - ``allowed``：本次是否放行（放行才计入窗口）。
        - ``should_audit``：仅在被拒且距上次该桶的限流审计超过去重窗口时为 True，
          让 server 只在限流「首次/间歇」时写审计，不逐次刷库。
        """
        now = time.monotonic() if now is None else now
        limit = self._limit_for(tool_name)
        if not limit or limit <= 0:
            return {"allowed": True, "limit": 0, "retry_after": 0.0, "should_audit": False}

        bucket = (principal or _SHARED_BUCKET, tool_name)
        with self._lock:
            window = self._calls[bucket]
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
            last = self._last_audit.get(bucket)
            # 首次命中（last is None）总记一条；之后同一桶在去重窗口内静默。
            should_audit = last is None or (now - last) >= _AUDIT_DEDUP_SECONDS
            if should_audit:
                self._last_audit[bucket] = now
            self._evict_idle(now)
            return {
                "allowed": False,
                "limit": limit,
                "retry_after": round(retry_after, 1),
                "should_audit": should_audit,
            }

    def _evict_idle(self, now: float) -> None:
        """清掉窗口已空的桶。调用方持锁。

        按工具名分桶时键的数量封顶在工具数（37 个），不清理也不会涨。按主体分桶之后
        键空间是「主体数 × 工具数」——主体来自 principals 表、匿名与管理员各收敛成一个
        固定键，所以并非无界，但也不再是个常数，没有理由把停用主体的窗口一直留着。
        只在**被拒**路径上顺带做：那是低频路径，放行路径不该为此付钱。
        """
        stale = [
            key
            for key, window in self._calls.items()
            if not window or window[-1] < now - _WINDOW_SECONDS
        ]
        for key in stale:
            self._calls.pop(key, None)
            self._last_audit.pop(key, None)

    def reset(self) -> None:
        """清空所有窗口（仅供测试）。"""
        with self._lock:
            self._calls.clear()
            self._last_audit.clear()
        self.invalidate_config()

    def invalidate_config(self) -> None:
        """让运行期设置更新后立即重新读取限流值。"""
        with self._config_lock:
            self._config_cache = None


# 进程级单例：窗口状态活在进程内存里（多 worker 下各持一份，见模块 docstring）。
_limiter = RateLimiter()


def check_rate_limit(
    tool_name: str, *, principal: str | None = None, now: float | None = None
) -> dict:
    return _limiter.check(tool_name, principal=principal, now=now)


def reset_rate_limit() -> None:
    _limiter.reset()


def invalidate_rate_limit_config() -> None:
    _limiter.invalidate_config()
