"""The composer's 联网搜索 / 深度探索 switch, held on the server for a short time.

When the user sends a message with a switch on, the browser records the choice here (a real
interface request, with the research-confirmation interaction header). When the assistant then
opens research for one of that user's projects, the choice is consumed and the search starts at
once. No second confirmation, and nothing a model can create by itself: only the browser route
arms it, and it expires.

Kept in memory: one backend process serves the workflow, and a lost choice after a restart only
means the user switches it on again.
"""
import threading
import time

TTL_SECONDS = 30 * 60
MODES = ('web', 'deep')

_lock = threading.Lock()
_armed = {}


def arm(user_id, mode, now=None):
    if mode not in MODES:
        raise ValueError('mode must be web or deep')
    with _lock:
        _armed[str(user_id)] = (mode, (now if now is not None else time.time()) + TTL_SECONDS)


def consume(user_id, now=None):
    """The armed mode for this user, once; None when nothing is armed or it expired."""
    with _lock:
        value = _armed.pop(str(user_id), None)
    if not value:
        return None
    mode, expires = value
    return mode if (now if now is not None else time.time()) <= expires else None


def peek(user_id, now=None):
    with _lock:
        value = _armed.get(str(user_id))
    if not value:
        return None
    return value[0] if (now if now is not None else time.time()) <= value[1] else None


def clear(user_id=None):
    with _lock:
        if user_id is None:
            _armed.clear()
        else:
            _armed.pop(str(user_id), None)
