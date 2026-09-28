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
does not count as stalled; after QUEUED_CAP_SECS it goes on to its next
source, or is left for the next pass when it has none, with no verdict on
the source, which is not queued on again for a while.

Within a source the batch size adapts: it shrinks to 1 and backs off when
the source errors, and grows back when downloads succeed. A chapter that
fails on its first source is retried on the next source that lists it (the
plan keeps every usable source per chapter, best first). A source that
delivered nothing of what it was asked for is asked last for the rest of
the run: its chapters go to the other sources first, and it gets them only
when those fail too. Only chapters no source could deliver (every source
that lists them was tried) end up 'failed'.

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
from .suwayomi import BREAKER_SECS, CircuitOpen, Client, SuwayomiError, SuwayomiUnreachable, site_key, with_cancel

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
# (its queue busy with its own updates or the user's downloads) before they
# go on to their next source, or are left for the next pass. Not rate
# limiting: the source never saw them. For QUEUED_BUSY_SECS after that the run
# (the pass) queues nothing more on that source.
QUEUED_CAP_SECS = 1800
QUEUED_BUSY_SECS = 1800
UNSTARTED_REASON = (f"not attempted: Suwayomi did not start this chapter within {QUEUED_CAP_SECS // 60} min "
                    "(its download queue was busy with other downloads)")
UNSTARTED_TRIED = f"not started by Suwayomi within {QUEUED_CAP_SECS // 60} min (its download queue was busy)"
# ... and the chapters of the pass that were not even queued on that source meanwhile
BUSY_TRIED = "not queued: its download queue was busy with other downloads"
_BUSY_REASON = "not attempted: {} is busy with other downloads; tried again next pass"
_BUSY_RE = re.compile(r"not attempted: (.+) is busy with other downloads; tried again next pass\Z")
# A page-by-page source whose image server refuses even paced page requests is
# backed off like a rate limit, but at most this long: its lane waits meanwhile.
# Once the backoff reaches it the chapter is given up at once (another warm-up
# would not follow the wait).
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
# run (in a pass, before every lane step) and by a background retry: started at
# once by every refresh pass, and after a failed dequeue once the breaker lets
# calls through again (and a request cut short has run into its own timeout),
# then backing off to LEFTOVER_RETRY_MAX, at most LEFTOVER_RETRY_TRIES times
# (the next download run or pass tries again).
LEFTOVER_KEY = "leftover_queue_ids"
MAX_LEFTOVERS = 200
LEFTOVER_RETRY_SECS = BREAKER_SECS + 5
LEFTOVER_RETRY_MAX = 900
LEFTOVER_RETRY_TRIES = 12
_leftover_lock = threading.Lock()             # read-modify-write of the stored list (and _unsaved)
# clear_leftovers and a chunk that queues an id still on the list take turns:
# in a pass several lanes download at once, and a chunk may queue again a
# chapter its own failed dequeue left behind (it is ours again then)
_clearing = threading.Lock()
# Changes to the list the database refused (busy past its timeout): (ids added,
# ids removed) relative to what is stored. leftovers() includes them and every
# later write, or the background retry, saves them.
_SAVED: tuple[tuple[int, ...], frozenset[int]] = ((), frozenset())
_unsaved = _SAVED
_retrier_lock = threading.Lock()
_retrier: threading.Thread | None = None
# A restore must not be refused because of the background retry (it only
# holds the download lock while it takes ids out, but on a hung Suwayomi that
# is minutes): while one waits (_retry_yield), the retry cuts its Suwayomi
# calls short, lets go of the lock and does not take it again (the ids stay
# remembered). _retry_holds is set while the retry holds the lock.
_retry_yield = threading.Event()
_retry_holds = threading.Event()
RETRY_LET_GO_SECS = 15


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
    """Sources seen rate-limiting us during one refresh pass, and sources
    whose chapters Suwayomi did not start (when), shared by the series that
    download at the same time, so the second one does not have to learn it
    again."""

    def __init__(self):
        self._lock = threading.Lock()
        self._names: set[str] = set()
        self._unstarted: dict[str, float] = {}

    def seen(self, name: str) -> bool:
        with self._lock:
            return name in self._names

    def add(self, name: str) -> None:
        with self._lock:
            self._names.add(name)

    def unstarted_at(self, name: str) -> float | None:
        with self._lock:
            return self._unstarted.get(name)

    def note_unstarted(self, name: str, at: float) -> None:
        with self._lock:
            self._unstarted[name] = at


