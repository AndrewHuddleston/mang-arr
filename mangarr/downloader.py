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
import socket
import time
from collections.abc import Callable
from contextlib import contextmanager

from . import config, limits
from .resolver import Plan, ranges
from .suwayomi import Client, SuwayomiError, SuwayomiUnreachable

log = logging.getLogger(__name__)

# A chunk is abandoned when none of its chapters has made progress for this
# long (a 150-page webtoon chapter on a slow source can take minutes; a dead
# source makes no progress at all).
STALL_SECS = 600
# and never waited on longer than this in total
CHUNK_CAP_SECS = 3 * 3600
# every try errored out faster than this: a dead chapter, not rate limiting
INSTANT_FAIL_SECS = 45
# How long a run waits for another process (the CLI) to finish its download
# before giving up with an error, and how often it checks meanwhile.
LOCK_WAIT_SECS = float(os.environ.get("MANGARR_LOCK_WAIT_SECS", str(6 * 3600)))
LOCK_POLL_SECS = 2.0
# Suwayomi itself not answering for this long while we watch a chunk ends the
# run (the caller's pass then stops) instead of waiting out STALL_SECS
UNREACHABLE_GIVE_UP_SECS = 120
# attempts to take our chapters back out of Suwayomi's queue when a chunk ends badly
DEQUEUE_TRIES = 3


class Cancelled(Exception):
    pass


class LockBusy(RuntimeError):
    """Another download run held the lock for longer than LOCK_WAIT_SECS."""


@contextmanager
def download_lock(path: str | None = None, should_cancel: Callable[[], bool] | None = None,
                  progress: Callable[[str], None] | None = None, wait_secs: float | None = None):
    """Only one download run at a time, across the web worker and the CLI.
    While another process holds it, poll (never block): a cancel raises
    Cancelled, and after wait_secs (LOCK_WAIT_SECS) LockBusy is raised. The
    holder writes its pid and host into the file so the waiter can say who."""
    path = path or config.LOCK_PATH
    wait_secs = LOCK_WAIT_SECS if wait_secs is None else wait_secs
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)      # not "w": that would wipe the holder's pid
    try:
        waited, announced = 0.0, False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                pass
            holder = _lock_holder(fd)
            if not announced:
                log.info("another download run (%s) holds %s; waiting for it", holder, path)
                announced = True
            if waited >= wait_secs:
                log.error("gave up waiting %.0f s for the download lock %s held by %s", waited, path, holder)
                raise LockBusy(f"another download run ({holder}) has held {path} for over {waited:.0f} s")
            report(f"waiting for another download run to finish ({holder}), {waited:.0f} s so far")
            if limits.pause(LOCK_POLL_SECS, cancel):
                raise Cancelled()
            waited += LOCK_POLL_SECS
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"pid {os.getpid()} on {socket.gethostname()}\n".encode(), 0)
        except OSError as e:
            log.debug("could not record lock holder in %s: %s", path, e)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _lock_holder(fd: int) -> str:
    try:
        return os.pread(fd, 200, 0).decode(errors="replace").strip() or "unknown process"
    except OSError:
        return "unknown process"


def download(client: Client, plan: Plan, only: set[float] | None = None,
             should_cancel: Callable[[], bool] | None = None, reasons: dict | None = None,
             progress: Callable[[str], None] | None = None, throttled: set | None = None,
             in_order: bool | None = None, dropped: Callable[[], set] | None = None) -> dict:
    """Returns {chapter_number: 'ok' | 'failed'} for every chapter attempted.
    Chapters not reached before a cancel are simply absent. When `reasons`
    is given it is filled with a human-readable reason per failed chapter.
    `dropped()`, when given, is asked before every chunk for chapters that are
    no longer wanted (ignored by the user meanwhile); those are skipped and
    left out of the result."""
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
    gone = _dropper(dropped, label)
    from . import settings
    if in_order is None:
        in_order = bool(settings.get("download_in_order"))
    if in_order:
        try:
            with download_lock(should_cancel=cancel, progress=report):
                _download_in_order(client, plan, sorted(pending), attempt, dead, tried, results, reasons,
                                   cancel, report, seen_throttle, label, gone)
        except Cancelled:
            log.warning("%s: download cancelled; %d done", label, sum(1 for r in results.values() if r == "ok"))
        return results
    try:
        with download_lock(should_cancel=cancel, progress=report):
            while pending:
                pending -= gone()
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
                    nums = [n for n in nums if n not in gone()]
                    if not nums:
                        continue
                    m = next(c for c in plan.candidates[nums[0]] if c.manga_id == manga_id)
                    chapters = {c.number: c for c in m.chapters}
                    todo = [chapters[n] for n in nums if n in chapters]
                    batch = config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
                    patient = all(attempt[n] + 1 >= len(plan.candidates[n]) for n in nums)
                    log.info("%s: downloading %d chapter(s) of %r from %s [%s]%s", label, len(todo),
                             m.title, m.source.name, ranges([c.number for c in todo]),
                             "" if patient else " (fallbacks available)")
                    ok, failed, why = _download_source(client, manga_id, todo, batch, label, m.source.name,
                                                       patient, cancel, report, seen_throttle,
                                                       throttled=m.source.throttled, gone=gone)
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


