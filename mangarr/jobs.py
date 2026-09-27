"""In-process job runner and scheduler.

Adding, refreshing and downloading take minutes to hours, so the web layer
never runs them inline: it submits a Job and the single worker thread runs
jobs one at a time (Suwayomi has one download queue, so two download jobs
would fight over it). Within one job, a refresh pass (refresh all, search
wanted) downloads from up to Download Lanes sites at once through its lanes
(lanes.LanePool), one series per site. The scheduler thread submits a
"refresh all" job every REFRESH_HOURS. Every job's progress and outcome is
visible on the Activity page and in the log.
"""
import collections
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import config, limits, metrics

log = logging.getLogger(__name__)

HISTORY = 300          # finished jobs kept for the Activity page and the API
MAX_QUEUED = 500       # queued jobs at most; beyond this submit() refuses (QueueFull)
ACTIVE = ("queued", "running")


class QueueFull(RuntimeError):
    """Too many jobs are waiting; the caller should try again later (HTTP 429)."""


@dataclass
class Job:
    id: int
    kind: str                      # add | refresh | refresh-all | import | adopt
    title: str
    series_id: int | None = None
    status: str = "queued"         # queued | running | done | failed | cancelled
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: str = ""
    message: str = ""
    cancel: bool = False
    key: str | None = None         # dedupe key: one queued/running job per key (see Runner.submit)
    # the series a multi-series pass is working on right now (pending_for sees it):
    # the one it resolves, and the ones its download lanes download or write
    active_series_id: int | None = None
    active_series_ids: frozenset = frozenset()
    # a pass's download lanes at work: [{lane, source, series_id, title, text, since}]
    lanes: list = field(default_factory=list)
    # a multi-series job (refresh pass) lists every series it covers:
    # {series_id, title, state: queued|running|done|nomatch|failed|error|cancelled, result}
    items: list = field(default_factory=list)
    # when `progress` last changed (None: not yet); health warns about a running job that stops moving
    progress_at: float | None = None

    def __setattr__(self, name, value):
        if name == "progress" and value != self.__dict__.get("progress"):
            object.__setattr__(self, "progress_at", time.time())
        object.__setattr__(self, name, value)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "seriesId": self.series_id,
                "status": self.status, "queuedAt": self.queued_at, "startedAt": self.started_at,
                "finishedAt": self.finished_at, "progress": self.progress, "progressAt": self.progress_at,
                "message": self.message, "items": self.items, "lanes": self.lanes}


