"""多用户调度：按任务类别分通道，通道内按用户轮转，批量任务让位于交互任务。

为什么需要它
------------
原来所有任务（包括只需一次模型调用的普通问答）共用一个信号量，默认只有 1 个名额，
一个 5 分钟的遥感分析就会让其他所有人的问答排队。这里改为：

* ``interactive`` 通道：追问与资料检索（只调用模型，不取图），名额较多；
* ``analysis`` 通道：遥感影像核查，名额由管理员配置，受外部图源与模型配额约束；
* 通道内按用户轮转（公平），同一用户的多个排队任务不会挤占别人；
* 批量/区域扫描（priority=1）让位于交互任务（priority=0），等待过久自动升级，不会饿死；
* 取消安全：排队中被取消的任务不会占用名额，已被授予名额但被取消的会归还。
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import time
from contextlib import asynccontextmanager

# 批量任务等待超过这个秒数，就按交互任务同等对待，避免长期饿死。
BATCH_AGING_SECONDS = 180.0


class _Waiter:
    __slots__ = ('future', 'user', 'priority', 'enqueued', 'sequence', 'key')

    def __init__(self, future, user, priority, sequence, key):
        self.future, self.user, self.priority = future, user, priority
        self.enqueued, self.sequence, self.key = time.monotonic(), sequence, key


class FairLane:
    """一个容量固定的通道。``async with lane.slot(user_id)`` 申请名额。"""

    def __init__(self, name: str, capacity: int):
        self.name = name
        self.capacity = max(1, int(capacity))
        self.running = 0
        self.running_by_user: collections.Counter = collections.Counter()
        # priority -> OrderedDict[user -> deque[_Waiter]]
        self._queues: dict[int, collections.OrderedDict] = {}
        self._sequence = itertools.count()
        self._waits = collections.deque(maxlen=500)  # 最近的排队时长（秒）
        self.completed = 0
        self.cancelled_while_waiting = 0

    # ------------------------------------------------------------------ 状态
    @property
    def waiting(self) -> int:
        return sum(len(q) for users in self._queues.values() for q in users.values())

    def resize(self, capacity: int) -> None:
        """改变容量而不丢掉排队中的任务。"""
        self.capacity = max(1, int(capacity))
        self._dispatch()

    def position(self, key) -> int | None:
        """某个排队任务前面大约还有几个（1 表示下一个）。不在排队则返回 None。"""
        mine = None
        for users in self._queues.values():
            for queue in users.values():
                for waiter in queue:
                    if waiter.key == key:
                        mine = waiter
        if mine is None:
            return None
        ahead = 0
        for users in self._queues.values():
            for queue in users.values():
                for waiter in queue:
                    if (waiter.priority, waiter.sequence) < (mine.priority, mine.sequence):
                        ahead += 1
        return ahead + 1

    def stats(self) -> dict:
        waits = sorted(self._waits)
        def pick(fraction):
            return round(waits[min(len(waits) - 1, int(len(waits) * fraction))], 2) if waits else 0.0
        return {'name': self.name, 'capacity': self.capacity, 'running': self.running, 'waiting': self.waiting,
                'completed': self.completed, 'cancelled_while_waiting': self.cancelled_while_waiting,
                'wait_seconds_p50': pick(.5), 'wait_seconds_p95': pick(.95),
                'users_running': sum(1 for v in self.running_by_user.values() if v > 0)}

    def estimated_wait_seconds(self, default_task_seconds: float = 90.0) -> int:
        """给 429 的 Retry-After 用：粗估下一个名额多久空出来。"""
        if self.running < self.capacity:
            return 1
        spare = max(1, self.capacity)
        return int(min(300, max(5, (self.waiting + 1) * default_task_seconds / spare)))

    # ------------------------------------------------------------------ 申请/归还
    @asynccontextmanager
    async def slot(self, user_id: str, *, priority: int = 0, key=None):
        await self.acquire(user_id, priority=priority, key=key)
        try:
            yield self
        finally:
            self.release(user_id)

    async def acquire(self, user_id: str, *, priority: int = 0, key=None) -> None:
        if self.running < self.capacity and not self.waiting:
            self._grant(user_id, waited=0.0)
            return
        future = asyncio.get_running_loop().create_future()
        waiter = _Waiter(future, user_id, priority, next(self._sequence), key)
        self._queues.setdefault(priority, collections.OrderedDict()).setdefault(user_id, collections.deque()).append(waiter)
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                # 名额已经授予但调用方被取消：归还，避免名额泄漏。
                self.release(user_id)
            else:
                self._discard(waiter)
                self.cancelled_while_waiting += 1
            raise

    def release(self, user_id: str) -> None:
        self.running = max(0, self.running - 1)
        if self.running_by_user[user_id] > 0:
            self.running_by_user[user_id] -= 1
        if self.running_by_user[user_id] <= 0:
            self.running_by_user.pop(user_id, None)
        self.completed += 1
        self._dispatch()

    # ------------------------------------------------------------------ 内部
    def _grant(self, user_id: str, *, waited: float) -> None:
        self.running += 1
        self.running_by_user[user_id] += 1
        self._waits.append(waited)

    def _discard(self, waiter: _Waiter) -> None:
        users = self._queues.get(waiter.priority)
        queue = users.get(waiter.user) if users else None
        if queue is not None:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
            if not queue:
                users.pop(waiter.user, None)
        if users is not None and not users:
            self._queues.pop(waiter.priority, None)

    def _effective_priority(self, priority: int, users: collections.OrderedDict, now: float) -> int:
        if priority <= 0:
            return priority
        oldest = min((q[0].enqueued for q in users.values() if q), default=now)
        return 0 if now - oldest >= BATCH_AGING_SECONDS else priority

    def _next_waiter(self):
        now = time.monotonic()
        ranked = []
        for priority, users in self._queues.items():
            if not users:
                continue
            oldest = min((q[0].enqueued for q in users.values() if q), default=now)
            # 先比有效优先级；有效优先级相同，等得更久的先走（先来先服务）
            ranked.append((self._effective_priority(priority, users, now), oldest, priority))
        if not ranked:
            return None
        ranked.sort()
        priority = ranked[0][2]
        users = self._queues[priority]
        # 在这一优先级里，优先让"当前占用名额最少"的用户，其次按轮转顺序。
        chosen = min(users, key=lambda u: self.running_by_user.get(u, 0)) if users else None
        if chosen is None:
            return None
        queue = users[chosen]
        waiter = queue.popleft()
        if not queue:
            users.pop(chosen)
        else:
            users.move_to_end(chosen)
        if not users:
            self._queues.pop(priority, None)
        return waiter

    def _dispatch(self) -> None:
        while self.running < self.capacity:
            waiter = self._next_waiter()
            if waiter is None:
                return
            if waiter.future.done():  # 已被取消
                continue
            self._grant(waiter.user, waited=time.monotonic() - waiter.enqueued)
            waiter.future.set_result(True)


class LaneSet:
    """门户使用的两个通道，并给出任务应走哪个通道。"""

    INTERACTIVE_MODES = frozenset({'contextual_qa', 'information_search'})

    def __init__(self, analysis_capacity: int = 3, interactive_capacity: int = 8):
        self.interactive = FairLane('interactive', interactive_capacity)
        self.analysis = FairLane('analysis', analysis_capacity)

    def lane_for(self, workflow_mode: str | None) -> FairLane:
        return self.interactive if workflow_mode in self.INTERACTIVE_MODES else self.analysis

    def resize(self, *, analysis: int | None = None, interactive: int | None = None) -> None:
        if analysis:
            self.analysis.resize(analysis)
        if interactive:
            self.interactive.resize(interactive)

    def stats(self) -> dict:
        return {'interactive': self.interactive.stats(), 'analysis': self.analysis.stats()}

    def position(self, key):
        return self.interactive.position(key) or self.analysis.position(key)