@dataclass
class RunMemo:
    """What one series' download run has learned so far, handed to every
    _download_source call of that run."""
    throttle: set[str] = field(default_factory=set)   # sources that rate-limited us (the caller records them)
    shared: PassShared | None = None                   # the same, pass-wide
    gave_up: set[str] = field(default_factory=set)     # sources backed off until we gave up on them
    rewarmed: set[int] = field(default_factory=set)    # chapter ids whose pages were fetched a second time
    unstarted: list[float] = field(default_factory=list)   # chapters Suwayomi did not start here (SeriesSteps)
    unqueued: list[float] = field(default_factory=list)    # not even queued here: its queue was busy lately
    busy: dict[str, float] = field(default_factory=dict)   # source -> when Suwayomi did not start its chapters

    def paced(self, name: str) -> bool:
        return name in self.throttle or (self.shared is not None and self.shared.seen(name))

    def note_throttle(self, name: str) -> None:
        self.throttle.add(name)
        if self.shared is not None:
            self.shared.add(name)

    def queue_busy(self, name: str) -> bool:
        """Suwayomi did not start chapters of source `name` less than
        QUEUED_BUSY_SECS ago (this run, or the pass): nothing more is queued
        there meanwhile."""
        times = [self.busy.get(name), self.shared.unstarted_at(name) if self.shared is not None else None]
        at = max((t for t in times if t is not None), default=None)
        return at is not None and time.monotonic() - at < QUEUED_BUSY_SECS

    def note_unstarted(self, name: str) -> None:
        self.busy[name] = now = time.monotonic()
        if self.shared is not None:
            self.shared.note_unstarted(name, now)


# The reason of a chapter that waits, in order, for an earlier one that
# failed on every source. stuck.py finds the series stuck behind a chapter
# from it, in the rows any pass left, so the start stays as it is.
WAITING_START = "waiting for chapter {}: chapters download in order"
_WAITING_RE = re.compile(r"waiting for chapter (\S{1,30}): chapters download in order")


def waiting_reason(n: float) -> str:
    return (WAITING_START.format(f"{n:g}") + f" and {n:g} failed on every source (it is retried on schedule; skip it "
            "on the series page, or turn off strict download in order, to go on without it)")


def waiting_unlisted_reason(n: float, note: str) -> str:
    """The reason of a chapter that waits, in order, for chapter n, which no
    source listed in the last resolve and is still waited for (note:
    db.unlisted_note)."""
    return WAITING_START.format(f"{n:g}") + f" and {n:g} is {note}"


def waiting_for(reason: str | None) -> str | None:
    """The chapter (as waiting_reason wrote its number) a chapter with this
    reason waits for, or None."""
    m = _WAITING_RE.match(reason or "")
    return m.group(1) if m else None


def busy_reason(name: str) -> str:
    """The reason of a chapter left for the next pass without being queued:
    Suwayomi did not start chapters of source `name` a short while ago, and
    no other source lists it (RunMemo.queue_busy)."""
    return _BUSY_REASON.format(name)


def busy_source(reason: str | None) -> str | None:
    """The source a busy_reason names; None for any other reason."""
    m = _BUSY_RE.match(reason or "")
    return m.group(1) if m else None