def _dropper(dropped: Callable[[], set] | None, label: str) -> Callable[[], set]:
    """Wrap dropped() so a failing check never stops a download, and log
    each chapter the first time it is dropped."""
    if dropped is None:
        return set
    told: set = set()

    def gone() -> set:
        try:
            out = set(dropped())
        except Exception as e:
            log.debug("%s: could not re-check wanted chapters: %s", label, e)
            return set(told)
        for n in sorted(out - told):
            log.info("%s: ch %g is no longer wanted (ignored meanwhile); skipping it", label, n)
        told.update(out)
        return set(told)
    return gone


def _download_in_order(client, plan, order, attempt, dead, tried, results, reasons, cancel, report,
                       seen_throttle, label, gone=set) -> None:
    """Strictly in chapter order. Consecutive chapters that come from the same
    source are fetched together; a chapter that fails is retried on its other
    sources at once; a chapter no source can deliver stops the series there,
    and the later chapters wait for it (their reason says so)."""
    def next_source(n):
        cands = plan.candidates[n]
        while attempt[n] < len(cands) and cands[attempt[n]].manga_id in dead:
            attempt[n] += 1
        return cands[attempt[n]] if attempt[n] < len(cands) else None

    idx = 0
    while idx < len(order):
        if cancel():
            raise Cancelled()
        skip = gone()
        n = order[idx]
        if results.get(n) == "ok" or n in skip:       # done, or ignored meanwhile: never blocks the rest
            idx += 1
            continue
        m = next_source(n)
        if m is None:
            cands = plan.candidates[n]
            results[n] = "failed"
            only_one = len(cands) == 1
            reasons[n] = "; ".join(tried.get(n) or [c.source.name for c in cands]) + \
                (" (no other source has this chapter)" if only_one else "")
            waiting = [x for x in order[idx + 1:] if results.get(x) != "ok" and x not in skip]
            for x in waiting:
                reasons[x] = (f"waiting for chapter {n:g}: chapters download in order and {n:g} failed on every "
                              "source (it is retried on schedule; turn off 'download in order' to skip ahead)")
            log.warning("%s: ch %g failed on every source; stopping here, %d later chapter(s) wait for it",
                        label, n, len(waiting))
            return
        batch = 1                                # one at a time: a failure never lets a later chapter in
        run, j = [n], idx + 1
        while j < len(order) and len(run) < 20:
            x = order[j]
            if results.get(x) != "ok" and x not in skip:
                nx = next_source(x)
                if nx is None or nx.manga_id != m.manga_id:
                    break
                run.append(x)
            j += 1
        chapters = {c.number: c for c in m.chapters}
        todo = [chapters[x] for x in run if x in chapters]
        patient = all(attempt[x] + 1 >= len(plan.candidates[x]) for x in run)
        log.info("%s: downloading %d chapter(s) of %r from %s in order [%s]%s", label, len(todo), m.title,
                 m.source.name, ranges(run), "" if patient else " (fallbacks available)")
        ok, failed, why = _download_source(client, m.manga_id, todo, batch, label, m.source.name, patient, cancel,
                                           report, seen_throttle, stop_on_fail=True, throttled=m.source.throttled,
                                           gone=gone)
        for x in ok:
            results[x] = "ok"
        if not failed:
            idx = j
            continue
        if not ok:
            dead.add(m.manga_id)
            log.warning("%s: %s delivered nothing this run; not retrying it", label, m.source.name)
        for x in failed:
            attempt[x] += 1
            tried.setdefault(x, []).append(f"{m.source.name}: {why.get(x, 'failed')}")
        idx = order.index(min(failed))          # resume at the first chapter that did not arrive


def download_one(client: Client, manga_id: int, chapter, label: str, source_name: str,
                 should_cancel: Callable[[], bool] | None = None,
                 progress: Callable[[str], None] | None = None) -> tuple[bool, list, dict]:
    """One chapter from one source entry, under the download lock. Returns
    (ok, failed numbers, {number: why}). Raises Cancelled when cancelled."""
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    with download_lock(should_cancel=cancel, progress=report):
        ok, failed, why = _download_source(client, manga_id, [chapter], 1, label, source_name, False,
                                           cancel, report, set())
    if not ok and not failed and cancel():         # stopped by the cancel, not a failure of the source
        raise Cancelled()
    return bool(ok), failed, why


