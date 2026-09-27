"""Download a plan through Suwayomi, one source at a time, paced per source.

Suwayomi has a single global download queue shared with its own library
updates and with the user's own queueing, so this module only ever touches
its own chapter ids: it enqueues them, watches them, and dequeues them if it
gives up. It never clears the queue. Only one mang-arr download run may
exist at a time (a file lock guards it, across the worker and the CLI).

Within a source the batch size adapts: it shrinks to 1 and backs off when
the source errors, and grows back when downloads succeed. A chapter that
fails on its first source is retried on the next source that lists it (the
plan keeps every usable source per chapter, best first). A source that
fails everything it was asked for is dropped for the rest of the run. Only
chapters no source could deliver end up 'failed'.
"""
import fcntl
import logging
import os
import time
from collections.abc import Callable
from contextlib import contextmanager

from . import config
from .resolver import Plan, ranges
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)

# A chunk is abandoned when none of its chapters has made progress for this
# long (a 150-page webtoon chapter on a slow source can take minutes; a dead
# source makes no progress at all).
STALL_SECS = 600
# and never waited on longer than this in total
CHUNK_CAP_SECS = 3 * 3600
# every try errored out faster than this: a dead chapter, not rate limiting
INSTANT_FAIL_SECS = 45


class Cancelled(Exception):
    pass


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


def download(client: Client, plan: Plan, only: set[float] | None = None,
             should_cancel: Callable[[], bool] | None = None, reasons: dict | None = None,
             progress: Callable[[str], None] | None = None, throttled: set | None = None) -> dict:
    """Returns {chapter_number: 'ok' | 'failed'} for every chapter attempted.
    Chapters not reached before a cancel are simply absent. When `reasons`
    is given it is filled with a human-readable reason per failed chapter."""
    reasons = reasons if reasons is not None else {}
    tried: dict[float, list[str]] = {}          # what happened on each source, per chapter
    wanted = set(plan.wanted()) if only is None else set(only)
    label = plan.series.title
    results: dict[float, str] = {}
    pending = {n for n in wanted if plan.candidates.get(n)}
    for n in wanted - pending:
        results[n] = "failed"
        reasons[n] = "no enabled source lists this chapter"
        log.warning("%s: ch %g has no usable source", label, n)
    attempt: dict[float, int] = dict.fromkeys(pending, 0)   # index into plan.candidates[n]
    dead: set[int] = set()                                     # manga ids that failed everything
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    seen_throttle = throttled if throttled is not None else set()
    try:
        with download_lock():
            while pending:
                by_source: dict[int, list[float]] = {}
                for n in sorted(pending):
                    cands = plan.candidates[n]
                    while attempt[n] < len(cands) and cands[attempt[n]].manga_id in dead:
                        attempt[n] += 1
                    if attempt[n] >= len(cands):
                        results[n] = "failed"
                        reasons[n] = "failed on every source: " + "; ".join(tried.get(n) or
                                                                              [c.source.name for c in cands])
                        log.warning("%s: ch %g %s", label, n, reasons[n])
                        continue
                    by_source.setdefault(cands[attempt[n]].manga_id, []).append(n)
                pending = set()
                for manga_id, nums in by_source.items():
                    if cancel():
                        raise Cancelled()
                    m = next(c for c in plan.candidates[nums[0]] if c.manga_id == manga_id)
                    chapters = {c.number: c for c in m.chapters}
                    todo = [chapters[n] for n in nums if n in chapters]
                    batch = config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
                    patient = all(attempt[n] + 1 >= len(plan.candidates[n]) for n in nums)
                    log.info("%s: downloading %d chapter(s) of %r from %s [%s]%s", label, len(todo),
                             m.title, m.source.name, ranges([c.number for c in todo]),
                             "" if patient else " (fallbacks available)")
                    ok, failed, why = _download_source(client, manga_id, todo, batch, label, m.source.name,
                                                       patient, cancel, report, seen_throttle)
                    for n in ok:
                        results[n] = "ok"
                    if failed:
                        if not ok:
                            dead.add(manga_id)
                            log.warning("%s: %s delivered nothing this run; not retrying it", label,
                                        m.source.name)
                        for n in failed:
                            attempt[n] += 1
                            tried.setdefault(n, []).append(f"{m.source.name}: {why.get(n, 'failed')}")
                            if attempt[n] < len(plan.candidates[n]):
                                nxt = plan.candidates[n][attempt[n]].source.name
                                log.info("%s: ch %g failed on %s, will try %s", label, n, m.source.name, nxt)
                                pending.add(n)
                            else:
                                results[n] = "failed"
                                only_one = len(plan.candidates[n]) == 1
                                reasons[n] = ("; ".join(tried[n]) +
                                              (" (no other source has this chapter)" if only_one else ""))
                                log.warning("%s: ch %g failed: %s", label, n, reasons[n])
    except Cancelled:
        log.warning("%s: download cancelled; %d done", label, sum(1 for r in results.values() if r == "ok"))
    return results


def download_one(client: Client, manga_id: int, chapter, label: str, source_name: str) -> tuple[bool, list, dict]:
    """One chapter from one source entry, under the download lock. Returns
    (ok, failed numbers, {number: why})."""
    with download_lock():
        ok, failed, why = _download_source(client, manga_id, [chapter], 1, label, source_name, False,
                                           lambda: False, lambda m: None, set())
    return bool(ok), failed, why


