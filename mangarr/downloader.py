"""Download a plan through Suwayomi, one source at a time, paced per source.

Suwayomi has a single global download queue, so sources are worked
sequentially and only one download run may exist at a time (a file lock
guards it - the worker and a CLI `add` must not clear each other's queue).
Within a source the batch size adapts: it shrinks to 1 and backs off when
the source throttles, and grows back when downloads succeed. A source that
keeps failing is abandoned for this run; its chapters stay wanted.
"""
import fcntl
import logging
import os
import time
from contextlib import contextmanager

from . import config
from .resolver import Plan, SourceMatch, ranges
from .suwayomi import Client

log = logging.getLogger(__name__)


@contextmanager
def download_lock(path: str = config.LOCK_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another download run holds %s; waiting for it", path)
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def download(client: Client, plan: Plan, only: set[float] | None = None) -> dict:
    """Returns {chapter_number: 'ok' | 'failed'} for every chapter attempted."""
    wanted = set(plan.wanted()) if only is None else set(only)
    by_source: dict[int, list[tuple[float, SourceMatch]]] = {}
    for n in sorted(wanted):
        m = plan.assignment.get(n)
        if m is not None:
            by_source.setdefault(m.manga_id, []).append((n, m))
    results: dict[float, str] = {}
    with download_lock():
        for manga_id, items in by_source.items():
            m = items[0][1]
            chapters = {c.number: c for c in m.chapters}
            todo = [chapters[n] for n, _ in items if n in chapters]
            batch = config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
            log.info("%s: downloading %d chapter(s) of %s from %s [%s]", plan.series.title, len(todo),
                     m.title, m.source.name, ranges([c.number for c in todo]))
            ok, failed = _download_source(client, manga_id, todo, batch, plan.series.title)
            for n in ok:
                results[n] = "ok"
            for n in failed:
                results[n] = "failed"
    return results


def _download_source(client, manga_id, todo, batch, label):
    ok, failed = [], []
    i, size, backoff = 0, batch, 0
    while i < len(todo):
        chunk = todo[i:i + size]
        client.clear()
        client.enqueue([c.id for c in chunk])
        client.start()
        stalled = _wait(client)
        have = client.downloaded_ids(manga_id)
        got = [c for c in chunk if c.id in have]
        if stalled and not got:
            size = 1
            backoff = min(300, (backoff or 30) * 2)
            log.warning("%s: throttled at ch %g - waiting %ds", label, chunk[0].number, backoff)
            time.sleep(backoff)
            if backoff >= 300:
                rest = todo[i:]
                log.error("%s: giving up on this source at ch %g (%d done, %d left wanted)",
                          label, chunk[0].number, len(ok), len(rest))
                failed.extend(c.number for c in rest)
                break
            continue
        ok.extend(c.number for c in got)
        missed = [c.number for c in chunk if c.id not in have]
        failed.extend(missed)
        if missed:
            log.warning("%s: chapter(s) %s failed", label, ranges(missed))
        backoff = 0
        if len(got) == len(chunk) and size < batch:
            size = min(batch, size * 2)
        i += len(chunk)
        log.info("%s: %d/%d done%s", label, len(ok), len(todo), f", {len(failed)} failed" if failed else "")
        time.sleep(2)
    client.stop()
    return ok, failed


def _wait(client, polls: int = 120, every: int = 5) -> bool:
    """Block until the queue drains. True if every item errored out."""
    for _ in range(polls):
        time.sleep(every)
        q = client.queue()
        if not q:
            return False
        if all(x["state"] == "ERROR" and x["tries"] >= 3 for x in q):
            return True
    log.warning("download queue did not drain in %ds", polls * every)
    return False
