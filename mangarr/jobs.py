"""In-process job runner and scheduler.

Adding, refreshing and downloading take minutes to hours, so the web layer
never runs them inline: it submits a Job and the single worker thread runs
jobs one at a time (Suwayomi has one download queue, so parallel downloads
would fight). The scheduler thread submits a "refresh all" job every
REFRESH_HOURS. Every job's progress and outcome is visible on the Activity
page and in the log.
"""
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import config

log = logging.getLogger(__name__)


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

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "seriesId": self.series_id,
                "status": self.status, "queuedAt": self.queued_at, "startedAt": self.started_at,
                "finishedAt": self.finished_at, "progress": self.progress, "message": self.message}


class Runner:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._next = 1
        self.current: Job | None = None
        self._thread = threading.Thread(target=self._loop, name="mangarr-jobs", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("job runner started")

    def submit(self, kind: str, title: str, fn, series_id: int | None = None) -> Job:
        with self._lock:
            job = Job(self._next, kind, title, series_id)
            self._next += 1
            self._jobs.append(job)
            del self._jobs[:-300]
        self._q.put((job, fn))
        log.info("job #%d queued: %s %s", job.id, kind, title)
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def cancel(self, job_id: int) -> bool:
        j = self.get(job_id)
        if not j:
            return False
        if j.status == "queued":
            j.status = "cancelled"
            j.finished_at = time.time()
            return True
        if j.status == "running":
            j.cancel = True                # the job checks this between steps
            return True
        return False

    def pending_for(self, series_id: int) -> bool:
        with self._lock:
            return any(j.series_id == series_id and j.status in ("queued", "running") for j in self._jobs)

    def _loop(self) -> None:
        while True:
            job, fn = self._q.get()
            if job.status == "cancelled":
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
                job.status = "failed"
                job.message = f"{type(e).__name__}: {e}"
                log.error("job #%d failed: %s %s - %s\n%s", job.id, job.kind, job.title, job.message,
                          traceback.format_exc())
            finally:
                job.finished_at = time.time()
                self.current = None


class Scheduler:
    """Submits a refresh-all job on an interval. Runs in its own thread so a
    slow job never delays the next tick being queued."""

    def __init__(self, runner: Runner, fn, interval_hours: float = config.REFRESH_HOURS,
                 first_after_s: float = config.FIRST_REFRESH_MIN * 60):
        self.runner, self.fn = runner, fn
        self.interval = interval_hours * 3600
        self.first_after = first_after_s
        self.next_at = time.time() + first_after_s
        self._thread = threading.Thread(target=self._loop, name="mangarr-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("scheduler started: refresh all every %.1fh, first in %.0fs", self.interval / 3600, self.first_after)

    def trigger(self) -> Job:
        self.next_at = time.time() + self.interval
        return self.runner.submit("refresh-all", "all monitored series", self.fn)

    def _loop(self) -> None:
        while True:
            time.sleep(5)
            if time.time() >= self.next_at:
                if any(j.kind == "refresh-all" and j.status in ("queued", "running") for j in self.runner.jobs()):
                    log.info("scheduled refresh skipped: one is already queued or running")
                    self.next_at = time.time() + self.interval
                    continue
                self.trigger()
