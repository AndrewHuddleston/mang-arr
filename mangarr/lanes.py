"""Download lanes: a refresh pass downloads from several sites at once.

A pass resolves its series one after the other on the job thread and hands
every series with chapters due to the LanePool, then goes on resolving. The
pool has one worker thread per lane. A free lane takes the first waiting
series (in pass order) that wants a site no other lane is busy with, runs
one step of it (one source entry: downloader.SeriesSteps.take, then
downloader._download_source) and gives the site back. So a site serves one
series at a time and keeps its pacing, and a slow or rate-limited site holds
up only the series that need it. When no waiting series can have the site it
wants next, a free lane takes the first one that can get the same chapters
from another free site of no worse a tier (SeriesSteps.alternatives), rather
than stay idle while the series waits; the site it skipped is still tried
if that one fails. What to fetch next for a series, fallbacks,
download in order and every reason are the same SeriesSteps a single-series
download uses; what came of a series is written once it is finished
(core.record_downloads, then core.finish_download imports it).

The lane count is the Download Lanes setting, but never more than Suwayomi's
own 'max sources in parallel' (read, never written: effective_lanes). The
pool is only created once a series has chapters due, so a pass with nothing
to download never asks Suwayomi for it.

The download lock is taken when the first series is handed over and held
until the pass ends (shutdown), across the lane threads: still one download
run per process and across processes, and a restore still cannot run
meanwhile. The pass only ends once every lane has ended, however it ended,
so nothing of it writes after that.

Suwayomi not answering is counted per outage, not per failure (Outages):
when several lanes and the resolve step run into one outage at once, the
pass holds for one breaker window and goes on; only a second outage right
after that stops it.
"""
import bisect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config, core, db, downloader, limits, metrics, settings, stuck
from .resolver import Plan
from .suwayomi import BREAKER_SECS, SuwayomiUnreachable, with_cancel

log = logging.getLogger(__name__)

# How long the pass holds after Suwayomi stopped answering (one breaker
# window) before it tries again; tests shorten it.
HOLD_SECS = BREAKER_SECS
# series resolved and waiting for a lane at most; the resolve step waits beyond that
PIPELINE_MAX_WAITING = 50
# The end of a pass waits for every lane to finish its step, however long
# that takes (a download step is cut short, writing and importing what
# arrived is not); after this long it says in the log what it waits for.
SHUTDOWN_JOIN_SECS = 100
LANE_DIED = "download worker stopped unexpectedly"
# job.lanes entry of a lane between steps
_IDLE = {"source": None, "series_id": None, "title": None, "text": "", "since": None}
# A series whose next site is busy or resting may take its chapters from
# another free site that lists them (see the module docstring); tests turn it
# off to compare.
TAKE_FREE_SITE = True
# item states of a series the pass is done with ("skipped": complete and finished, not checked this time)
FINISHED = ("done", "nomatch", "failed", "error", "skipped")


class PoolStopped(RuntimeError):
    """The lanes stopped (the pass was cancelled, or Suwayomi is not
    answering) while a series was being handed over."""


def effective_lanes(client) -> tuple[int, int | None]:
    """(lanes to use this pass, Suwayomi's 'max sources in parallel' or None
    when it was not asked or could not be read). The Download Lanes setting,
    but never more than Suwayomi downloads from at once: more lanes would only
    queue chapters it does not start. Suwayomi's setting is only read."""
    want = int(limits.setting("download_lanes"))
    if want <= 1:
        return 1, None
    cap = client.max_sources_in_parallel()
    if cap is None:
        log.warning("could not read Suwayomi's 'max sources in parallel'; downloading from one source at a time "
                    "this pass")
        return 1, None
    n = max(1, min(want, cap))
    if n < want:
        log.warning("Suwayomi downloads from at most %d source(s) at once (its 'max sources in parallel'); using %d "
                    "download lane(s) instead of %d - raise it on the Settings page", cap, n, want)
    return n, cap