def lanes_key(name: str) -> str:
    """The lane a source downloads through: its site (suwayomi.site_key),
    so the EN and ALL variants of one extension share one."""
    return site_key(name)


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
    nothing (asked last from then on), which source delivered or failed
    each chapter (attempts), and the reason for every chapter that did not
    arrive. No I/O.

    In order, chapters go strictly one after the other, consecutive ones
    from the same source fetched together; a chapter that fails is retried
    on its other sources at once, and one no source can deliver stops the
    series there (the later chapters wait for it, and their reason says so).
    Otherwise chapters are grouped per source in rounds; the ones that fail
    go to their next source in the next round. A chapter Suwayomi did not
    start goes on to its next source the same way (not_started), but with
    no source left it is not attempted this time instead of failed.

    Each chapter's sources are tried best first (plan.candidates), every one
    of them before the chapter fails; an entry that delivered nothing in a
    run (dead: one broken chapter can be all a short run asks) is not
    dropped but moved behind the others, and is never an alternative. When
    the sites wants() names are busy with other series, a caller may take
    the same chapters from another source that lists them (alternatives: no
    worse a tier, on another site); the source skipped that way is still
    tried if that one fails."""

    def __init__(self, plan: Plan, wanted: set[float], in_order: bool, label: str, reasons: dict):
        self.plan, self.in_order, self.label, self.reasons = plan, in_order, label, reasons
        self.results: dict[float, str] = {}
        self.tried: dict[float, list[str]] = {}            # what happened on each source, per chapter
        self.dead: set[int] = set()                        # manga ids that delivered nothing: asked last
        self.attempts: list[tuple[str, float, str]] = []   # (source name, chapter, 'ok' | 'failed') per try
        self.held: dict[float, str] = {}                   # its last source did not start it -> reason if none left
        self.finished = False
        pending = {n for n in wanted if plan.candidates.get(n)}
        for n in wanted - pending:
            self.results[n] = "failed"
            reasons[n] = "no enabled source lists this chapter"
            log.warning("%s: ch %g has no usable source", label, n)
        self.used: dict[float, set[int]] = {n: set() for n in pending}   # entries (manga ids) tried per chapter
        self.order = sorted(pending)                       # in order
        self.idx = 0
        self.pending = pending                             # batch: the chapters for the next round
        self.round: list[_Group] = []

    def _open(self, n: float) -> list[SourceMatch]:
        """Chapter n's entries not tried yet for it, best first, the dead
        ones (delivered nothing this run) after the others."""
        used, dead = self.used[n], self.dead
        left = [c for c in self.plan.candidates[n] if c.manga_id not in used]
        return [c for c in left if c.manga_id not in dead] + [c for c in left if c.manga_id in dead]

    def _next_source(self, n: float):
        return next(iter(self._open(n)), None)

    def _others(self, n: float, m: SourceMatch) -> bool:
        """Whether chapter n has an entry left to try besides m, not counting
        dead ones (a run is patient when it has none)."""
        return any(c.manga_id != m.manga_id and c.manga_id not in self.dead for c in self._open(n))

    def _instead(self, n: float) -> list[SourceMatch]:
        """The entries chapter n could come from now instead of its next
        source: not tried yet, on other sites (one each, the best), and no
        worse a tier (normal, rate-limited, page by page: Source.tier). Never
        a dead entry: waiting for the next source beats a switch to one that
        delivered nothing."""
        left = self._open(n)
        if not left:
            return []
        first, out = left[0], []
        seen = {lanes_key(first.source.name)}
        for c in left[1:]:
            k = lanes_key(c.source.name)
            if k not in seen and c.manga_id not in self.dead and c.source.tier <= first.source.tier:
                seen.add(k)
                out.append(c)
        return out

    def alternatives(self, skip: set) -> list[str]:
        """Sites (lanes_key) this series could download from instead when
        every site of wants() is busy or resting: in order, another entry of
        the next chapter; otherwise another entry of a chapter of the round.
        Best first; [] when there are none, or it is done."""
        keys = self.wants(skip)
        if not keys:
            return []
        if self.in_order:
            nums = [self.order[self.idx]]
        else:
            nums = [n for g in self.round for n in g.nums if n not in skip]
        out = [lanes_key(c.source.name) for n in nums for c in self._instead(n)]
        return [k for k in dict.fromkeys(out) if k not in keys]

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
        if m is None and n in self.held:
            log.warning("%s: ch %g was not started on any source left (Suwayomi's queue is busy); it and the "
                        "later chapters wait for the next pass", self.label, n)
            self.stop(self.held[n])
            return []
        if m is None:
            cands = self.plan.candidates[n]
            results[n] = "failed"
            only_one = len(cands) == 1
            self.reasons[n] = "; ".join(self.tried.get(n) or [c.source.name for c in cands]) + \
                (" (no other source has this chapter)" if only_one else "")
            waiting = [x for x in order[self.idx + 1:] if results.get(x) != "ok" and x not in skip]
            for x in waiting:
                self.reasons[x] = waiting_reason(n)
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
            if m is None and n in self.held:
                self.reasons[n] = self.held[n]
                log.warning("%s: ch %g was not started on any source left (Suwayomi's queue is busy); it waits "
                            "for the next pass", self.label, n)
                continue
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
        """The next run on site `key` (one of wants(), or of alternatives()),
        or None when this series has nothing for that site (any more)."""
        keys = self.wants(skip)
        if key not in keys and key not in self.alternatives(skip):
            return None
        if self.in_order:
            return self._take_in_order(key, skip)
        g = next((g for g in self.round if g.key == key and any(n not in skip for n in g.nums)), None)
        if g is not None:
            self.round.remove(g)
            nums = [n for n in g.nums if n not in skip]
            m = next(c for c in self.plan.candidates[nums[0]] if c.manga_id == g.manga_id)
            instead = ""
        else:
            m, nums = self._take_instead(key, skip)
            instead = " instead of their busy source(s)"
        chapters = {c.number: c for c in m.chapters}
        todo = [chapters[n] for n in nums if n in chapters]
        # a page-by-page source fetches one chapter at a time
        batch = 1 if m.source.page_warm else config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
        patient = not any(self._others(n, m) for n in nums)
        log.info("%s: downloading %d chapter(s) of %r from %s%s [%s]%s", self.label, len(todo),
                 m.title, m.source.name, instead, ranges([c.number for c in todo]),
                 "" if patient else " (fallbacks available)")
        return Run(key, m, todo, batch, patient, False)

    def _take_in_order(self, key: str, skip: set) -> Run:
        """In order: the next chapter and the ones after it that come from
        the same entry. On site `key` that is its next source, or else the
        entry there that lists it too (alternatives), which then also takes
        the chapters after it whose next source is on the same busy site."""
        order = self.order
        n = order[self.idx]
        first = self._next_source(n)
        m, busy_site = first, None
        if lanes_key(first.source.name) != key:
            m = next(c for c in self._instead(n) if lanes_key(c.source.name) == key)
            busy_site = lanes_key(first.source.name)
            log.info("%s: %s is busy; ch %g comes from %s instead", self.label, first.source.name, n, m.source.name)

        def fits(x: float, nx: SourceMatch) -> bool:
            if nx.manga_id == m.manga_id:
                return True
            return busy_site is not None and lanes_key(nx.source.name) == busy_site and \
                any(c.manga_id == m.manga_id for c in self._instead(x))
        run, j = [n], self.idx + 1
        while j < len(order) and len(run) < config.RUN_MAX_CHAPTERS:
            x = order[j]
            if self.results.get(x) != "ok" and x not in skip:
                nx = self._next_source(x)
                if nx is None or not fits(x, nx):
                    break
                run.append(x)
            j += 1
        chapters = {c.number: c for c in m.chapters}
        todo = [chapters[x] for x in run if x in chapters]
        patient = not any(self._others(x, m) for x in run)
        log.info("%s: downloading %d chapter(s) of %r from %s in order [%s]%s", self.label, len(todo), m.title,
                 m.source.name, ranges(run), "" if patient else " (fallbacks available)")
        # one at a time: a failure never lets a later chapter in
        return Run(key, m, todo, 1, patient, True, end=j)

    def _take_instead(self, key: str, skip: set) -> tuple[SourceMatch, list[float]]:
        """Out of order, when every site of the round is busy: the chapters of
        the round an entry on site `key` lists too (alternatives), taken out
        of their groups; of several entries there, the one that lists most."""
        picks: dict[int, list[float]] = {}
        entry: dict[int, SourceMatch] = {}
        for g in self.round:
            for n in g.nums:
                c = None if n in skip else next((c for c in self._instead(n) if lanes_key(c.source.name) == key),
                                                None)
                if c is not None:
                    picks.setdefault(c.manga_id, []).append(n)
                    entry[c.manga_id] = c
        mid = max(picks, key=lambda i: len(picks[i]))
        taken = set(picks[mid])
        for g in self.round:
            g.nums = [n for n in g.nums if n not in taken]
        self.round = [g for g in self.round if g.nums]
        return entry[mid], sorted(taken)

    def record(self, run: Run, ok: list, failed: list, why: dict) -> None:
        """What came of a run: delivered chapters are done, failed ones move
        on to their next source (or fail for good)."""
        m, plan = run.match, self.plan
        for n in ok:
            self.results[n] = "ok"
        self.attempts += [(m.source.name, n, "ok") for n in ok] + [(m.source.name, n, "failed") for n in failed]
        if run.in_order:
            if not failed:
                # all of it arrived: go on after it; cut short (a cancel), wants() goes
                # on from the first chapter that did not arrive
                if all(self.results.get(c.number) == "ok" for c in run.todo):
                    self.idx = run.end
                return
            if not ok:
                self._delivered_nothing(m)
            for x in failed:
                self.used[x].add(m.manga_id)
                self.tried.setdefault(x, []).append(f"{m.source.name}: {why.get(x, 'failed')}")
                self.held.pop(x, None)
            self.idx = self.order.index(min(failed))    # resume at the first chapter that did not arrive
            return
        if not failed:
            return
        if not ok:
            self._delivered_nothing(m)
        for n in failed:
            self.used[n].add(m.manga_id)
            self.tried.setdefault(n, []).append(f"{m.source.name}: {why.get(n, 'failed')}")
            self.held.pop(n, None)
            nxt = next((c for c in plan.candidates[n] if c.manga_id not in self.used[n]), None)
            if nxt is not None:
                log.info("%s: ch %g failed on %s, will try %s", self.label, n, m.source.name, nxt.source.name)
                self.pending.add(n)
            else:
                self.results[n] = "failed"
                only_one = len(plan.candidates[n]) == 1
                self.reasons[n] = ("; ".join(self.tried[n]) +
                                   (" (no other source has this chapter)" if only_one else ""))
                log.warning("%s: ch %g failed: %s", self.label, n, self.reasons[n])

    def _delivered_nothing(self, m: SourceMatch) -> None:
        """A run of entry m delivered nothing: the other entries get its
        chapters first from now on, and it is asked only for the ones they
        fail too (a short run may have hit just one broken chapter)."""
        if m.manga_id not in self.dead:
            self.dead.add(m.manga_id)
            log.warning("%s: %s delivered nothing this run; asking the other sources first from now on",
                        self.label, m.source.name)

    def not_started(self, run: Run, nums: list[float], queued: bool = True) -> None:
        """Chapters Suwayomi did not start from the run's source (its queue
        was busy with other downloads; memo.unstarted), or that were not
        even queued there because it did not start others a short while ago
        (queued=False; memo.unqueued): no verdict on the source. Each goes
        on to its next source; one with no source left is not attempted this
        time (UNSTARTED_REASON, or busy_reason when it was never queued), and
        in order the series waits there."""
        name = run.match.source.name
        tried, reason = (UNSTARTED_TRIED, UNSTARTED_REASON) if queued else (BUSY_TRIED, busy_reason(name))
        for n in nums:
            self.used[n].add(run.match.manga_id)
            self.tried.setdefault(n, []).append(f"{name}: {tried}")
            self.held[n] = reason
        if self.in_order:
            self.idx = self.order.index(min(nums))
        else:
            self.pending.update(nums)

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
             in_order: bool | None = None, dropped: Callable[[], set] | None = None,
             attempts: list | None = None) -> dict:
    """Returns {chapter_number: 'ok' | 'failed'} for every chapter attempted.
    Chapters not reached before a cancel are simply absent. When `reasons`
    is given it is filled with a human-readable reason per failed chapter,
    and `attempts` with (source name, chapter, 'ok' | 'failed') for every
    chapter a source delivered or failed (SeriesSteps.attempts).
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
                if memo.unstarted:
                    steps.not_started(run, memo.unstarted)
                    memo.unstarted = []
                if memo.unqueued:
                    steps.not_started(run, memo.unqueued, queued=False)
                    memo.unqueued = []
    except Cancelled:
        log.warning("%s: download cancelled; %d done", label, sum(1 for r in steps.results.values() if r == "ok"))
    if attempts is not None:
        attempts.extend(steps.attempts)
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
    if memo.unstarted and not ok:                   # Suwayomi never got to it
        return False, [chapter.number], {chapter.number: UNSTARTED_REASON}
    if memo.unqueued and not ok:
        return False, [chapter.number], {chapter.number: busy_reason(source_name)}
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
    other downloads) ends the call with it and the chapters after it in
    memo.unstarted; at a source whose chapters it did not start a short
    while ago (memo.queue_busy) nothing is queued, and the chapters are in
    memo.unqueued: that is no verdict on the source either. On
    a page-by-page source (`warm`) each chapter's pages
    are fetched one by one before it is queued; one whose pages the image
    server keeps refusing is backed off like a rate limit without ever
    being queued, and one Suwayomi does not build from its cache gets its
    pages fetched once more (memo.rewarmed)."""
    ok, failed, why = [], [], {}
    ops = with_cancel(client, cancel)
    i, size, backoff = 0, batch, 0
    waited = 0                                      # the longest backoff waited since the last success
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
        if memo.queue_busy(source_name):
            memo.unqueued = [c.number for c in todo[i:] if c.number not in skip]
            log.info("%s: Suwayomi did not start chapters of %s within %d min lately (its queue is busy); not "
                     "queueing ch %s there now", label, source_name, QUEUED_CAP_SECS // 60, ranges(memo.unqueued))
            break
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
        fell_back = ""                              # page by page was skipped: the chapter's reasons say so
        if warmed is not None and warmed.state == "no_pages":
            fell_back = f"not fetched page by page ({warmed.why}); "
        if warmed is not None and warmed.state in ("refused", "deadline"):
            outcome, got = warmed.state, []         # never queued: straight to the backoff below
        else:
            t_start = time.monotonic()
            outcome, unsure = "error", False
            try:
                try:
                    _enqueue(ops, ids)
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
                memo.note_unstarted(source_name)
                memo.unstarted = [c.number for c in todo[i:] if c.number not in skip]
                metrics.record_unstarted(source_name)
                log.warning("%s: Suwayomi did not start ch %s from %s within %d min (its queue is busy); trying "
                            "ch %s on other sources, or leaving them for the next pass", label,
                            ranges([c.number for c in chunk]), source_name, QUEUED_CAP_SECS // 60,
                            ranges(memo.unstarted))
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
                    why[c.number] = (fell_back + "the normal download failed instantly on every try" if fell_back
                                     else "the source has no working pages for this chapter (failed instantly on "
                                          "every try)")
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
            # a warm-up would not follow the longest backoff: give up at once (a lane
            # rests the site instead of holding it)
            if not (outcome in ("refused", "deadline") and backoff >= max_backoff):
                log.warning("%s: %s %s on ch %g - backing off %ds", label, source_name, did, chunk[0].number,
                            backoff)
                said = "refused the request" if outcome == "stalled" else did
                report(f"{source_name} {said} (rate limiting): waiting {backoff} s before retrying chapter "
                       f"{chunk[0].number:g} ({len(ok)} of {len(todo)} done)")
                if limits.pause(backoff, cancel):
                    break                           # cancelled: these chapters were simply not reached
                waited = backoff
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
                after = f", gave up after {waited}s of backoff" if waited else ""
                for c in rest:
                    why[c.number] = (fell_back if c in chunk else "") + what + after
                break
            continue
        ok.extend(c.number for c in got)
        missed = [c.number for c in chunk if c.id not in have]
        failed.extend(missed)
        for n in missed:
            why[n] = fell_back + "Suwayomi finished the batch without this chapter (download error)"
        if missed:
            log.warning("%s: %s failed ch %s", label, source_name, ranges(missed))
            if stop_on_fail:
                break
        backoff = waited = 0
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


def _enqueue(ops, ids: list[int]) -> None:
    """Queue a chunk. An id still on the leftover list (an earlier dequeue of
    that chapter failed) is ours again once it is queued: it comes off the
    list, in turn with clear_leftovers, which would otherwise take it back
    out of the queue under this chunk."""
    if not set(ids) & set(leftovers()):
        ops.enqueue(ids)
        return
    with _clearing:
        ops.enqueue(ids)
        if not _forget_leftovers(ids):
            _retry_leftovers_later(ops)             # saves the shorter list once the database takes it


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
    being downloaded right now (in a pass, a lane queuing one of them
    again waits for this: _enqueue). Returns True when nothing is left
    over; a failure is logged at debug level and tried again later. A
    cancel (should_cancel) raises Cancelled; the ids stay remembered."""
    if not leftovers():
        return True
    with _clearing:
        return _clear_leftovers(client, should_cancel)


