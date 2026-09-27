"""Download a plan through Suwayomi, one source at a time, paced per source.

Suwayomi has a single global download queue shared with its own library
updates and with the user's own queueing, so this module only ever touches
its own chapter ids: it enqueues them, watches them, and dequeues them if it
gives up. It never clears the queue. Ids it could not dequeue because
Suwayomi was not answering are remembered and taken out later (see
clear_leftovers). Only one mang-arr download run may exist at a time, per
process and across the worker and the CLI (a file lock guards it). What to
fetch next for a series, and what came of it, is kept apart from the loop
that fetches it (SeriesSteps), so a caller can interleave several series,
one per site. A chapter Suwayomi has not started yet (its queue is busy)
does not count as stalled; after QUEUED_CAP_SECS its series is left for the
next pass, with no verdict on the source.

Within a source the batch size adapts: it shrinks to 1 and backs off when
the source errors, and grows back when downloads succeed. A chapter that
fails on its first source is retried on the next source that lists it (the
plan keeps every usable source per chapter, best first). A source that
fails everything it was asked for is dropped for the rest of the run. Only
chapters no source could deliver end up 'failed'.

A source whose image server refuses bursts (Settings: page by page) goes one
chapter at a time: its pages are first requested through Suwayomi one by one
(pagewarm), then Suwayomi builds the chapter from its cache.
"""
import fcntl
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import config, limits, metrics, pagewarm
from .limits import Cancelled
from .resolver import Plan, SourceMatch, ranges
from .suwayomi import BREAKER_SECS, CircuitOpen, Client, SuwayomiError, SuwayomiUnreachable, with_cancel

log = logging.getLogger(__name__)

# A chunk is abandoned when none of its chapters has made progress for this
# long (a 150-page webtoon chapter on a slow source can take minutes; a dead
# source makes no progress at all).
STALL_SECS = 600
# and never waited on longer than this in total
CHUNK_CAP_SECS = 3 * 3600
# every try errored out faster than this: a dead chapter, not rate limiting
INSTANT_FAIL_SECS = 45
# How long our chapters may wait in Suwayomi's queue without being started
# (its queue busy with its own updates or the user's downloads) before the
# series is left for the next pass. Not rate limiting: the source never saw them.
QUEUED_CAP_SECS = 1800
UNSTARTED_REASON = (f"not attempted: Suwayomi did not start this chapter within {QUEUED_CAP_SECS // 60} min "
                    "(its download queue was busy with other downloads)")
# A page-by-page source whose image server refuses even paced page requests is
# backed off like a rate limit, but at most this long: its lane waits meanwhile.
WARM_BACKOFF_MAX = 120
WARM_BUILD_FAILED = ("its pages were fetched one by one, but Suwayomi still could not build the chapter "
                     "(page cache cleared, or a page kept failing)")
# How long a run waits for another process (the CLI) to finish its download
# before giving up with an error (a bad value: see config.env_number), and
# how often it checks meanwhile.
LOCK_WAIT_SECS = config.env_number("MANGARR_LOCK_WAIT_SECS", 6 * 3600, 0, 7 * 86400)
LOCK_POLL_SECS = 2.0
# Suwayomi itself not answering for this long while we watch a chunk ends the
# run (the caller's pass then stops) instead of waiting out STALL_SECS
UNREACHABLE_GIVE_UP_SECS = 120
# attempts to take our chapters back out of Suwayomi's queue when a chunk ends badly
DEQUEUE_TRIES = 3
# once the job is cancelled, one try of at most this long: a cancel is not held
# up by a Suwayomi that does not answer (the retry below takes the ids out)
CANCEL_DEQUEUE_SECS = 10
# Ids still not dequeued after that (Suwayomi not answering, or an enqueue cut
# short by a cancel that may still land) are kept in an internal setting, at
# most MAX_LEFTOVERS (the newest), and taken out at the start of every download
# run and by a background retry: started at once by every refresh pass, and
# after a failed dequeue once the breaker lets calls through again (and a
# request cut short has run into its own timeout), then backing off to
# LEFTOVER_RETRY_MAX, at most LEFTOVER_RETRY_TRIES times (the next download run
# or pass tries again).
LEFTOVER_KEY = "leftover_queue_ids"
MAX_LEFTOVERS = 200
LEFTOVER_RETRY_SECS = BREAKER_SECS + 5
LEFTOVER_RETRY_MAX = 900
LEFTOVER_RETRY_TRIES = 12
_leftover_lock = threading.Lock()             # read-modify-write of the stored list (and _unsaved)
# Changes to the list the database refused (busy past its timeout): (ids added,
# ids removed) relative to what is stored. leftovers() includes them and every
# later write, or the background retry, saves them.
_SAVED: tuple[tuple[int, ...], frozenset[int]] = ((), frozenset())
_unsaved = _SAVED
_retrier_lock = threading.Lock()
_retrier: threading.Thread | None = None


