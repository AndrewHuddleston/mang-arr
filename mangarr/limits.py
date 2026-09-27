"""Numeric settings as the jobs use them, clamped to sane ranges, a sleep and
a blocking call that a cancel interrupts, and spacing between calls.

Settings are clamped to RANGES when they are saved, but a value already in the
database (or written by an older version, or by hand) must not be able to
wedge the single job thread: a pause of 1e9 s, a NaN refresh interval or an
infinite recheck window. So every job-side read goes through setting(),
which rejects non-finite values and clamps to RANGES, and says so in the log.
"""
import logging
import math
import threading
import time
from collections.abc import Callable

from . import settings

log = logging.getLogger(__name__)

# key -> (lowest, highest) accepted at the point of use
RANGES: dict[str, tuple[float, float]] = {
    "refresh_hours": (0.25, 168.0),             # every 15 min .. once a week
    "throttled_delay_seconds": (0.0, 600.0),    # pause between chapters on a rate-limited source
    "recheck_finished_days": (0.0, 365.0),      # 0 = always re-check finished series
    "download_lanes": (1.0, 8.0),               # sources downloading at once in a pass
    "page_delay_seconds": (0.5, 60.0),          # gap between page requests on a page-by-page source
}

_warned: dict[str, object] = {}                 # key -> last bad value logged (log once per value)


class Cancelled(Exception):
    """A job was cancelled while it waited or looked something up."""


def bound(key: str, value) -> float:
    """value as a float inside RANGES[key]; the (equally bounded) default
    when it is not a finite number. No logging: see clamp."""
    lo, hi = RANGES[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = math.nan
    if not math.isfinite(v):
        v = float(settings.DEFAULTS[key])       # an env default can be out of range (or NaN) too
        if not math.isfinite(v):
            v = lo
    return min(max(v, lo), hi)


def clamp(key: str, value) -> float:
    """bound(key, value), with a warning (once per bad value) when that
    changes it."""
    lo, hi = RANGES[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = math.nan
    out = bound(key, value)
    if out != v and _warned.get(key) != repr(value):
        _warned[key] = repr(value)
        log.warning("setting %s = %r is outside %g..%g; using %g", key, value, lo, hi, out)
    return out


def setting(key: str) -> float:
    """The current value of a numeric setting, clamped (see clamp)."""
    try:
        raw = settings.get(key)
    except Exception as e:                      # unreadable settings: run on the default
        log.debug("could not read %s: %s", key, e)
        raw = settings.DEFAULTS[key]
    return clamp(key, raw)


def pause(seconds: float, should_cancel: Callable[[], bool] | None = None, step: float = 1.0) -> bool:
    """Sleep up to `seconds` in short steps; returns True as soon as
    should_cancel() says so (the rest of the pause is skipped)."""
    cancel = should_cancel or (lambda: False)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return cancel()
    # counted steps rather than a deadline, so a patched time.sleep in tests
    # cannot turn this into a busy loop
    for _ in range(math.ceil(seconds / step)):
        if cancel():
            return True
        time.sleep(min(step, seconds))
        seconds -= step
    return cancel()


class Spacer:
    """At least `gap` seconds between the starts of calls with the same key
    (one source's searches), across threads. Each caller reserves the next
    free slot under the lock and then waits for it with the lock released,
    so callers queue up in order and different keys never wait for each
    other. Keys not used for a while are dropped (prune)."""
    PRUNE_AT = 256              # keys kept before old ones are dropped

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 pause: Callable[..., bool] | None = None):
        self._clock = clock
        self._pause = pause                     # None: limits.pause, looked up at each wait
        self._next: dict[str, float] = {}       # key -> earliest start of the next call
        self._lock = threading.Lock()

    def wait(self, key: str, gap: float, should_cancel: Callable[[], bool] | None = None) -> bool:
        """Wait for key's next slot. True when cancelled meanwhile (the slot
        stays taken, so the next caller keeps its spacing)."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(key, now))
            self._next[key] = slot + gap
            if len(self._next) > self.PRUNE_AT:
                self._prune(now, 3600)
        return (self._pause or pause)(slot - now, should_cancel)

    def prune(self, max_age: float = 3600) -> None:
        """Drop keys whose last slot is more than max_age s in the past."""
        with self._lock:
            self._prune(self._clock(), max_age)

    def _prune(self, now: float, max_age: float) -> None:
        for k in [k for k, t in self._next.items() if now - t > max_age]:
            del self._next[k]


def interruptible(fn: Callable, should_cancel: Callable[[], bool] | None, step: float = 1.0):
    """fn() run in a helper thread while this one checks should_cancel()
    every `step` s: returns what fn returns (or raises what it raises), or
    raises Cancelled as soon as a cancel comes. fn is then left to finish on
    its own and its result is dropped, so this is only for calls that are
    safe to abandon (reads, searches), never for a change that must be undone
    if it lands. Without should_cancel, fn() is simply called."""
    if should_cancel is None:
        return fn()
    if should_cancel():
        raise Cancelled()
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:              # handed to the caller below
            box["error"] = e
        finally:
            done.set()
    threading.Thread(target=run, name="mangarr-call", daemon=True).start()
    while not done.wait(step):
        if should_cancel():
            log.debug("cancelled while waiting for %s; leaving it to finish in the background",
                      getattr(fn, "__qualname__", "a call"))
            raise Cancelled()
    if "error" in box:
        raise box["error"]
    return box["value"]
