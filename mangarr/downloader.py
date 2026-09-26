"""Download a plan through Suwayomi, one source at a time, paced per source.

Suwayomi has a single global download queue, so sources are worked
sequentially and only one download run may exist at a time (a file lock
guards it - the worker and a CLI `add` must not clear each other's queue).
Within a source the batch size adapts: it shrinks to 1 and backs off when
the source throttles, and grows back when downloads succeed.

A chapter that fails on its first source is retried on the next source
that lists it (the plan keeps every usable source per chapter, best
first). A source that fails everything it was asked for is dropped for the
rest of the run. Only chapters no source could deliver end up 'failed'.
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
def download_lock(path: str | None = None):
    path = path or config.LOCK_PATH
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
    label = plan.series.title
    results: dict[float, str] = {}
    pending = {n for n in wanted if plan.candidates.get(n)}
    for n in wanted - pending:
        results[n] = "failed"
        log.warning("%s: ch %g has no usable source", label, n)
    attempt: dict[float, int] = {n: 0 for n in pending}     # index into plan.candidates[n]
    dead: set[int] = set()                                    # manga ids that failed everything
    with download_lock():
        while pending:
            # group what is pending by the source each chapter should try next
            by_source: dict[int, list[float]] = {}
            for n in sorted(pending):
                cands = plan.candidates[n]
                while attempt[n] < len(cands) and cands[attempt[n]].manga_id in dead:
                    attempt[n] += 1
                if attempt[n] >= len(cands):
                    results[n] = "failed"
                    log.warning("%s: ch %g failed on every source (%s)", label, n,
                                ", ".join(c.source.name for c in cands))
                    continue
                by_source.setdefault(cands[attempt[n]].manga_id, []).append(n)
            pending = set()
            for manga_id, nums in by_source.items():
                m = next(c for c in plan.candidates[nums[0]] if c.manga_id == manga_id)
                chapters = {c.number: c for c in m.chapters}
                todo = [chapters[n] for n in nums if n in chapters]
                batch = config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
                # be patient (long backoff) only when no other source can supply these chapters
                patient = all(attempt[n] + 1 >= len(plan.candidates[n]) for n in nums)
                log.info("%s: downloading %d chapter(s) of %r from %s [%s]%s", label, len(todo),
                         m.title, m.source.name, ranges([c.number for c in todo]),
                         "" if patient else " (fallbacks available)")
                ok, failed = _download_source(client, manga_id, todo, batch, label, m.source.name, patient)
                for n in ok:
                    results[n] = "ok"
                if failed:
                    if not ok:
                        dead.add(manga_id)
                        log.warning("%s: %s delivered nothing this run; not retrying it", label, m.source.name)
                    for n in failed:
                        attempt[n] += 1
                        if attempt[n] < len(plan.candidates[n]):
                            nxt = plan.candidates[n][attempt[n]].source.name
                            log.info("%s: ch %g failed on %s, will try %s", label, n, m.source.name, nxt)
                            pending.add(n)
                        else:
                            results[n] = "failed"
                            log.warning("%s: ch %g failed on every source", label, n)
    return results


def _download_source(client, manga_id, todo, batch, label, source_name, patient=True):
    ok, failed = [], []
    i, size, backoff = 0, batch, 0
    max_backoff = config.BACKOFF_MAX if patient else config.BACKOFF_MAX_WITH_FALLBACK
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
            backoff = min(max_backoff, (backoff or 30) * 2)
            log.warning("%s: %s errored on ch %g - backing off %ds", label, source_name, chunk[0].number, backoff)
            time.sleep(backoff)
            if backoff >= max_backoff:
                rest = todo[i:]
                log.error("%s: giving up on %s at ch %g (%d done, %d left)", label, source_name,
                          chunk[0].number, len(ok), len(rest))
                failed.extend(c.number for c in rest)
                break
            continue
        ok.extend(c.number for c in got)
        missed = [c.number for c in chunk if c.id not in have]
        failed.extend(missed)
        if missed:
            log.warning("%s: %s failed ch %s", label, source_name, ranges(missed))
        backoff = 0
        if len(got) == len(chunk) and size < batch:
            size = min(batch, size * 2)
        i += len(chunk)
        log.info("%s: %s %d/%d done%s", label, source_name, len(ok), len(todo),
                 f", {len(failed)} failed" if failed else "")
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
