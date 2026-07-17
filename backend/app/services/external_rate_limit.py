"""外部 API 进程内固定窗口限流（按 app_id）。

多实例部署时需换 Redis；本批按 B8 约定先进程内实现。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class FixedWindowRateLimiter:
    """滑动窗口：过去 window_seconds 内最多 max_requests 次。"""

    def __init__(self, *, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, key: str, max_requests: int) -> tuple[bool, int]:
        """返回 (allowed, remaining)。max_requests<=0 表示不限流。"""
        if max_requests <= 0:
            return True, -1
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= max_requests:
                return False, 0
            q.append(now)
            return True, max_requests - len(q)


# 进程内单例；测试可调用 .reset()
external_rate_limiter = FixedWindowRateLimiter(window_seconds=60.0)