class Runner:
    def __init__(self, max_queued: int = MAX_QUEUED, history: int = HISTORY):
        # (job, fn) waiting to run, oldest first. A plain deque under _lock
        # rather than a queue.Queue, so that cancelling a queued job takes it
        # out at once: cancelled entries never pile up behind a long job.
        self._pending: collections.deque = collections.deque()
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)      # signalled when _pending gains an entry
        self._next = 1
        self.max_queued, self.history = max_queued, history
        self.current: Job | None = None
        self._thread = threading.Thread(target=self._loop, name="mangarr-jobs", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("job runner started")

    def submit(self, kind: str, title: str, fn, series_id: int | None = None, key: str | None = None) -> Job:
        """Queue fn(job). With a key, a job with the same key that is still
        queued or running is returned instead of queueing a duplicate. Raises
        QueueFull when max_queued jobs are already waiting."""
        with self._lock:
            if key is not None:
                same = next((j for j in self._jobs if j.key == key and j.status in ACTIVE), None)
                if same:
                    log.info("job #%d (%s) already %s; not queueing another %s %s", same.id, key, same.status,
                             kind, title)
                    return same
            waiting = sum(1 for j in self._jobs if j.status == "queued")
            if waiting >= self.max_queued:
                log.warning("job queue full (%d waiting); refused %s %s", waiting, kind, title)
                raise QueueFull(f"{waiting} jobs are already waiting; try again once some have run")
            job = Job(self._next, kind, title, series_id, key=key)
            self._next += 1
            self._jobs.append(job)
            self._trim()
            self._pending.append((job, fn))
            self._ready.notify()
        log.info("job #%d queued: %s %s", job.id, kind, title)
        return job

    def _trim(self) -> None:
        """Keep every queued/running job (they still run, so they must stay
        visible, cancellable and deduplicated) and the newest `history`
        finished ones. Caller holds _lock."""
        finished = [j for j in self._jobs if j.status not in ACTIVE]
        drop = len(finished) - self.history
        if drop > 0:
            gone = {id(j) for j in finished[:drop]}
            self._jobs = [j for j in self._jobs if id(j) not in gone]

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def active(self, *kinds: str) -> Job | None:
        """The newest queued or running job of one of these kinds, if any."""
        with self._lock:
            return next((j for j in reversed(self._jobs) if j.kind in kinds and j.status in ACTIVE), None)

    def cancel(self, job_id: int) -> bool:
        with self._lock:                   # the runner changes status under the same lock
            j = next((j for j in self._jobs if j.id == job_id), None)
            if not j or j.status not in ACTIVE:
                return False
            j.cancel = True                # a running job checks this between steps (and in its waits)
            if j.status == "queued":
                j.status = "cancelled"
                j.finished_at = time.time()
                # out of the waiting line now, not when the worker reaches it
                self._pending = collections.deque(e for e in self._pending if e[0] is not j)
            return True

    def pending_for(self, series_id: int) -> bool:
        """A job for this series is queued or running, including a pass
        (refresh-all, search wanted) that is working on it right now."""
        with self._lock:
            return any(j.status in ACTIVE and (series_id in (j.series_id, j.active_series_id)
                                               or series_id in j.active_series_ids) for j in self._jobs)

    def _loop(self) -> None:
        while True:
            with self._ready:
                while not self._pending:
                    self._ready.wait()
                job, fn = self._pending.popleft()
                if job.status == "cancelled" or job.cancel:
                    if job.status != "cancelled":
                        job.status, job.finished_at = "cancelled", time.time()
                    continue
                job.status, job.started_at = "running", time.time()
                self.current = job
            log.info("job #%d start: %s %s", job.id, job.kind, job.title)
            try:
                result = fn(job)
                job.status = "done"
                job.message = str(result) if result is not None else job.progress
                log.info("job #%d done: %s %s - %s", job.id, job.kind, job.title, job.message)
            except Exception as e:
                job.message = _failure_text(job, e)
                if job.cancel:             # it stopped because it was asked to
                    job.status = "cancelled"
                    log.warning("job #%d cancelled: %s %s - %s", job.id, job.kind, job.title, job.message)
                else:
                    job.status = "failed"
                    log.error("job #%d failed: %s %s - %s\n%s", job.id, job.kind, job.title, job.message,
                              traceback.format_exc())
            finally:
                with self._lock:
                    job.finished_at = time.time()
                    job.active_series_id = None
                    job.active_series_ids, job.lanes = frozenset(), []
                    self.current = None
                    self._trim()
                metrics.record_job(job.kind, job.status)
                if job.kind == "refresh-all" and job.status == "done":
                    metrics.record_refresh_done(job.finished_at)


def _failure_text(job: Job, e: Exception) -> str:
    """The message of a job that ended with an exception: its type and text,
    or for a cancel (limits.Cancelled has no text) plain words with the step
    it was at."""
    if isinstance(e, limits.Cancelled):
        return f"cancelled; last step: {job.progress}"[:300] if job.progress else "cancelled"
    return f"{type(e).__name__}: {e}"


class Scheduler:
    """Submits a refresh-all job on an interval. Runs in its own thread so a
    slow job never delays the next tick being queued."""

    def __init__(self, runner: Runner, fn, interval_hours: float = config.REFRESH_HOURS,
                 first_after_s: float = config.FIRST_REFRESH_MIN * 60):
        self.runner, self.fn = runner, fn
        self.interval = limits.clamp("refresh_hours", interval_hours) * 3600
        self.first_after = first_after_s
        self.next_at = time.time() + first_after_s
        self.last_at: float | None = None      # when the last pass was queued (the next is due interval later)
        self._thread = threading.Thread(target=self._loop, name="mangarr-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("scheduler started: refresh all every %.1fh, first in %.0fs",
                 self.interval / 3600, self.first_after)

    def trigger(self) -> Job:
        """Queue a refresh-all now, unless one is already queued or running."""
        self.last_at = time.time()
        self.next_at = self.last_at + self.interval
        j = self.runner.active("refresh-all")
        if j:
            log.info("refresh-all already %s (#%d); not queueing another", j.status, j.id)
            return j
        return self.runner.submit("refresh-all", "all monitored series", self.fn, key="refresh-all")

    def tick(self) -> None:
        """One scheduler step: pick up a changed interval, queue a pass when due."""
        new = limits.setting("refresh_hours") * 3600      # the Settings page can change it; always finite
        if new != self.interval:
            log.info("refresh interval now %.2fh", new / 3600)
            self.interval = new
            if self.last_at is not None:                   # recomputed from the last run, never by deltas
                self.next_at = self.last_at + new
        if time.time() >= self.next_at:
            try:
                self.trigger()
            except QueueFull as e:
                log.warning("scheduled refresh-all not queued: %s", e)
                self.next_at = time.time() + 300           # try again in a few minutes

    def _loop(self) -> None:
        while True:
            time.sleep(5)
            try:
                self.tick()
            except Exception as e:
                log.warning("scheduler step failed: %s: %s", type(e).__name__, e)