def _download_source(client, manga_id, todo, batch, label, source_name, patient, cancel, report, seen_throttle,
                     stop_on_fail: bool = False, throttled: bool = False, gone=set):
    """Returns (ok numbers, failed numbers, {number: why it failed}). `report`
    receives one-line progress messages for the Activity page. The pause
    between chapters applies only to a rate-limited source (`throttled`, or
    one seen rate-limiting us during this run), not to every batch of one.
    Chapters in gone() (no longer wanted) are skipped and appear in neither list."""
    ok, failed, why = [], [], {}
    i, size, backoff = 0, batch, 0
    max_backoff = config.BACKOFF_MAX if patient else config.BACKOFF_MAX_WITH_FALLBACK
    while i < len(todo):
        if cancel():
            raise Cancelled()
        skip = gone()
        while i < len(todo) and todo[i].number in skip:
            i += 1
        if i >= len(todo):
            break
        paced = throttled or source_name in seen_throttle
        span = min(size, len(todo) - i)             # how far this chunk moves us along todo
        chunk = [c for c in todo[i:i + span] if c.number not in skip]
        ids = [c.id for c in chunk]
        report(f"{source_name}: chapter {ranges([c.number for c in chunk])} ({len(ok)} of {len(todo)} done"
               + (", rate-limited source: one at a time" if paced and size == 1 else "") + ")")
        t_start = time.monotonic()
        outcome = "error"
        try:
            client.enqueue(ids)
            client.start()
            outcome = _wait(client, ids, cancel, every=2 if len(ids) == 1 else 5)
        finally:
            if outcome != "done":                   # stalled, timeout, cancelled, or an exception
                _dequeue(client, ids, label, outcome)
        instant = outcome == "stalled" and time.monotonic() - t_start < INSTANT_FAIL_SECS
        if outcome == "cancelled":
            break                                   # keep what arrived; the caller sees the cancel and stops
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
            i += span
            if stop_on_fail or limits.pause(2, cancel):
                break
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
            if limits.pause(backoff, cancel):
                break                               # cancelled: these chapters were simply not reached
            if backoff >= max_backoff:
                rest = [c for c in todo[i:] if c.number not in skip]
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
            if stop_on_fail:
                break
        backoff = 0
        if len(got) == len(chunk) and size < batch:
            size = min(batch, size * 2)
        i += span
        log.info("%s: %s %d/%d done%s", label, source_name, len(ok), len(todo),
                 f", {len(failed)} failed" if failed else "")
        pace = limits.setting("throttled_delay_seconds") if paced else 0.0
        if pace and i < len(todo):
            report(f"{source_name}: {len(ok)} of {len(todo)} done; pausing {pace:g} s between chapters "
                   "(rate-limited source)")
        # a cancel cuts the pause short and returns what arrived so far; the
        # caller then sees the cancel and stops
        if limits.pause(pace if pace and i < len(todo) else 2, cancel):
            break
    return ok, failed, why


def _dequeue(client, ids: list[int], label: str, outcome: str) -> None:
    """Take our chapter ids back out of Suwayomi's queue (leave nothing of
    ours behind), with a few tries; a failure is logged, never raised, so it
    cannot hide the error that ended the chunk."""
    for attempt in range(1, DEQUEUE_TRIES + 1):
        try:
            client.dequeue(ids)
            return
        except Exception as e:
            if attempt == DEQUEUE_TRIES:
                log.warning("%s: could not remove chapter id(s) %s from Suwayomi's queue after %s: %s", label,
                            ids, outcome, e)
                return
            time.sleep(2)


def _wait(client, ids: list[int], cancel, every: int = 5) -> str:
    """Watch our chapter ids until they leave the queue.
    Returns 'done', 'stalled' (every remaining one errored out), 'timeout'
    (no progress for STALL_SECS or CHUNK_CAP_SECS overall) or 'cancelled'."""
    ours = set(ids)
    started = last_change = time.monotonic()
    last_seen: dict[int, tuple] = {}
    down_since: float | None = None
    while True:
        time.sleep(every)
        if cancel():
            return "cancelled"
        try:
            items = [x for x in client.queue() if x["id"] in ours]
            down_since = None
        except SuwayomiUnreachable as e:
            now = time.monotonic()
            down_since = down_since or now
            if now - down_since > UNREACHABLE_GIVE_UP_SECS:
                log.error("Suwayomi has not answered for %d s while downloading; giving up: %s",
                          now - down_since, e)
                raise
            log.warning("could not read the download queue: %s", e)
            items = None
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