class LockBusy(RuntimeError):
    """Another download run held the lock for longer than LOCK_WAIT_SECS."""


def acquire_download_lock(path: str | None = None, should_cancel: Callable[[], bool] | None = None,
                          progress: Callable[[str], None] | None = None, wait_secs: float | None = None) -> int:
    """Take the download lock and return its file descriptor (give it back
    with release_download_lock). While another process holds it, poll (never
    block): a cancel raises Cancelled, and after wait_secs (LOCK_WAIT_SECS)
    LockBusy is raised. The holder writes its pid and host into the file so
    the waiter can say who. Split from download_lock so a refresh pass can
    hold it across threads."""
    path = path or config.LOCK_PATH
    wait_secs = LOCK_WAIT_SECS if wait_secs is None else wait_secs
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)      # not "w": that would wipe the holder's pid
    try:
        waited, announced = 0.0, False
        # the same words for the whole wait: waiting is not progress, so a job
        # stuck behind another run shows up in health (see health.stalled_job)
        since = time.strftime("%H:%M")
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
            report(f"waiting for another download run to finish ({holder}) since {since}")
            if limits.pause(LOCK_POLL_SECS, cancel):
                raise Cancelled()
            waited += LOCK_POLL_SECS
    except BaseException:
        os.close(fd)
        raise
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()} on {socket.gethostname()}\n".encode(), 0)
    except OSError as e:
        log.debug("could not record lock holder in %s: %s", path, e)
    return fd


