"""The chapters that are being downloaded, or wait their turn, right now:
what the Activity page lists chapter by chapter and the series page shows on
a chapter's row. Kept in memory by the process that downloads (the web
service): it is what is going on, not a record of it. What came of a
chapter is in the database, as before.

A chapter is "queued" from the moment its series is handed to the download
lanes (or its download is started) and "downloading" while a source works
on it; it leaves the list when it arrived or failed, and the whole series
does when its download is over (clear). Every call is safe from any thread
and never raises: a progress display must not be able to fail a download.
"""
import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

QUEUED, DOWNLOADING = "queued", "downloading"
STALE_SECS = 12 * 3600          # a row nothing has touched for this long is dropped when the list is read
MAX_TEXT = 300

_lock = threading.Lock()
_rows: dict[tuple[int, float], dict] = {}
_local = threading.local()


@contextmanager
def tracking(series_id: int | None, title: str):
    """The downloads this thread starts inside the block are the series'
    (downloader._download_source asks current()): so the downloader needs
    to be told nothing about series."""
    before = getattr(_local, "series", None)
    _local.series = (int(series_id), str(title)) if series_id is not None else None
    try:
        yield
    finally:
        _local.series = before


def current() -> tuple[int, str] | None:
    """(series id, title) of the series this thread downloads for, if any."""
    return getattr(_local, "series", None)


def _safe(fn):
    def call(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:                  # never into a download
            log.debug("inflight.%s: %s: %s", fn.__name__, type(e).__name__, e)
            return None
    call.__name__, call.__doc__ = fn.__name__, fn.__doc__
    return call


@_safe
def queue(series_id: int | None, title: str, numbers, source: str | None = None, text: str = "") -> None:
    """These chapters of the series wait their turn (at `source`, when that
    is known). One that is being downloaded is set back to waiting."""
    if series_id is None:
        return
    now = time.time()
    with _lock:
        for n in numbers:
            key = (int(series_id), float(n))
            was = _rows.get(key)
            _rows[key] = {"series_id": int(series_id), "title": str(title), "number": float(n), "source": source,
                          "state": QUEUED, "text": str(text)[:MAX_TEXT],
                          "since": was["since"] if was and was["state"] == QUEUED else now, "at": now}


@_safe
def start(series_id: int | None, title: str, numbers, source: str, text: str = "") -> None:
    """`source` works on these chapters now."""
    if series_id is None:
        return
    now = time.time()
    with _lock:
        for n in numbers:
            _rows[(int(series_id), float(n))] = {
                "series_id": int(series_id), "title": str(title), "number": float(n), "source": source,
                "state": DOWNLOADING, "text": str(text)[:MAX_TEXT], "since": now, "at": now}


@_safe
def say(series_id: int | None, source: str, text: str) -> None:
    """The progress line of the chapters `source` is downloading for the series."""
    if series_id is None:
        return
    now = time.time()
    with _lock:
        for r in _rows.values():
            if r["series_id"] == int(series_id) and r["state"] == DOWNLOADING and r["source"] == source:
                r["text"], r["at"] = str(text)[:MAX_TEXT], now


@_safe
def finish(series_id: int | None, numbers) -> None:
    """These chapters arrived or failed: no longer in flight."""
    if series_id is None:
        return
    with _lock:
        for n in numbers:
            _rows.pop((int(series_id), float(n)), None)


@_safe
def clear(series_id: int | None) -> None:
    """The series' download is over."""
    if series_id is None:
        return
    with _lock:
        for key in [k for k in _rows if k[0] == int(series_id)]:
            del _rows[key]


def reset() -> None:
    """Forget everything (tests, and a start)."""
    with _lock:
        _rows.clear()


def rows(series_id: int | None = None) -> list[dict]:
    """The chapters in flight, of one series or of all: what is downloading
    first, then what waits, each in the order it got there."""
    now = time.time()
    with _lock:
        for key in [k for k, r in _rows.items() if now - r["at"] > STALE_SECS]:
            del _rows[key]
        out = [dict(r) for r in _rows.values() if series_id is None or r["series_id"] == int(series_id)]
    out.sort(key=lambda r: (r["state"] != DOWNLOADING, r["since"], r["title"], r["number"]))
    return out


def by_number(series_id: int) -> dict[float, dict]:
    """{chapter number: its row} for one series."""
    return {r["number"]: r for r in rows(series_id)}