class Outages:
    """Suwayomi not answering, as one pass sees it: from the resolve step and
    from every lane. The first report starts an episode: the pass holds for
    HOLD_SECS (one breaker window), and reports during that hold belong to
    the same episode (several lanes see one outage at once). Once the hold
    is over (served), another report before anything worked again (ok) is a
    second episode and stops the pass: wait one window, then stop if
    Suwayomi still does not answer, as a pass always did."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.count = 0                  # episodes in a row, with nothing working in between
        self.pending = False            # an episode's hold has not been served yet
        self.hold_until = 0.0
        self.stopped = False
        self.why = ""

    def report(self, e: Exception) -> str:
        """Suwayomi did not answer: 'hold' (wait, then go on) or 'stop'."""
        with self._lock:
            if self.stopped:
                return "stop"
            if self.pending:
                return "hold"
            self.count += 1
            if self.count >= 2:
                self.stopped, self.why = True, str(e)[:200]
                return "stop"
            self.pending, self.hold_until = True, self._clock() + HOLD_SECS
            return "hold"

    def served(self) -> None:
        """The hold is over: the next report is a new episode."""
        with self._lock:
            self.pending = False

    def ok(self) -> None:
        """Suwayomi answered: the count starts again (not during a hold,
        which a lane that finished just before it would otherwise undo)."""
        with self._lock:
            if not self.pending:
                self.count = 0

    def hold_left(self) -> float:
        with self._lock:
            return max(0.0, self.hold_until - self._clock()) if self.pending else 0.0


@dataclass(eq=False)
class SeriesTask:
    """One series of the pass, from its hand-over to the pool until it is written."""
    index: int                          # position in the pass: earlier series go first
    series_id: int
    title: str
    item: dict                          # its entry in job.items
    plan: Plan
    wanted: list
    steps: downloader.SeriesSteps
    seen: set = field(default_factory=set)      # sources that rate-limited it (recorded when it is written)
    told: set = field(default_factory=set)      # chapters already logged as no longer wanted
    want: list = field(default_factory=list)    # the sites it could download from next
    alts: list = field(default_factory=list)    # ... or else, while those are busy (SeriesSteps.alternatives)
    names: dict = field(default_factory=dict)   # site -> a source name, for the texts
    ran: bool = False                   # a step started: something may have to be written
    cut: bool = False                   # stopped by a cancel or a stopped pass before it was done
    error: Exception | None = None
    deleted: bool = False


class LanePool:
    """Download lanes for one pass (see the module docstring). The resolve
    step calls submit() for each series with chapters due, then close() and
    join(); shutdown() ends it however the pass ended. One condition (_cv)
    guards all the state below; nothing does I/O, sleeps or calls back while
    holding it. A lane that dies loses only the series it held (written at
    the end with LANE_DIED); the others go on, and only when no lane is left
    does the pool stop."""

    def __init__(self, client, job, lanes: int, label: str, outages: Outages,
                 on_error: Callable[[int, Exception], None] | None = None, cap: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.job, self.lanes, self.label = client, job, max(1, int(lanes)), label
        self.outages, self.on_error, self.cap, self.clock = outages, on_error, cap, clock
        self.shared = downloader.PassShared()
        self._cv = threading.Condition()
        self._waiting: list[SeriesTask] = []        # by index
        self._busy: dict[str, SeriesTask] = {}      # site -> the series a lane runs on it
        self._claimed: dict[int, SeriesTask] = {}   # lane -> the series it runs a step of
        self._finishing: dict[int, SeriesTask] = {}     # lane -> the series it writes
        self._lost: list[SeriesTask] = []           # held by a lane that died: written at the end
        self._ready_at: dict[str, float] = {}       # site -> when it may serve the next series (pacing)
        self._closed = self._stopping = False
        self._crashed = False                       # every lane died with work left
        self._dead = 0                              # lanes that died
        self._stop_why = ""
        self._lane_state: dict[int, dict] = {}      # lane -> {source, series_id, title, text, since}
        self._resolving = ""
        self._ending = ""                           # what the end of the pass waits for
        self.downloaded = self.imported = self.errors = 0
        self._threads: list[threading.Thread] = []
        self._running = 0                           # lanes started and not ended
        self._fd: int | None = None                 # the download lock, from the first submit to shutdown

    def cancelled(self) -> bool:
        return self.job.cancel or self._stopping

    @property
    def stop_why(self) -> str:
        return self._stop_why

    def _in_flight(self) -> int:
        """Under _cv: series a lane runs a step of or writes right now."""
        return len(self._claimed) + len(self._finishing)

    def start(self) -> None:
        """Start the lanes. When the system refuses a thread (a thread or
        process limit), the pass goes on with the lanes it got; RuntimeError
        when it got none."""
        with self._cv:
            self._running = self.lanes              # all at once: a lane that dies at once is not the last
        for k in range(1, self.lanes + 1):
            t = threading.Thread(target=self._worker, args=(k,), name=f"mangarr-lane-{k}", daemon=True)
            try:
                t.start()
            except RuntimeError as e:
                with self._cv:
                    self._running -= self.lanes - k + 1     # they never ran: join() must not wait for them
                    if k == 1:
                        raise
                    log.warning("%s: could start only %d of %d download lane(s) (%s); going on with those",
                                self.label, k - 1, self.lanes, e)
                    self.lanes = k - 1
                    self._all_died()
                break
            self._threads.append(t)
        log.info("%s: %d download lane(s), Suwayomi allows %s source(s) in parallel", self.label, self.lanes,
                 "?" if self.cap is None else self.cap)

    # -- the resolve step's side ---------------------------------------------------------------

    def submit(self, index: int, series_id: int, title: str, item: dict, plan: Plan, wanted: list,
               progress: Callable[[str], None] | None = None) -> None:
        """Hand over a resolved series with chapters due. The first call
        takes the download lock (waiting for another run like any download
        does: LockBusy, Cancelled). Waits while PIPELINE_MAX_WAITING series
        already wait for a lane; PoolStopped when the lanes stop meanwhile."""
        if self._fd is None:
            self._fd = downloader.acquire_download_lock(should_cancel=self.cancelled, progress=progress)
        with self._cv:
            while len(self._waiting) >= PIPELINE_MAX_WAITING and not self.cancelled() and self._running:
                self._resolving = f"waiting: {len(self._waiting)} series queued for download lanes"
                self._render()
                self._cv.wait(1.0)
            if self.cancelled():
                raise PoolStopped(self._stop_why or "cancelled")
        task = SeriesTask(index, series_id, title, item, plan, list(wanted),
                          downloader.SeriesSteps(plan, set(wanted), bool(settings.get("download_in_order")), title,
                                                 {}))
        for m in plan.matches:
            task.names.setdefault(downloader.lanes_key(m.source.name), m.source.name)
        with db.connect() as con:
            gone = self._gone(con, task)
            self._next(task, gone())
        if not task.want:                           # nothing any source can be asked for: written at once
            self._finalize(task)
            with self._cv:
                self._render()
            return
        with self._cv:
            item["state"], item["result"] = "waiting", f"resolved: {len(wanted)} chapter(s) due; waiting for a " \
                                                       "download lane"
            bisect.insort(self._waiting, task, key=lambda t: t.index)
            self._cv.notify_all()
            self._render()

    def resolving(self, text: str) -> None:
        """What the resolve step is doing, for the pass progress line."""
        with self._cv:
            self._resolving = text
            self._render()

    def close(self) -> None:
        """No more series: the lanes end once every series is finished."""
        with self._cv:
            self._closed = True
            self._resolving = ""
            self._cv.notify_all()
            self._render()

    def stop(self, why: str) -> None:
        """Take no further steps: every lane ends after the one it is in
        (cut short: the steps see cancelled())."""
        with self._cv:
            self._stop_locked(why)

    def _stop_locked(self, why: str) -> None:
        if not self._stopping:
            self._stopping, self._stop_why = True, why
            log.info("%s: stopping the download lanes: %s", self.label, why)
        self._cv.notify_all()

    def join(self) -> None:
        """After close(): wait until every lane has ended (every series is
        finished, or the lanes stopped). A cancel of the job stops them. A
        lane that died has ended; the others finish the work."""
        with self._cv:
            while self._running > 0 and any(t.is_alive() for t in self._threads):
                if not self._stopping and self.job.cancel:
                    self._stop_locked("cancelled")
                self._cv.wait(1.0)

    def counts(self) -> tuple[int, int, int]:
        """(chapters downloaded, imported, series errors) of the series written so far."""
        with self._cv:
            return self.downloaded, self.imported, self.errors

    def shutdown(self) -> None:
        """End of the pass, however it ended: stop the lanes and wait until
        every one has ended (a step is cut short by the stop; writing and
        importing what arrived is not, and is waited for however long it
        takes, so nothing of the pass writes once it is over and the lock
        is free), write the series still waiting that had started and those
        of lanes that died, mark the others, give the download lock back
        and clear the lanes from the Activity page. Never raises."""
        try:
            with self._cv:
                if self._running or self._waiting:     # the pass did not get to the end of join()
                    self._stop_locked("cancelled" if self.job.cancel else "the pass ended early")
                self._stopping = True
                if any(t.is_alive() for t in self._threads):
                    self._ending = "ending: waiting for the download lanes to finish their step"
                    self._render()
            t0 = time.monotonic()
            for t in self._threads:
                t.join(max(0.0, t0 + SHUTDOWN_JOIN_SECS - time.monotonic()))
                if t.is_alive():
                    log.warning("%s: %s is still busy %d s after the pass ended (an import is not cut short); "
                                "waiting for it", self.label, t.name, SHUTDOWN_JOIN_SECS)
                    t.join()
            with self._cv:
                left = list(self._waiting) + self._lost
                self._waiting.clear()
                self._lost = []
            for task in left:
                self._settle(task)
        except Exception:
            log.exception("%s: ending the download lanes failed", self.label)
        finally:
            if self._fd is not None:
                try:
                    downloader.release_download_lock(self._fd)
                except OSError as e:
                    log.warning("%s: could not release the download lock: %s", self.label, e)
                self._fd = None
            with self._cv:
                self._lane_state.clear()
                self._ending = ""
                self.job.lanes = []
                self.job.active_series_ids = frozenset()
            metrics.record_lanes(0, 0, 0)

    def _settle(self, task: SeriesTask) -> None:
        """A series still waiting when the pass ended, or held by a lane that
        died: written when a step of it ran (what arrived is kept),
        otherwise only marked."""
        item = task.item
        if self._crashed and task.error is None:
            task.error = RuntimeError(LANE_DIED)
        try:
            if task.ran or task.deleted:
                task.cut = True
                self._finalize(task)
            elif task.error is not None:
                item["state"], item["result"] = "error", str(task.error)[:300]
                with self._cv:
                    self.errors += 1
            else:
                item["state"], item["result"] = "cancelled", self._stopped_text()
        except Exception:
            log.exception("%s: %s: could not write what its downloads did", self.label, task.title)

    def _stopped_text(self) -> str:
        return "pass cancelled" if self.job.cancel else f"pass stopped: {self._stop_why}"

    # -- the lanes -----------------------------------------------------------------------------

    def _worker(self, lane: int) -> None:
        try:
            while True:
                with self._cv:
                    while True:
                        if self._stopping:
                            return
                        if self._closed and not self._waiting and not self._in_flight():
                            return
                        task, key, wait = self._pick()
                        if task is not None:
                            break
                        self._retext()                  # countdowns go on while no lane is busy
                        self._cv.wait(min(wait, 1.0) if wait > 0 else 1.0)
                    self._claim(lane, task, key)
                finished, rest = True, 0.0
                try:
                    finished, rest = self._step(task, key, lane)
                except Exception as e:
                    task.error = e
                    log.exception("%s: %s: a download step failed", self.label, task.title)
                finally:
                    self._release(lane, task, key, finished, rest)
                if finished:
                    try:
                        self._finalize(task)
                    except Exception as e:
                        log.exception("%s: %s: could not write what its downloads did", self.label, task.title)
                        task.item["state"], task.item["result"] = "error", f"{type(e).__name__}: {e}"[:300]
                        with self._cv:
                            self.errors += 1
                    finally:
                        with self._cv:
                            self._finishing.pop(lane, None)
                            self._cv.notify_all()
                            self._render()
        except BaseException:
            log.exception("%s: download lane %d stopped unexpectedly", self.label, lane)
            with self._cv:
                self._lane_died(lane)
        finally:
            with self._cv:
                self._running -= 1
                self._all_died()
                self._cv.notify_all()

    def _all_died(self) -> None:
        """Under _cv: when no lane is left and one died with work left (or
        more series to come), the pool stops: nothing would run them."""
        if not self._running and self._dead and not self._stopping and (self._waiting or not self._closed):
            self._crashed = True
            self._stop_locked(LANE_DIED)

    def _lane_died(self, lane: int) -> None:
        """Under _cv: lane `lane` died. The series it held gives its site
        back, so the other lanes can serve that site, and is written at the
        end of the pass with LANE_DIED; the other lanes go on."""
        self._dead += 1
        self._lane_state.pop(lane, None)
        lost = self._claimed.pop(lane, None) or self._finishing.pop(lane, None)
        if lost is not None:
            for key in [k for k, t in self._busy.items() if t is lost]:
                del self._busy[key]
            if lost.error is None:
                lost.error = RuntimeError(LANE_DIED)
            self._lost.append(lost)
        self._cv.notify_all()

    @staticmethod
    def _next(task: SeriesTask, skip: set) -> None:
        """What `task` can download from next (want), and instead (alts)."""
        task.want = task.steps.wants(skip)
        task.alts = task.steps.alternatives(skip) if TAKE_FREE_SITE and task.want else []

    def _pick(self) -> tuple[SeriesTask | None, str | None, float]:
        """Under _cv: the first waiting series (pass order) with a site it
        wants that is free and rested, as (task, site, 0); else the first
        one with such a site among its alternatives; otherwise (None, None,
        seconds until that may change, 0 when unknown). Wants go first, so a
        series never takes as its alternative the site a later series needs
        and could have now."""
        left = self.outages.hold_left()
        if left > 0:
            return None, None, left
        self.outages.served()                   # a hold that just ended: the next outage is a new one
        now, soonest = self.clock(), 0.0
        for alts in (False, True):
            for task in self._waiting:
                for key in task.alts if alts else task.want:
                    if key in self._busy:
                        continue
                    at = self._ready_at.get(key, 0.0)
                    if at > now:
                        soonest = at - now if not soonest else min(soonest, at - now)
                        continue
                    return task, key, 0.0
        return None, None, soonest

    def _retext(self) -> None:
        """Under _cv: every waiting series says what it waits for now. Run on
        every change (_render) and by idle lanes, so a text never names a
        site that is free again, nor shows the download line of a step that
        is over."""
        left, now = self.outages.hold_left(), self.clock()
        for task in self._waiting:
            self._say(task, self._wait_text(task, left, now))

    def _wait_text(self, task: SeriesTask, left: float, now: float) -> str:
        """What `task` waits for: Suwayomi, a lane (one of its sites is free),
        or else its first site (busy with another series, or resting)."""
        if left > 0:
            return f"waiting: Suwayomi is not answering, trying again in {left:.0f} s"
        first = ""
        for key in task.want + task.alts:       # the text names a site it wants; a free alternative: a lane
            name = task.names.get(key, key)
            other = self._busy.get(key)
            at = self._ready_at.get(key, 0.0)
            if other is not None:
                why = f"waiting for {name}: busy with {other.title}"
            elif at > now:
                why = f"waiting for {name}: paced, next chapter in {at - now:.0f} s"
            elif task.ran:
                return "waiting for a download lane"
            else:
                return f"resolved: {len(task.wanted)} chapter(s) due; waiting for a download lane"
            first = first or why
        return first or "waiting for a download lane"

    @staticmethod
    def _say(task: SeriesTask, text: str) -> None:
        if task.item.get("result") != text:
            task.item["result"] = text

    def _claim(self, lane: int, task: SeriesTask, key: str) -> None:
        """Under _cv: lane `lane` runs a step of `task` on site `key`."""
        self._waiting.remove(task)
        self._busy[key] = task
        self._claimed[lane] = task
        task.item["state"] = "running"
        self._lane_state[lane] = {"source": task.names.get(key, key), "series_id": task.series_id,
                                  "title": task.title, "text": "", "since": time.time()}
        log.debug("%s: lane %d takes %s for %s", self.label, lane, key, task.title)
        self._render()

    def _release(self, lane: int, task: SeriesTask, key: str, finished: bool, rest: float) -> None:
        """After a step: the site is free again (after `rest` s when it is
        paced); an unfinished series waits for its next step."""
        with self._cv:
            self._busy.pop(key, None)
            self._claimed.pop(lane, None)
            self._lane_state.pop(lane, None)
            if rest > 0:
                self._ready_at[key] = max(self._ready_at.get(key, 0.0), self.clock() + rest)
            if finished:
                self._finishing[lane] = task            # still the pass's until it is written
            else:
                task.item["state"] = "waiting"
                bisect.insort(self._waiting, task, key=lambda t: t.index)
            self._cv.notify_all()
            self._render()

    def _gone(self, con, task: SeriesTask) -> Callable[[], set]:
        return downloader._dropper(lambda: core.dropped_chapters(con, task.series_id, set(task.wanted)),
                                   task.title, told=task.told)

    def _step(self, task: SeriesTask, key: str, lane: int) -> tuple[bool, float]:
        """One run of `task` on site `key` (on the lane's thread). Queue
        entries a failed dequeue left behind (an earlier run, or a step of
        this pass during an outage) are taken out first, as a download run
        does at its start: the background retry cannot while the pass holds
        the lock, and a stray chapter would sit in Suwayomi's queue on this
        site ahead of the series. Returns (the series is finished, seconds
        the site rests before its next series: the rate-limit pause, longer
        after a source was given up)."""
        if self.cancelled():
            task.cut = True
            return True, 0.0
        with db.connect() as con:
            if not db.get_series(con, task.series_id):
                task.deleted = True
                log.info("%s: %s was deleted while it waited for a download lane", self.label, task.title)
                return True, 0.0
            gone = self._gone(con, task)
            run = task.steps.take(key, gone())
            if run is None:                     # its chapters there were ignored meanwhile
                self._next(task, gone())
                return not task.want, 0.0
            memo = downloader.RunMemo(throttle=task.seen, shared=self.shared)
            task.ran = True
            src = run.match.source
            t0 = self.clock()
            try:
                downloader.clear_leftovers(self.client, self.cancelled)
                ok, failed, why = downloader._download_source(
                    self.client, run.match.manga_id, run.todo, run.batch, task.title, src.name, run.patient,
                    self.cancelled, self._reporter(task, lane), memo, stop_on_fail=run.in_order,
                    throttled=src.throttled, warm=src.page_warm, gone=gone)
            except downloader.Cancelled:
                task.cut = True
                return True, 0.0
            except SuwayomiUnreachable as e:
                task.error = e
                log.warning("%s: %s: Suwayomi is not answering: %s", self.label, task.title, e)
                if self.outages.report(e) == "stop":
                    self.stop("Suwayomi is not answering")
                return True, 0.0
            finally:
                metrics.record_lane_time(src.name, self.clock() - t0)
            task.steps.record(run, ok, failed, why)
            if memo.unstarted:
                task.steps.not_started(run, memo.unstarted)
            if memo.unqueued:
                task.steps.not_started(run, memo.unqueued, queued=False)
            self.outages.ok()
            rest = limits.setting("throttled_delay_seconds") if src.throttled or self.shared.seen(src.name) else 0.0
            if src.name in memo.gave_up:
                rest += config.BACKOFF_MAX_WITH_FALLBACK
            self._next(task, gone())
            if task.want and self.cancelled():
                task.cut, task.want, task.alts = True, [], []
            return not task.want, rest

    def _reporter(self, task: SeriesTask, lane: int) -> Callable[[str], None]:
        def report(m: str) -> None:
            with self._cv:
                task.item["result"] = m
                st = self._lane_state.get(lane)
                if st is not None and st["series_id"] == task.series_id:
                    st["text"] = m
                self._render()
        return report

    def _finalize(self, task: SeriesTask) -> None:
        """Write what the downloads of a finished series did and import them
        (outside _cv, with its own connection); set its item and add it to
        the counts. A series the pass stopped before any step of it ran is
        only marked."""
        item, sid = task.item, task.series_id
        if task.deleted:
            item["state"], item["result"] = "cancelled", "series was deleted"
            return
        if not task.ran and task.error is None and self.cancelled():
            item["state"], item["result"] = "cancelled", self._stopped_text()
            return
        steps = task.steps
        out = core.Outcome(sid, task.plan, results=steps.results)
        with db.connect() as con:
            if not db.get_series(con, sid):
                item["state"], item["result"] = "cancelled", "series was deleted"
                return
            core.record_downloads(con, sid, task.plan, task.wanted, steps.results, steps.reasons, task.seen,
                                  steps.attempts)
            try:
                core.finish_download(con, with_cancel(self.client, self.cancelled), out)
            except core.Gone:
                item["state"], item["result"] = "cancelled", "series was deleted"
                return
            # the chapter the series stopped at, if any (after the import: what arrived counts as on disk)
            stuck.update(con, with_cancel(self.client, self.cancelled), sid, task.plan.series, task.plan)
            if task.error is not None:
                e = task.error
                item["state"], item["result"] = "error", f"{type(e).__name__}: {e}"[:300]
            elif task.cut:
                item["state"] = "cancelled"
                item["result"] = self._stopped_text() + (f" after {out.downloaded} chapter(s) downloaded"
                                                         if out.downloaded else "")
            else:
                item["state"], item["result"] = core.describe_outcome(con, sid, out)
        if task.error is not None and self.on_error is not None:
            try:                                # bookkeeping must never end the pass
                self.on_error(sid, task.error)
            except Exception as rec:
                log.warning("%s: could not record the error for %s: %s: %s", self.label, task.title,
                            type(rec).__name__, rec)
        with self._cv:
            self.downloaded += out.downloaded
            self.imported += out.imported
            self.errors += task.error is not None

    def _render(self) -> None:
        """Under _cv: what the Activity page shows (job.lanes, the progress
        line, the series being downloaded) and the lane gauges. Replaced,
        never changed in place, so a page render never sees half of it."""
        job = self.job
        self._retext()
        job.lanes = [{"lane": k, **self._lane_state.get(k, _IDLE)} for k in range(1, self.lanes + 1)]
        job.active_series_ids = frozenset([t.series_id for t in self._busy.values()] +
                                          [t.series_id for t in self._finishing.values()])
        done = sum(1 for it in job.items if it.get("state") in FINISHED)
        parts = [f"{done}/{len(job.items)} done"]
        if self._ending:
            parts.append(self._ending)
        if self._resolving:
            parts.append(f"resolving {self._resolving}")
        busy = [f"{s['title']}: {s['text']}" if s["text"] else f"{s['source']} - {s['title']}"
                for _, s in sorted(self._lane_state.items())]
        parts.append(f"lanes {len(self._busy)}/{self.lanes}" + (": " + " | ".join(busy) if busy else ""))
        if self._waiting:
            parts.append(f"{len(self._waiting)} waiting")
        job.progress = "; ".join(parts)[:600]
        metrics.record_lanes(self.lanes, len(self._busy), len(self._waiting))