def _clear_leftovers(client, should_cancel: Callable[[], bool] | None) -> bool:
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
    when a download run holds it (never waits). For the background retry
    only: _retry_holds says so meanwhile."""
    path = path or config.LOCK_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    _retry_holds.set()                  # before the attempt: a restore that finds the lock taken meanwhile waits
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _retry_holds.clear()
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
        _retry_holds.clear()
        os.close(fd)


@contextmanager
def leftover_retry_held_off():
    """For a restore, while it takes and holds the download lock: the
    background retry of clear_leftovers lets go of the lock at once and does
    not take it again. Yields a function telling whether the retry still
    holds the lock (the restore waits RETRY_LET_GO_SECS at most for it)."""
    _retry_yield.set()
    try:
        yield _retry_holds.is_set
    finally:
        _retry_yield.clear()


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
    holds the lock: that run clears them (at its start; a pass before every
    lane step), and every change to the stored list is made under the lock
    (a restore relies on it). A restore that waits for the lock is let in at
    once (leftover_retry_held_off)."""
    delay = LEFTOVER_RETRY_SECS if first is None else first
    for _ in range(LEFTOVER_RETRY_TRIES):
        threading.Event().wait(delay)               # not time.sleep: tests patch that out
        try:
            if not _retry_yield.is_set():           # a restore is waiting for the lock, or holds it
                with _lock_if_free() as free:
                    if free and not _retry_yield.is_set():
                        if _unsaved != _SAVED:
                            _store_leftovers(lambda cur: cur)   # cur already includes them: this only writes them
                        clear_leftovers(client, _retry_yield.is_set)
        except Cancelled:
            log.debug("a restore needs the download lock; retrying the chapter ids left in Suwayomi's queue later")
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