def _download_source(client, manga_id, todo, batch, label, source_name, patient, cancel, report, seen_throttle):
    """Returns (ok numbers, failed numbers, {number: why it failed}). `report`
    receives one-line progress messages for the Activity page."""
    from . import settings
    ok, failed, why = [], [], {}
    i, size, backoff = 0, batch, 0
    max_backoff = config.BACKOFF_MAX if patient else config.BACKOFF_MAX_WITH_FALLBACK
    throttled = batch <= config.BATCH_THROTTLED
    pace = float(settings.get("throttled_delay_seconds") or 0) if throttled else 0.0
    while i < len(todo):
        if cancel():
            raise Cancelled()
        chunk = todo[i:i + size]
        ids = [c.id for c in chunk]
        report(f"{source_name}: chapter {ranges([c.number for c in chunk])} ({len(ok)} of {len(todo)} done"
               + (", rate-limited source: one at a time" if throttled else "") + ")")
        client.enqueue(ids)
        client.start()
        t_start = time.monotonic()
        outcome = _wait(client, ids, cancel)
        instant = outcome == "stalled" and time.monotonic() - t_start < INSTANT_FAIL_SECS
        if outcome in ("stalled", "timeout", "cancelled"):
            try:
                client.dequeue(ids)                  # leave nothing of ours behind
            except SuwayomiError as e:
                log.debug("dequeue after %s failed: %s", outcome, e)
        if outcome == "cancelled":
            raise Cancelled()
        have = client.downloaded_ids(manga_id)
        got = [c for c in chunk if c.id in have]
        if instant and not got:
            # Suwayomi gave up on every try within seconds: the source has no
            # working pages for these chapters ("All CDN attempts failed"),
            # which no amount of waiting fixes. Do not back off; fail them.
            for c in chunk:
                failed.append(c.number)
                why[c.number] = "the source has no working pages for this chapter (failed instantly on every try)"
            log.warning("%s: %s has no working pages for ch %s - not retrying this run", label, source_name,
                        ranges([c.number for c in chunk]))
            i += len(chunk)
            time.sleep(2)
            continue
        if outcome in ("stalled", "timeout") and not got:
            seen_throttle.add(source_name)          # refused after trying for a while: rate limiting
            size = 1
            backoff = min(max_backoff, (backoff or 30) * 2)
            log.warning("%s: %s %s on ch %g - backing off %ds", label, source_name,
                        "made no progress" if outcome == "timeout" else "errored", chunk[0].number, backoff)
            report(f"{source_name} {'made no progress' if outcome == 'timeout' else 'refused the request'}"
                   f" (rate limiting): waiting {backoff} s before retrying chapter "
                   f"{chunk[0].number:g} ({len(ok)} of {len(todo)} done)")
            time.sleep(backoff)
            if backoff >= max_backoff:
                rest = todo[i:]
                log.error("%s: giving up on %s at ch %g (%d done, %d left)", label, source_name,
                          chunk[0].number, len(ok), len(rest))
                failed.extend(c.number for c in rest)
                what = ("Suwayomi reported an error on every try" if outcome == "stalled"
                        else "download made no progress")
                for c in rest:
                    why[c.number] = f"{what}, gave up after {backoff}s of backoff"
                break
            continue
        ok.extend(c.number for c in got)
        missed = [c.number for c in chunk if c.id not in have]
        failed.extend(missed)
        for n in missed:
            why[n] = "Suwayomi finished the batch without this chapter (download error)"
        if missed:
            log.warning("%s: %s failed ch %s", label, source_name, ranges(missed))
        backoff = 0
        if len(got) == len(chunk) and size < batch:
            size = min(batch, size * 2)
        i += len(chunk)
        log.info("%s: %s %d/%d done%s", label, source_name, len(ok), len(todo),
                 f", {len(failed)} failed" if failed else "")
        if pace and i < len(todo):
            report(f"{source_name}: {len(ok)} of {len(todo)} done; pausing {pace:g} s between chapters "
                   "(rate-limited source)")
            time.sleep(pace)
        else:
            time.sleep(2)
    return ok, failed, why


def _wait(client, ids: list[int], cancel, every: int = 5) -> str:
    """Watch our chapter ids until they leave the queue.
    Returns 'done', 'stalled' (every remaining one errored out), 'timeout'
    (no progress for STALL_SECS or CHUNK_CAP_SECS overall) or 'cancelled'."""
    ours = set(ids)
    started = last_change = time.monotonic()
    last_seen: dict[int, tuple] = {}
    while True:
        time.sleep(every)
        if cancel():
            return "cancelled"
        try:
            items = [x for x in client.queue() if x["id"] in ours]
        except SuwayomiError as e:
            log.warning("could not read the download queue: %s", e)
            items = None
        now = time.monotonic()
        if items is not None:
            if not items:
                return "done"
            if all(x["state"] == "ERROR" and x["tries"] >= 3 for x in items):
                return "stalled"
            snapshot = {x["id"]: (x["state"], x["tries"], round(x["progress"], 3)) for x in items}
            if snapshot != last_seen:
                last_seen, last_change = snapshot, now
        if now - last_change > STALL_SECS:
            log.warning("no download progress for %d s", STALL_SECS)
            return "timeout"
        if now - started > CHUNK_CAP_SECS:
            log.warning("download chunk exceeded %d s", CHUNK_CAP_SECS)
            return "timeout"
