"""Failed-login throttle: slows password guessing without locking real users out.

Keyed by (username, client address). After MAX_FAILURES failures inside WINDOW_SECONDS the
pair is refused for the rest of the window, even with the right password, so a guesser gets
nothing from continuing. A different username or address is unaffected, so one attacker
cannot lock out someone else's account from elsewhere. Success clears the pair.

In memory and per process: enough to stop online guessing against one worker today. With
several workers each counts separately (the limit becomes N times looser, never stricter);
move the dict to Redis when the service is scaled out. Bounded: expired entries are dropped
and the table is capped, so the throttle itself cannot be used to exhaust memory.
"""
import threading
import time

MAX_FAILURES = 8
WINDOW_SECONDS = 15 * 60
MAX_ENTRIES = 50_000

_lock = threading.Lock()
_failures = {}   # (username, address) -> [first_failure_monotonic, count]


def _key(username, address):
    return ((username or '').strip().lower()[:128], (address or '')[:64])


def _prune(now):
    for key in [k for k, (started, _) in _failures.items() if now - started > WINDOW_SECONDS]:
        del _failures[key]
    if len(_failures) > MAX_ENTRIES:
        # Over capacity under attack: forget the oldest half rather than grow.
        for key in sorted(_failures, key=lambda k: _failures[k][0])[:len(_failures) // 2]:
            del _failures[key]


def retry_after(username, address, now=None):
    """Seconds the caller must wait, or 0 when an attempt is allowed."""
    now = time.monotonic() if now is None else now
    with _lock:
        entry = _failures.get(_key(username, address))
        if not entry:
            return 0
        started, count = entry
        if now - started > WINDOW_SECONDS:
            _failures.pop(_key(username, address), None)
            return 0
        return int(WINDOW_SECONDS - (now - started)) + 1 if count >= MAX_FAILURES else 0


def record_failure(username, address, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        _prune(now)
        entry = _failures.get(_key(username, address))
        if entry and now - entry[0] <= WINDOW_SECONDS:
            entry[1] += 1
        else:
            _failures[_key(username, address)] = [now, 1]


def record_success(username, address):
    with _lock:
        _failures.pop(_key(username, address), None)


def reset():
    """For tests."""
    with _lock:
        _failures.clear()
