"""Numeric settings as the jobs use them, clamped to sane ranges, and a sleep
that a cancel interrupts.

Settings are validated when they are saved, but a value already in the
database (or written by an older version, or by hand) must not be able to
wedge the single job thread: a pause of 1e9 s, a NaN refresh interval or an
infinite recheck window. So every job-side read goes through setting(),
which rejects non-finite values and clamps to RANGES, and says so in the log.
"""
import logging
import math
import time
from collections.abc import Callable

from . import settings

log = logging.getLogger(__name__)

# key -> (lowest, highest) accepted at the point of use
RANGES: dict[str, tuple[float, float]] = {
    "refresh_hours": (0.25, 168.0),             # every 15 min .. once a week
    "throttled_delay_seconds": (0.0, 600.0),    # pause between chapters on a rate-limited source
    "recheck_finished_days": (0.0, 365.0),      # 0 = always re-check finished series
}

_warned: dict[str, object] = {}                 # key -> last bad value logged (log once per value)


def clamp(key: str, value) -> float:
    """value as a float inside RANGES[key]; the default when it is not a
    finite number. Logs a warning (once per bad value) when it changes it."""
    lo, hi = RANGES[key]
    default = float(settings.DEFAULTS[key])
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = math.nan
    if not math.isfinite(v):
        out = default
    else:
        out = min(max(v, lo), hi)
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
