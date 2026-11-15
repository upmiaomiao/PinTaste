"""Server-owned page execution policy and bounded, shared paid-I/O submission.

The model revision remains immutable. Deployment policy is frozen separately
only for newly enqueued jobs. Active paid requests are never preempted to make
room for a new batch; registered batches share the next available submissions.
"""
from contextlib import contextmanager
import threading

from .wuyu_errors import PipelineError


POLICY_KEY = '__execution_policy'
_creation_lock = threading.Lock()


def _invalid():
    return PipelineError('execution_policy_invalid',
        '任务并行执行配置无效，尚未发起新的模型请求；请由管理员检查，已有页面和文件保留。')


def _capacity(config, key):
    value = config.get(key, 0)
    if type(value) is not int or not 0 <= value <= 200:
        raise _invalid()
    return value


def freeze_execution_policy(config):
    workers = _capacity(config, 'WUYU_EXECUTION_PAGE_WORKERS')
    if not workers:
        return None  # Explicit compatibility: no policy, keep historical model workers.
    if not _capacity(config, 'WUYU_PAGE_IO_WORKERS'):
        raise _invalid()  # Never fall back to 20 independent 200-thread pools.
    return {'schema_version': 1, 'page_workers': workers}


def validate_execution_policy(value, config=None):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'page_workers'}:
        raise _invalid()
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise _invalid()
    if type(value['page_workers']) is not int or not 1 <= value['page_workers'] <= 200:
        raise _invalid()
    if config is not None and not _capacity(config, 'WUYU_PAGE_IO_WORKERS'):
        raise _invalid()
    return dict(value)


def public_execution_policy(value):
    try:
        return validate_execution_policy(value)
    except PipelineError:
        return None


def bind_execution_policy(payload, config):
    """Discard even a copied/retried client field; only the server may freeze it."""
    payload.pop(POLICY_KEY, None)
    policy = freeze_execution_policy(config)
    if policy is not None:
        payload[POLICY_KEY] = policy


class _Submission:
    def __init__(self, owner):
        self.owner = owner
        self.used = False

    def __enter__(self):
        return self

    def submit(self, function, *args, **kwargs):
        if self.used:
            raise RuntimeError('Submission has already been consumed')
        future = self.owner._submit(function, *args, **kwargs)
        self.used = True
        return future

    def __exit__(self, *_):
        if not self.used:
            self.owner._release()
            self.used = True


class SharedPagePool:
    """At most capacity running/queued/reserved pages across the whole worker.

Submissions are non-blocking so a job owner can durably accept its completed
results while another batch occupies the pool. With N registered batches each
batch may refill up to ceil(capacity/N). Earlier in-flight work drains naturally.
"""
    def __init__(self, capacity, factory):
        if type(capacity) is not int or not 1 <= capacity <= 200:
            raise _invalid()
        self.capacity = capacity
        self.executor = factory(max_workers=capacity, thread_name_prefix='wuyu-page-io')
        self.condition = threading.Condition()
        self.batches = set()
        self.outstanding = 0
        self.peak = 0

    def batch(self, workers):
        return _SharedBatch(self, workers)

    def shutdown(self):
        # Owners observe the service stop_event and stop dispatching. Already
        # submitted calls must finish saving their paid response/output files.
        self.executor.shutdown(wait=False, cancel_futures=False)


class _SharedBatch:
    def __init__(self, pool, workers):
        self.pool = pool
        self.workers = workers
        self.outstanding = 0
        self.futures = set()

    def __enter__(self):
        with self.pool.condition:
            self.pool.batches.add(self)
            self.pool.condition.notify_all()
        return self

    @property
    def window(self):
        with self.pool.condition:
            share = (self.pool.capacity + len(self.pool.batches) - 1) // max(1, len(self.pool.batches))
            return min(self.workers, share)

    def try_reserve(self):
        with self.pool.condition:
            if self.pool.outstanding >= self.pool.capacity or self.outstanding >= self.window:
                return None
            self.pool.outstanding += 1
            self.pool.peak = max(self.pool.peak, self.pool.outstanding)
            self.outstanding += 1
            return _Submission(self)

    def _release(self):
        with self.pool.condition:
            self.outstanding -= 1
            self.pool.outstanding -= 1
            self.pool.condition.notify_all()

    def _submit(self, function, *args, **kwargs):
        future = self.pool.executor.submit(function, *args, **kwargs)
        with self.pool.condition:
            self.futures.add(future)

        def finished(done):
            with self.pool.condition:
                self.futures.discard(done)
            self._release()
        future.add_done_callback(finished)
        return future

    def wait_for_capacity(self, timeout):
        with self.pool.condition:
            self.pool.condition.wait(timeout)

    def __exit__(self, *_):
        # Match the previous per-batch pool's paid-I/O drain on exceptions, but
        # never shut down another user's shared executor.
        with self.pool.condition:
            while self.outstanding:
                self.pool.condition.wait(.05)
            self.pool.batches.remove(self)
            self.pool.condition.notify_all()


class _LegacyBatch:
    def __init__(self, executor, workers):
        self.executor = executor
        self.window = workers

    def try_reserve(self):
        return _Submission(self)

    def _submit(self, function, *args, **kwargs):
        return self.executor.submit(function, *args, **kwargs)

    def _release(self):
        pass

    def wait_for_capacity(self, timeout):
        threading.Event().wait(timeout)


@contextmanager
def page_executor(app, factory, workers, stop_dispatch):
    capacity = _capacity(app.config, 'WUYU_PAGE_IO_WORKERS')
    if not capacity:
        with factory(max_workers=workers, thread_name_prefix='wuyu-page-io') as executor:
            try:
                yield _LegacyBatch(executor, workers)
            except BaseException:
                stop_dispatch()
                raise
        return
    with _creation_lock:
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        pool = app.extensions.get('wuyu_page_io_pool')
        if pool is None:
            pool = SharedPagePool(capacity, factory)
            app.extensions['wuyu_page_io_pool'] = pool
        elif pool.capacity != capacity:
            raise _invalid()  # A live process cannot silently resize a shared pool.
    with pool.batch(workers) as batch:
        try:
            yield batch
        except BaseException:
            stop_dispatch()
            raise


def shutdown_page_executor(app):
    with _creation_lock:
        pool = getattr(app, 'extensions', {}).get('wuyu_page_io_pool')
    if pool is not None:
        pool.shutdown()
