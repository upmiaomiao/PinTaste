"""每个线程一个长期复用的 SQLite 连接。

为什么
------
原来每次查询都新建并关闭连接。在 WAL 模式下，打开和关闭连接各要 1–2 毫秒（Windows 上更明显）。
对话列表一次要查约 30 次、任务详情约 10 次、任务每个事件还要读写各一次，
这些全是在事件循环线程里同步执行的，用户一多，页面刷新就会互相拖慢。
实测：30 个任务时对话列表 143 ms，其中 90% 以上是连接开关。

做法
----
* 每个线程第一次使用时建立连接并一直复用（事件循环线程一个，``asyncio.to_thread`` 的工作线程各一个）。
* ``with db.connect() as conn`` 的语义不变：正常退出提交，异常回滚。
* 同一线程内嵌套使用时，只有最外层负责提交/回滚，内层复用同一连接（避免内层提前提交外层事务）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def pooling_enabled(default: bool = True) -> bool:
    """环境变量 WUYU_SQLITE_POOL=0 可在任何环境关闭连接复用（例如需要随时移动/备份数据库文件的 Windows 主机）。"""
    import os
    value = os.getenv('WUYU_SQLITE_POOL')
    return default if value is None else value.strip().lower() not in ('0', 'false', 'no', 'off')


class ThreadLocalSQLite:
    def __init__(self, path: Path, *, timeout: float = 15.0, row_factory=None, persistent: bool = True):
        self.path = Path(path)
        self.timeout = timeout
        self.row_factory = row_factory
        # persistent=False：每次使用新建连接、用完关闭（旧行为，不占着数据库文件）。
        self.persistent = persistent
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            # check_same_thread=False：只为了能在关闭服务时从别的线程统一关掉；日常仍是每线程各用各的。
            connection = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
            if self.row_factory is not None:
                connection.row_factory = self.row_factory
            self._local.connection = connection
            self._local.depth = 0
            with self._lock:
                self._all.append(connection)
        return connection

    @contextmanager
    def connect(self):
        if not self.persistent:
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            if self.row_factory is not None:
                connection.row_factory = self.row_factory
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
            return
        connection = self._connection()
        if self._local.depth:          # 嵌套：复用同一连接，由最外层提交或回滚
            self._local.depth += 1
            try:
                yield connection
            finally:
                self._local.depth -= 1
            return
        self._local.depth = 1
        try:
            with connection:           # 正常退出提交，异常回滚
                yield connection
        finally:
            self._local.depth = 0

    def close_all(self) -> None:
        with self._lock:
            connections = list(self._all)
            self._all = []
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