def release_download_lock(fd: int) -> None:
    """Give back a lock taken with acquire_download_lock (any thread may)."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def download_lock(path: str | None = None, should_cancel: Callable[[], bool] | None = None,
                  progress: Callable[[str], None] | None = None, wait_secs: float | None = None):
    """Only one download run at a time, across the web worker and the CLI
    (see acquire_download_lock)."""
    fd = acquire_download_lock(path, should_cancel, progress, wait_secs)
    try:
        yield
    finally:
        release_download_lock(fd)


def _lock_holder(fd: int) -> str:
    try:
        return os.pread(fd, 200, 0).decode(errors="replace").strip() or "unknown process"
    except OSError:
        return "unknown process"


class PassShared:
    """Sources seen rate-limiting us during one refresh pass, shared by the
    series that download at the same time, so the second one does not have
    to learn it again."""

    def __init__(self):
        self._lock = threading.Lock()
        self._names: set[str] = set()

    def seen(self, name: str) -> bool:
        with self._lock:
            return name in self._names

    def add(self, name: str) -> None:
        with self._lock:
            self._names.add(name)


@dataclass
class RunMemo:
    """What one series' download run has learned so far, handed to every
    _download_source call of that run."""
    throttle: set[str] = field(default_factory=set)   # sources that rate-limited us (the caller records them)
    shared: PassShared | None = None                   # the same, pass-wide
    gave_up: set[str] = field(default_factory=set)     # sources backed off until we gave up on them
    rewarmed: set[int] = field(default_factory=set)    # chapter ids whose pages were fetched a second time
    stop: str | None = None                            # set: stop this series, with this reason for the rest

    def paced(self, name: str) -> bool:
        return name in self.throttle or (self.shared is not None and self.shared.seen(name))

    def note_throttle(self, name: str) -> None:
        self.throttle.add(name)
        if self.shared is not None:
            self.shared.add(name)


def lanes_key(name: str) -> str:
    """The site a source name stands for: the EN and ALL variants of one
    extension ('Comick (Unoriginal) (EN)' and '... (ALL)') are one site."""
    return re.sub(r"\s*\((en|all)\)\s*$", "", name.lower().strip())


@dataclass
class Run:
    """One call of _download_source: chapters of one series from one source entry."""
    key: str                    # lanes_key of the source
    match: SourceMatch
    todo: list                  # suwayomi.Chapter, in order
    batch: int
    patient: bool               # no chapter here has another source left to fall back on
    in_order: bool
    end: int = 0                # in order: where the next run starts when this one delivers everything


@dataclass
class _Group:
    manga_id: int
    key: str
    nums: list[float]


class SeriesSteps:
    """The bookkeeping of one series' download, apart from the loop that runs
    it: which source to ask next for which chapters (wants/take), what came
    of it (record), fallbacks to the next source, sources that delivered
    nothing, and the reason for every chapter that did not arrive. No I/O.

    In order, chapters go strictly one after the other, consecutive ones
    from the same source fetched together; a chapter that fails is retried
    on its other sources at once, and one no source can deliver stops the
    series there (the later chapters wait for it, and their reason says so).
    Otherwise chapters are grouped per source in rounds; the ones that fail
    go to their next source in the next round."""

    def __init__(self, plan: Plan, wanted: set[float], in_order: bool, label: str, reasons: dict):
        self.plan, self.in_order, self.label, self.reasons = plan, in_order, label, reasons
        self.results: dict[float, str] = {}
        self.tried: dict[float, list[str]] = {}            # what happened on each source, per chapter
        self.dead: set[int] = set()                        # manga ids that failed everything
        self.finished = False
        pending = {n for n in wanted if plan.candidates.get(n)}
        for n in wanted - pending:
            self.results[n] = "failed"
            reasons[n] = "no enabled source lists this chapter"
            log.warning("%s: ch %g has no usable source", label, n)
        self.attempt: dict[float, int] = dict.fromkeys(pending, 0)   # index into plan.candidates[n]
        self.order = sorted(pending)                       # in order
        self.idx = 0
        self.pending = pending                             # batch: the chapters for the next round
        self.round: list[_Group] = []

    def _next_source(self, n: float):
        cands = self.plan.candidates[n]
        while self.attempt[n] < len(cands) and cands[self.attempt[n]].manga_id in self.dead:
            self.attempt[n] += 1
        return cands[self.attempt[n]] if self.attempt[n] < len(cands) else None

    def wants(self, skip: set) -> list[str]:
        """The sites (lanes_key) this series could download from next,
        chapters in `skip` (no longer wanted) left out. [] when it is done."""
        if self.finished:
            return []
        if self.in_order:
            return self._wants_in_order(skip)
        while True:
            self.round = [g for g in self.round if any(n not in skip for n in g.nums)]
            if self.round:
                return list(dict.fromkeys(g.key for g in self.round))
            if not self.pending:
                self.finished = True
                return []
            self._next_round(skip)

    def _wants_in_order(self, skip: set) -> list[str]:
        order, results = self.order, self.results
        while self.idx < len(order) and (results.get(order[self.idx]) == "ok" or order[self.idx] in skip):
            self.idx += 1                           # done, or ignored meanwhile: never blocks the rest
        if self.idx >= len(order):
            self.finished = True
            return []
        n = order[self.idx]
        m = self._next_source(n)
        if m is None:
            cands = self.plan.candidates[n]
            results[n] = "failed"
            only_one = len(cands) == 1
            self.reasons[n] = "; ".join(self.tried.get(n) or [c.source.name for c in cands]) + \
                (" (no other source has this chapter)" if only_one else "")
            waiting = [x for x in order[self.idx + 1:] if results.get(x) != "ok" and x not in skip]
            for x in waiting:
                self.reasons[x] = (f"waiting for chapter {n:g}: chapters download in order and {n:g} failed on "
                                   "every source (it is retried on schedule; turn off 'download in order' to skip "
                                   "ahead)")
            log.warning("%s: ch %g failed on every source; stopping here, %d later chapter(s) wait for it",
                        self.label, n, len(waiting))
            self.finished = True
            return []
        return [lanes_key(m.source.name)]

    def _next_round(self, skip: set) -> None:
        """Group the pending chapters by their next source; fail the ones no
        source is left for."""
        plan = self.plan
        self.pending -= skip
        groups: dict[int, _Group] = {}
        for n in sorted(self.pending):
            m = self._next_source(n)
            if m is None:
                cands = plan.candidates[n]
                self.results[n] = "failed"
                self.reasons[n] = "failed on every source: " + "; ".join(self.tried.get(n) or
                                                                         [c.source.name for c in cands])
                log.warning("%s: ch %g %s", self.label, n, self.reasons[n])
                continue
            g = groups.get(m.manga_id)
            if g is None:
                g = groups[m.manga_id] = _Group(m.manga_id, lanes_key(m.source.name), [])
            g.nums.append(n)
        self.pending = set()
        self.round = list(groups.values())

    def take(self, key: str, skip: set) -> Run | None:
        """The next run on site `key`, or None when this series has nothing
        for that site (any more)."""
        if key not in self.wants(skip):
            return None
        plan = self.plan
        if self.in_order:
            order = self.order
            n = order[self.idx]
            m = self._next_source(n)
            run, j = [n], self.idx + 1
            while j < len(order) and len(run) < config.RUN_MAX_CHAPTERS:
                x = order[j]
                if self.results.get(x) != "ok" and x not in skip:
                    nx = self._next_source(x)
                    if nx is None or nx.manga_id != m.manga_id:
                        break
                    run.append(x)
                j += 1
            chapters = {c.number: c for c in m.chapters}
            todo = [chapters[x] for x in run if x in chapters]
            patient = all(self.attempt[x] + 1 >= len(plan.candidates[x]) for x in run)
            log.info("%s: downloading %d chapter(s) of %r from %s in order [%s]%s", self.label, len(todo), m.title,
                     m.source.name, ranges(run), "" if patient else " (fallbacks available)")
            # one at a time: a failure never lets a later chapter in
            return Run(key, m, todo, 1, patient, True, end=j)
        g = next(g for g in self.round if g.key == key and any(n not in skip for n in g.nums))
        self.round.remove(g)
        nums = [n for n in g.nums if n not in skip]
        m = next(c for c in plan.candidates[nums[0]] if c.manga_id == g.manga_id)
        chapters = {c.number: c for c in m.chapters}
        todo = [chapters[n] for n in nums if n in chapters]
        # a page-by-page source fetches one chapter at a time
        batch = 1 if m.source.page_warm else config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
        patient = all(self.attempt[n] + 1 >= len(plan.candidates[n]) for n in nums)
        log.info("%s: downloading %d chapter(s) of %r from %s [%s]%s", self.label, len(todo),
                 m.title, m.source.name, ranges([c.number for c in todo]),
                 "" if patient else " (fallbacks available)")
        return Run(key, m, todo, batch, patient, False)

    def record(self, run: Run, ok: list, failed: list, why: dict) -> None:
        """What came of a run: delivered chapters are done, failed ones move
        on to their next source (or fail for good)."""
        m, plan = run.match, self.plan
        for n in ok:
            self.results[n] = "ok"
        if run.in_order:
            if not failed:
                self.idx = run.end
                return
            if not ok:
                self.dead.add(m.manga_id)
                log.warning("%s: %s delivered nothing this run; not retrying it", self.label, m.source.name)
            for x in failed:
                self.attempt[x] += 1
                self.tried.setdefault(x, []).append(f"{m.source.name}: {why.get(x, 'failed')}")
            self.idx = self.order.index(min(failed))    # resume at the first chapter that did not arrive
            return
        if not failed:
            return
        if not ok:
            self.dead.add(m.manga_id)
            log.warning("%s: %s delivered nothing this run; not retrying it", self.label, m.source.name)
        for n in failed:
            self.attempt[n] += 1
            self.tried.setdefault(n, []).append(f"{m.source.name}: {why.get(n, 'failed')}")
            if self.attempt[n] < len(plan.candidates[n]):
                nxt = plan.candidates[n][self.attempt[n]].source.name
                log.info("%s: ch %g failed on %s, will try %s", self.label, n, m.source.name, nxt)
                self.pending.add(n)
            else:
                self.results[n] = "failed"
                only_one = len(plan.candidates[n]) == 1
                self.reasons[n] = ("; ".join(self.tried[n]) +
                                   (" (no other source has this chapter)" if only_one else ""))
                log.warning("%s: ch %g failed: %s", self.label, n, self.reasons[n])

    def stop(self, reason: str) -> None:
        """Nothing more for this series: every wanted chapter not tried to the
        end gets `reason`."""
        for n in self.order:
            if n not in self.results:
                self.reasons[n] = reason
        self.finished = True
        self.round, self.pending = [], set()


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
    wanted = set(plan.wanted()) if only is None else set(only)
    label = plan.series.title
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    gone = _dropper(dropped, label)
    from . import settings
    if in_order is None:
        in_order = bool(settings.get("download_in_order"))
    memo = RunMemo(throttle=throttled if throttled is not None else set())
    steps = SeriesSteps(plan, wanted, in_order, label, reasons)
    try:
        with download_lock(should_cancel=cancel, progress=report):
            clear_leftovers(client, cancel)
            while True:
                if cancel():
                    raise Cancelled()
                skip = gone()
                keys = steps.wants(skip)
                if not keys:
                    break
                run = steps.take(keys[0], skip)
                if run is None:
                    continue
                ok, failed, why = _download_source(client, run.match.manga_id, run.todo, run.batch, label,
                                                   run.match.source.name, run.patient, cancel, report, memo,
                                                   stop_on_fail=run.in_order, throttled=run.match.source.throttled,
                                                   warm=run.match.source.page_warm, gone=gone)
                steps.record(run, ok, failed, why)
                if memo.stop:
                    steps.stop(memo.stop)
    except Cancelled:
        log.warning("%s: download cancelled; %d done", label, sum(1 for r in steps.results.values() if r == "ok"))
    return steps.results


def _dropper(dropped: Callable[[], set] | None, label: str, told: set | None = None) -> Callable[[], set]:
    """Wrap dropped() so a failing check never stops a download, and log
    each chapter the first time it is dropped (`told` keeps that across
    calls when the caller needs it to)."""
    if dropped is None:
        return set
    told = told if told is not None else set()

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


def download_one(client: Client, manga_id: int, chapter, label: str, source_name: str,
                 should_cancel: Callable[[], bool] | None = None,
                 progress: Callable[[str], None] | None = None) -> tuple[bool, list, dict]:
    """One chapter from one source entry, under the download lock. Returns
    (ok, failed numbers, {number: why}). Raises Cancelled when cancelled."""
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    memo = RunMemo()
    warm = _page_warm_name(source_name)
    with download_lock(should_cancel=cancel, progress=report):
        clear_leftovers(client, cancel)
        ok, failed, why = _download_source(client, manga_id, [chapter], 1, label, source_name, False,
                                           cancel, report, memo, warm=warm)
    if not ok and not failed and cancel():         # stopped by the cancel, not a failure of the source
        raise Cancelled()
    if memo.stop and not ok:                        # Suwayomi never got to it
        return False, [chapter.number], {chapter.number: memo.stop}
    return bool(ok), failed, why


def _page_warm_name(name: str) -> bool:
    """Whether Settings has source `name` fetched page by page (a download
    with no resolved plan: one chapter). Anything unreadable means no."""
    try:
        from . import settings
        v = settings.get("page_warm_sources")
        return isinstance(v, list) and name.lower().strip() in v
    except Exception as e:
        log.debug("could not read page_warm_sources: %s: %s", type(e).__name__, e)
        return False


def _download_source(client, manga_id, todo, batch, label, source_name, patient, cancel, report, memo: RunMemo,
                     stop_on_fail: bool = False, throttled: bool = False, warm: bool = False, gone=set):
    """Returns (ok numbers, failed numbers, {number: why it failed}). `report`
    receives one-line progress messages for the Activity page. The pause
    between chapters applies only to a rate-limited source (`throttled`, or
    one seen rate-limiting us during this run: memo), not to every batch of
    one. Chapters in gone() (no longer wanted) are skipped and appear in
    neither list. A cancel cuts every Suwayomi call short, also a hung one;
    the chunk's ids are then taken back out (_dequeue) and what arrived so
    far is returned. A chunk Suwayomi never started (its queue busy with
    other downloads) sets memo.stop and ends the call: that is no verdict
    on the source. On a page-by-page source (`warm`) each chapter's pages
    are fetched one by one before it is queued; one whose pages the image
    server keeps refusing is backed off like a rate limit without ever
    being queued, and one Suwayomi does not build from its cache gets its
    pages fetched once more (memo.rewarmed)."""
    ok, failed, why = [], [], {}
    ops = with_cancel(client, cancel)
    i, size, backoff = 0, batch, 0
    max_backoff = config.BACKOFF_MAX if patient else config.BACKOFF_MAX_WITH_FALLBACK
    if warm:
        size = batch = 1                            # one chapter at a time, its pages first
        max_backoff = min(max_backoff, WARM_BACKOFF_MAX)
    while i < len(todo):
        if cancel():
            raise Cancelled()
        skip = gone()
        while i < len(todo) and todo[i].number in skip:
            i += 1
        if i >= len(todo):
            break
        paced = throttled or memo.paced(source_name)
        span = min(size, len(todo) - i)             # how far this chunk moves us along todo
        chunk = [c for c in todo[i:i + span] if c.number not in skip]
        ids = [c.id for c in chunk]
        status = (f"{source_name}: chapter {ranges([c.number for c in chunk])} ({len(ok)} of {len(todo)} done"
                  + (", rate-limited source: one at a time" if paced and size == 1 else ""))
        report(status + ")")
        warmed = None
        if warm:
            n0 = chunk[0].number
            warmed = pagewarm.warm_chapter(ops, chunk[0], source_name, cancel, report,
                                           suffix=f" ({len(ok)} of {len(todo)} done)")
            metrics.record_warm(source_name, warmed.state)
            if warmed.state == "cancelled":
                break                               # nothing of ours was queued: nothing to take back
            if warmed.state == "no_pages":
                log.info("%s: %s ch %g: %s; falling back to a normal download", label, source_name, n0, warmed.why)
            elif warmed.usable:
                report(f"{source_name}: chapter {n0:g} - {warmed.fetched} of {warmed.pages} pages cached; "
                       f"Suwayomi is building the chapter ({len(ok)} of {len(todo)} done)")
        if warmed is not None and warmed.state in ("refused", "deadline"):
            outcome, got = warmed.state, []         # never queued: straight to the backoff below
        else:
            t_start = time.monotonic()
            outcome, unsure = "error", False
            try:
                try:
                    ops.enqueue(ids)
                except CircuitOpen:
                    outcome = "not sent"            # refused before anything reached Suwayomi: nothing to take back
                    raise
                except Cancelled:
                    unsure = True                   # cut short in flight: it may still land after the dequeue
                    raise
                ops.start()
                outcome = _wait(client, ids, cancel, every=2 if len(ids) == 1 else 5,
                                moved=lambda share, status=status: report(f"{status}, this batch {share:.0%})"))
            except Cancelled:
                outcome = "cancelled"
            finally:
                if outcome not in ("done", "not sent"):     # stalled, timeout, unstarted, cancelled, or an exception
                    _dequeue(client, ids, label, outcome, cancel, unsure)
            if outcome == "unstarted":
                memo.stop = UNSTARTED_REASON
                metrics.record_unstarted(source_name)
                log.warning("%s: %s did not start ch %s within %d min (Suwayomi's queue is busy); stopping this "
                            "series for now", label, source_name, ranges([c.number for c in chunk]),
                            QUEUED_CAP_SECS // 60)
                break
            instant = outcome == "stalled" and time.monotonic() - t_start < INSTANT_FAIL_SECS
            if outcome == "cancelled":
                break                               # keep what arrived; the caller sees the cancel and stops
            try:
                have = ops.downloaded_ids(manga_id)
            except Cancelled:
                break                               # the same; the next import links what did arrive
            got = [c for c in chunk if c.id in have]
            if warmed is not None and warmed.usable:
                if got:
                    metrics.record_warm_download(source_name, "ok")
                elif chunk[0].id not in memo.rewarmed:
                    # Suwayomi went to the source for the pages after all: its
                    # cache was cleared meanwhile. Fetch them again, once.
                    memo.rewarmed.add(chunk[0].id)
                    metrics.record_warm_download(source_name, "rewarm")
                    log.warning("%s: Suwayomi did not build ch %g from its page cache (cleared meanwhile?); "
                                "fetching its pages again", label, chunk[0].number)
                    continue
                else:
                    metrics.record_warm_download(source_name, "failed_after_warm")
                    failed.append(chunk[0].number)
                    why[chunk[0].number] = WARM_BUILD_FAILED
                    log.warning("%s: %s ch %g: %s", label, source_name, chunk[0].number, WARM_BUILD_FAILED)
                    i += span
                    if stop_on_fail or limits.pause(2, cancel):
                        break
                    continue
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
        if outcome in ("stalled", "timeout", "refused", "deadline") and not got:
            memo.note_throttle(source_name)         # refused after trying for a while: rate limiting
            size = 1
            backoff = min(max_backoff, (backoff or 30) * 2)
            did = {"timeout": "made no progress", "stalled": "errored",
                   "refused": "refused even paced page requests",
                   "deadline": "was too slow even page by page"}[outcome]
            log.warning("%s: %s %s on ch %g - backing off %ds", label, source_name, did, chunk[0].number, backoff)
            said = "refused the request" if outcome == "stalled" else did
            report(f"{source_name} {said} (rate limiting): waiting {backoff} s before retrying chapter "
                   f"{chunk[0].number:g} ({len(ok)} of {len(todo)} done)")
            if limits.pause(backoff, cancel):
                break                               # cancelled: these chapters were simply not reached
            if backoff >= max_backoff:
                rest = [c for c in todo[i:] if c.number not in skip]
                log.error("%s: giving up on %s at ch %g (%d done, %d left)", label, source_name,
                          chunk[0].number, len(ok), len(rest))
                memo.gave_up.add(source_name)
                failed.extend(c.number for c in rest)
                if outcome == "refused":
                    what = (f"the image server refused even paced page requests ({warmed.fetched} of "
                            f"{warmed.pages} pages)")
                elif outcome == "deadline":
                    what = (f"fetching pages one at a time took over {warmed.limit / 60:.0f} min "
                            f"({warmed.fetched} of {warmed.pages} pages)")
                else:
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


def _dequeue(client, ids: list[int], label: str, outcome: str, cancel: Callable[[], bool] | None = None,
             unsure: bool = False) -> None:
    """Take our chapter ids back out of Suwayomi's queue (leave nothing of
    ours behind), with a few tries; once the job is cancelled, a single short
    one, so a cancel is never held up by a Suwayomi that does not answer.
    Ids that may still be queued (no answer, or `unsure`: an enqueue cut short
    by a cancel can land after this) are remembered and taken out later
    (clear_leftovers). A failure is logged, never raised, so it cannot hide
    the error that ended the chunk."""
    cancel = cancel or (lambda: False)
    err: Exception | None = None
    for _ in range(DEQUEUE_TRIES):
        quick = cancel()
        try:
            if quick:
                client.dequeue(ids, timeout=CANCEL_DEQUEUE_SECS)
            else:
                with_cancel(client, cancel).dequeue(ids)
            err = None
            break
        except Cancelled as e:
            err = e                                 # cancelled while it waited: the next try is the short one
        except Exception as e:
            err = e
            # no answer to a short try, or the breaker is open: more tries now would not get one either
            if quick or isinstance(e, CircuitOpen):
                break
            limits.pause(2, cancel)
    if err is None and not unsure:
        return
    if err is not None:
        why = "the job was cancelled" if isinstance(err, Cancelled) else str(err) or type(err).__name__
        log.warning("%s: could not remove chapter id(s) %s from Suwayomi's queue after %s (%s); "
                    "will try again once it answers", label, ids, outcome, why)
    else:
        log.debug("%s: the enqueue of chapter id(s) %s was cut short by the cancel and may still land; "
                  "checking the queue again later", label, ids)
    _remember_leftovers(ids)
    _retry_leftovers_later(client)


def _as_ids(value) -> list[int]:
    try:
        return [int(i) for i in value or []]
    except (TypeError, ValueError) as e:
        log.debug("unreadable %s: %s", LEFTOVER_KEY, e)
        return []


def _with_unsaved(stored: list[int]) -> list[int]:
    added, removed = _unsaved
    return [i for i in stored if i not in removed and i not in added] + list(added)


def leftovers() -> list[int]:
    """Chapter ids a failed download may have left in Suwayomi's queue: the
    stored list plus changes the database has not taken yet."""
    from . import settings
    return _with_unsaved(_as_ids(settings.get(LEFTOVER_KEY)))[-MAX_LEFTOVERS:]


def _store_leftovers(change: Callable[[list[int]], list[int]]) -> bool:
    """Rewrite the stored list as change(current), keeping the newest
    MAX_LEFTOVERS. When the database refuses the write (busy for longer than
    its timeout), the change is kept in memory, where leftovers() sees it,
    and saved by the next write or the background retry (the caller starts
    it): dropping it would leave the ids in Suwayomi's queue for good.
    Returns False then; never raises (see _dequeue)."""
    global _unsaved
    from . import db, settings
    with _leftover_lock:
        stored: list[int] | None = None
        try:
            with db.connect() as con:
                settings.refresh(con)               # the current list, not a cached one
                stored = _as_ids(settings.all_values(con).get(LEFTOVER_KEY))
                new = change(_with_unsaved(stored))[-MAX_LEFTOVERS:]
                if new != stored:
                    settings.set_many(con, {LEFTOVER_KEY: new}, internal=True)
        except Exception as e:
            if stored is None:
                stored = _as_ids(settings.get(LEFTOVER_KEY))    # the last list read
            new = change(_with_unsaved(stored))[-MAX_LEFTOVERS:]
            if _unsaved == _SAVED:
                log.warning("could not save the chapter ids left in Suwayomi's queue (%s: %s); keeping them in "
                            "memory and saving them again later", type(e).__name__, e)
            else:
                log.debug("still cannot save the chapter ids left in Suwayomi's queue: %s: %s", type(e).__name__, e)
            _unsaved = (tuple(i for i in new if i not in stored), frozenset(i for i in stored if i not in new))
            return False
        if _unsaved != _SAVED:
            log.info("the chapter ids left in Suwayomi's queue could be saved again")
            _unsaved = _SAVED
        return True


def _remember_leftovers(ids: list[int]) -> bool:
    return _store_leftovers(lambda cur: [i for i in cur if i not in ids] + list(ids))


def _forget_leftovers(ids: list[int]) -> bool:
    return _store_leftovers(lambda cur: [i for i in cur if i not in ids])


def clear_leftovers(client, should_cancel: Callable[[], bool] | None = None) -> bool:
    """Take the chapter ids an earlier chunk could not dequeue out of
    Suwayomi's queue; ids it no longer has queued (downloaded, or removed
    meanwhile) are simply forgotten. Only ever those ids: nothing else in the
    queue is touched. The caller holds the download lock, so none of them is
    being downloaded right now. Returns True when nothing is left over; a
    failure is logged at debug level and tried again later. A cancel
    (should_cancel) raises Cancelled; the ids stay remembered."""
    ids = leftovers()
    if not ids:
        return True
    ask = with_cancel(client, should_cancel)
    try:
        queued = {x["id"] for x in ask.queue()}
        still = [i for i in ids if i in queued]
        if still:
            ask.dequeue(still)
    except Cancelled:
        raise
    except Exception as e:
        log.debug("chapter id(s) %s are still to be taken out of Suwayomi's queue: %s", ids, e)
        return False
    if not _forget_leftovers(ids):
        _retry_leftovers_later(client)              # saves the shorter list once the database takes it
    if still:
        log.info("removed chapter id(s) %s that an earlier failed download left in Suwayomi's queue", still)
    else:
        log.debug("chapter id(s) %s are no longer in Suwayomi's queue", ids)
    return True


@contextmanager
def _lock_if_free(path: str | None = None):
    """The download lock if nobody holds it (yields True); False at once
    when a download run holds it (never waits)."""
    path = path or config.LOCK_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            os.ftruncate(fd, 0)                     # who holds it, for a run that waits meanwhile
            os.pwrite(fd, f"pid {os.getpid()} on {socket.gethostname()}, removing leftover queue entries\n"
                      .encode(), 0)
        except OSError as e:
            log.debug("could not record lock holder in %s: %s", path, e)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def retry_leftovers_now(client) -> None:
    """At the start of a pass: take out ids left over from an earlier run
    (one that ended, or a process that restarted, before its retry
    succeeded), in the background so a Suwayomi that is still down cannot
    hold the pass up."""
    if leftovers():
        _retry_leftovers_later(client, first=0)


def _retry_leftovers_later(client, first: float | None = None) -> None:
    """Start the background retry of clear_leftovers, unless it is running."""
    global _retrier
    with _retrier_lock:
        if _retrier is not None and _retrier.is_alive():
            return
        _retrier = threading.Thread(target=_retry_leftovers, args=(client, first), name="mangarr-dequeue-retry",
                                    daemon=True)
        _retrier.start()


def _retry_leftovers(client, first: float | None = None) -> None:
    """Background: save changes the database refused, then clear_leftovers,
    after `first` s (by default once the breaker lets calls through again),
    backing off while that does not work out. Skipped while a download run
    holds the lock: that run clears them at its start, and every change to
    the stored list is made under the lock (a restore relies on it)."""
    delay = LEFTOVER_RETRY_SECS if first is None else first
    for _ in range(LEFTOVER_RETRY_TRIES):
        threading.Event().wait(delay)               # not time.sleep: tests patch that out
        try:
            with _lock_if_free() as free:
                if free:
                    if _unsaved != _SAVED:
                        _store_leftovers(lambda cur: cur)   # cur already includes them: this only writes them
                    clear_leftovers(client)
        except Exception as e:
            log.debug("retrying the chapter ids left in Suwayomi's queue failed: %s: %s", type(e).__name__, e)
        if not leftovers() and _unsaved == _SAVED:
            return
        delay = min(LEFTOVER_RETRY_MAX, max(delay * 2, LEFTOVER_RETRY_SECS))
    log.warning("stopped retrying for now: chapter id(s) %s may still be in Suwayomi's queue%s; the next download "
                "run takes them out", leftovers(),
                "" if _unsaved == _SAVED else " (and could not be saved)")


def _queue_unreadable(again: bool, e: Exception, extra: str = "") -> None:
    """Log a failed read of the download queue: a warning the first time,
    debug while it keeps failing (it is polled every few seconds)."""
    if again:
        log.debug("still cannot read the download queue: %s", e)
    else:
        log.warning("could not read the download queue: %s (still watching%s)", e, extra)


def _wait(client, ids: list[int], cancel, every: int = 5, moved: Callable[[float], None] | None = None) -> str:
    """Watch our chapter ids until they leave the queue.
    Returns 'done', 'stalled' (every remaining one errored out), 'timeout'
    (no progress for STALL_SECS or CHUNK_CAP_SECS overall), 'unstarted'
    (still waiting in the queue, never tried, after QUEUED_CAP_SECS) or
    'cancelled'. While Suwayomi has not started any of them (its queue is
    busy with other downloads, or it downloads from fewer sources at once
    than we queue on) that is not a stall: the stall clock starts once one
    of them moves. moved(share done, 0..1) hears about every change
    Suwayomi reports."""
    ours = set(ids)
    # reading the queue is safe to abandon, so a cancel cuts a hung read short
    reader = with_cancel(client, cancel)
    started = last_change = time.monotonic()
    last_seen: dict[int, tuple] = {}
    down_since: float | None = None
    unstarted_since: float | None = None
    failing = False                  # the queue could not be read last time: warn once per streak, not per poll
    while True:
        time.sleep(every)
        if cancel():
            return "cancelled"
        try:
            items = [x for x in reader.queue() if x["id"] in ours]
            if failing:
                log.info("the download queue can be read again")
            down_since, failing = None, False
        except Cancelled:
            return "cancelled"
        except SuwayomiUnreachable as e:
            now = time.monotonic()
            down_since = down_since or now
            if now - down_since > UNREACHABLE_GIVE_UP_SECS:
                log.error("Suwayomi has not answered for %d s while downloading; giving up: %s",
                          now - down_since, e)
                raise
            _queue_unreadable(failing, e, f"; giving up after {UNREACHABLE_GIVE_UP_SECS} s")
            failing, items = True, None
        except SuwayomiError as e:
            _queue_unreadable(failing, e)
            failing, items = True, None
        now = time.monotonic()
        if items is not None:
            if not items:
                return "done"
            if all(x["state"] == "ERROR" and x["tries"] >= 3 for x in items):
                return "stalled"
            snapshot = {x["id"]: (x["state"], x["tries"], round(x["progress"], 3)) for x in items}
            if snapshot != last_seen:
                last_seen, last_change = snapshot, now
                if moved:
                    moved((len(ours) - len(items) + sum(min(max(x["progress"], 0.0), 1.0) for x in items))
                          / len(ours))
            if all(x["state"] == "QUEUED" and x["tries"] == 0 and not x["progress"] for x in items):
                if unstarted_since is None:
                    unstarted_since = now
                last_change = now                   # waiting for Suwayomi to get to them, not stalled
                if now - unstarted_since > QUEUED_CAP_SECS:
                    log.warning("our chapter(s) %s were not started by Suwayomi for %d s (its queue is busy)",
                                ids, QUEUED_CAP_SECS)
                    return "unstarted"
            else:
                unstarted_since = None
        if now - last_change > STALL_SECS:
            log.warning("no download progress for %d s", STALL_SECS)
            return "timeout"
        if now - started > CHUNK_CAP_SECS:
            log.warning("download chunk exceeded %d s", CHUNK_CAP_SECS)
            return "timeout"
