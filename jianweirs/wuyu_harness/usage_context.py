"""Task-local billing context; inherited by asyncio acquisition tasks only."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import uuid
import hashlib

_scope = ContextVar('jianwei_usage_scope', default=None)
_reserved = ContextVar('jianwei_imagery_reserved', default=False)


class Reservation:
    def __init__(self):
        self.keep = True

    def cancel(self):
        self.keep = False


@contextmanager
def user_scope(limits, user_id):
    token = _scope.set((limits, user_id))
    try:
        yield
    finally:
        _scope.reset(token)


@asynccontextmanager
async def imagery_reservation(event_id=None):
    control = Reservation()
    scope = _scope.get()
    if scope is None or _reserved.get():
        yield control
        return
    limits, user_id = scope
    event = 'image_' + (hashlib.sha256((user_id + ':' + str(event_id)).encode()).hexdigest() if event_id else uuid.uuid4().hex)
    created = limits.charge(user_id, 'imagery', event)
    nested = _reserved.set(True)
    try:
        yield control
    except BaseException:
        if created:
            limits.refund(event)
        raise
    finally:
        _reserved.reset(nested)
        if created and not control.keep:
            limits.refund(event)
