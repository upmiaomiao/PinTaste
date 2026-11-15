"""单进程内存限流：滑动窗口，按（用户, 类别）计数。

用途：防止前端异常轮询、脚本或误操作在短时间内大量提交任务/上传，拖慢其他用户。
这不替代任务容量上限，只是更早、更便宜地拦住突发流量。
"""
from __future__ import annotations

import collections
import time

from fastapi import HTTPException

# 类别 -> (窗口内最多次数, 窗口秒数)
DEFAULT_LIMITS = {
    'submit': (20, 60),      # 提交任务/追问
    'prepare': (60, 60),     # 对话预检（每次回答一个问题都会调用）
    'upload': (20, 60),      # 上传材料
    'location': (30, 60),    # 地点检索
}


class RateLimiter:
    def __init__(self, limits: dict | None = None, *, max_keys: int = 20000):
        self.limits = dict(DEFAULT_LIMITS, **(limits or {}))
        self._events: dict[tuple, collections.deque] = {}
        self._max_keys = max_keys

    def check(self, user_id: str, bucket: str) -> None:
        limit, window = self.limits.get(bucket, (120, 60))
        now = time.monotonic()
        key = (user_id, bucket)
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self._max_keys:
                self._evict(now)
            events = self._events[key] = collections.deque()
        while events and now - events[0] > window:
            events.popleft()
        if len(events) >= limit:
            retry = max(1, int(window - (now - events[0])) + 1)
            raise HTTPException(429, '操作太频繁，请稍后再试', headers={'Retry-After': str(retry)})
        events.append(now)

    def _evict(self, now: float) -> None:
        """清掉已经没有近期记录的键，防止内存无限增长。"""
        for key in [k for k, e in self._events.items() if not e or now - e[-1] > 120]:
            self._events.pop(key, None)
        if len(self._events) >= self._max_keys:
            self._events.clear()
