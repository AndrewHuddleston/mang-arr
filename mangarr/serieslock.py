"""One lock per series, for whatever works in its library folder.

Renaming a series' files (renamer.py) must not run while an import links
files into the same folder, a delete removes them or a download of the
series is under way, and none of those may start while its files are being
renamed. The download lock does not do for this: a refresh pass holds it for
hours, and its lanes import on threads of their own.

hold(series_id) is the lock: exclusive for a rename, an import, a link
repair and a delete; shared for a download (downloads of one series do not
wait for each other). It is a file lock (flock) on a small file per series
next to the database, so it also holds between processes (the web
worker, the daemon, the CLI), and it goes away with the process that held
it: a killed rename never leaves a series locked.

A thread that holds a series' lock may take it again (an import inside a
link repair); taking the exclusive lock while holding only the shared one
is refused (two downloads doing that would wait for each other for ever),
so a download lets go before its import starts.
"""
import fcntl
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from . import config

log = logging.getLogger(__name__)

WAIT_SECS = 300.0       # an import, download or delete waits this long for a rename (a series takes seconds)
POLL_SECS = 0.2
FOLDER = "series-locks"

_local = threading.local()
_warned = False


class Busy(RuntimeError):
    """The series' lock was not free in time: something else is working in
    its library folder."""


class Cancelled(Busy):
    """The wait for the lock was cancelled."""


def lock_dir() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH) or ".", FOLDER)


def _path(series_id: int) -> str:
    return os.path.join(lock_dir(), f"series-{int(series_id)}.lock")


def _mine() -> dict:
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    return held


def held(series_id: int) -> str | None:
    """"exclusive" or "shared" when this thread holds the series' lock, else None."""
    entry = _mine().get(int(series_id))
    return None if entry is None else "shared" if entry["shared"] else "exclusive"


@contextmanager
def hold(series_id: int, shared: bool = False, wait_secs: float = WAIT_SECS,
         should_cancel: Callable[[], bool] | None = None, strict: bool = False):
    """Hold the series' lock: exclusive, or shared (a download). Waits up to
    wait_secs for it (polling, so a cancel ends the wait: Cancelled), then
    raises Busy. When the lock file cannot be made or opened (a data folder
    that cannot be written), the work goes on without the lock, with a
    warning - unless strict (a rename), which raises Busy: files are never
    renamed unguarded."""
    global _warned
    sid = int(series_id)
    mine = _mine()
    entry = mine.get(sid)
    if entry is not None:
        if entry["shared"] and not shared:
            raise RuntimeError(f"series #{sid}: the exclusive lock was asked for while holding the shared one")
        entry["count"] += 1
        try:
            yield
        finally:
            entry["count"] -= 1
        return
    try:
        os.makedirs(lock_dir(), exist_ok=True)
        fd = os.open(_path(sid), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        if strict:
            raise Busy(f"the lock file of series #{sid} cannot be opened ({e})") from e
        if not _warned:
            _warned = True
            log.warning("cannot open the lock file %s (%s); going on without it: files of a series could be "
                        "renamed while they are imported", _path(sid), e)
        yield
        return
    try:
        _take(fd, sid, shared, wait_secs, should_cancel)
        mine[sid] = {"shared": shared, "count": 1}
        try:
            yield
        finally:
            del mine[sid]
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _take(fd: int, sid: int, shared: bool, wait_secs: float, should_cancel: Callable[[], bool] | None) -> None:
    cancel = should_cancel or (lambda: False)
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    deadline = time.monotonic() + max(wait_secs, 0.0)
    announced = False
    while True:
        try:
            fcntl.flock(fd, mode)
            return
        except OSError:
            pass
        if cancel():
            raise Cancelled(f"cancelled while waiting for series #{sid}")
        if time.monotonic() >= deadline:
            raise Busy(f"series #{sid} is in use: its files are being imported, downloaded, renamed or deleted")
        if not announced:
            announced = True
            log.info("series #%d is in use (an import, download, rename or delete); waiting for it", sid)
        threading.Event().wait(POLL_SECS)           # not time.sleep: tests patch that out


# -- one rename at a time ------------------------------------------------------------

RENAME_LOCK = "rename.lock"


def rename_lock_path() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH) or ".", RENAME_LOCK)


@contextmanager
def rename_run(wait_secs: float = 0.0):
    """Held by a rename (or an undo, or the repair of an interrupted one)
    from its first step to its last, and for a moment by a database restore
    while it swaps the database: one of these at a time, in whatever
    process. Busy when it is not free within wait_secs. It goes away with
    the process that held it, so a rename left unfinished by a kill is known
    by its journal (renamer.repair), never by this lock."""
    path = rename_lock_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise Busy(f"the rename lock {path} cannot be opened ({e})") from e
    try:
        deadline = time.monotonic() + max(wait_secs, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("files are being renamed (or a rename is being undone) right now") from None
            threading.Event().wait(POLL_SECS)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
