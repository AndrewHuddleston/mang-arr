"""Suwayomi or AniList stopping in the middle of a job (review round 2,
findings 68 and 78): chapter ids a failed chunk could not take back out of
Suwayomi's queue are taken out later, a freeze while sources are searched
ends the pass within minutes, a cancel is noticed within about a second, and
a job that stops moving shows up in health. The real Client.gq runs against
a faked urlopen and clock: no network, no real sleeps beyond a few short
waits for helper threads."""
import contextlib
import fcntl
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from unittest import mock

from mangarr import anilist, core, db, downloader, health, jobs, limits, lists, resolver, settings, suwayomi
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, CircuitOpen, Client, Source, SuwayomiUnreachable

try:
    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    web = None

API = "http://suwayomi.test"


class Clock:
    """Fake monotonic time; the patched time.sleep advances it."""

    def __init__(self):
        self.t = 1000.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self.t

    def advance(self, secs: float) -> None:
        with self._lock:
            self.t += secs


class FakeSuwayomi:
    """Suwayomi (and AniList) behind urlopen. Up, it keeps a download queue
    (startDownloader downloads everything queued at once). An outage is
    'refuse' (connection refused) or 'hang' (accepts, never answers: every
    request costs its full timeout on the clock). after[op] = (mode, secs)
    starts an outage for secs once op has been answered. stall[op] makes a
    request really wait (real time) until that event is set, then go on as
    usual: answered (a change lands late) or not, per the outage. Every
    request is logged as (op, variables, what happened), and its timeout in
    `timeouts`. `hits` is what every search finds, `entries` the chapter
    list per manga id."""

    OPS = ("enqueueChapterDownloads", "dequeueChapterDownloads", "startDownloader", "downloadStatus",
           "aboutServer", "fetchSourceManga", "fetchMangaAndChapters", "fetchChapterPages", "sources", "updateManga",
           "manga(")

    def __init__(self, clock: Clock, sources: int = 1):
        self.clock, self.sources = clock, sources
        self.queue: list[int] = []
        self.downloaded: set[int] = set()
        self.log: list[tuple] = []
        self.timeouts: list[tuple[str, float]] = []
        self.outage: tuple[str, float] | None = None
        self.after: dict[str, tuple[str, float]] = {}
        self.block: threading.Event | None = None     # set: a hung request really blocks until it is set
        self.stall: dict[str, threading.Event] = {}
        self.hits: list[dict] = []
        self.entries: dict[int, list[dict]] = {}

    def requests(self, op: str) -> list[tuple]:
        return [(v, how) for o, v, how in self.log if o == op]

    def __call__(self, req, timeout):
        if req.full_url.startswith("https://graphql.anilist.co"):
            return io.BytesIO(b'{"data": {"Media": null}}')
        body = json.loads(req.data)
        op = next(o for o in self.OPS if o in body["query"])
        variables = body.get("variables") or {}
        self.timeouts.append((op, timeout))
        if op in self.stall:
            self.stall[op].wait(10)
        mode = self.outage[0] if self.outage and self.clock.now() < self.outage[1] else "up"
        if mode == "refuse":
            self.log.append((op, variables, "refused"))
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if mode == "hang":
            if self.block is not None:
                self.block.wait(10)
            self.clock.advance(timeout)
            self.log.append((op, variables, "timed out"))
            raise TimeoutError("timed out")
        self.log.append((op, variables, "answered"))
        data = self._answer(op, variables)
        if op in self.after:
            m, secs = self.after.pop(op)
            self.outage = (m, self.clock.now() + secs)
        return io.BytesIO(json.dumps({"data": data}).encode())

    def _answer(self, op, v):
        ok = {"clientMutationId": None}
        if op == "enqueueChapterDownloads":
            self.queue += [i for i in v["ids"] if i not in self.queue]
            return {op: ok}
        if op == "dequeueChapterDownloads":
            self.queue = [i for i in self.queue if i not in v["ids"]]
            return {op: ok}
        if op == "startDownloader":
            self.downloaded.update(self.queue)
            self.queue = []
            return {op: ok}
        if op == "downloadStatus":
            return {op: {"queue": [{"chapter": {"id": i}, "state": "QUEUED", "tries": 0, "progress": 0.0}
                                   for i in self.queue]}}
        if op == "aboutServer":
            return {op: {"version": "v2.0"}}
        if op == "sources":
            return {op: {"nodes": [{"id": str(i), "displayName": f"Source {i}", "lang": "en"}
                                   for i in range(1, self.sources + 1)]}}
        if op == "fetchSourceManga":
            return {op: {"mangas": self.hits}}
        if op == "fetchMangaAndChapters":
            hit = next(h for h in self.hits if h["id"] == v["id"])
            return {op: {"manga": {**hit, "artist": None, "inLibrary": False}, "chapters": self.entries[v["id"]]}}
        if op == "updateManga":
            return {op: {"manga": {"id": v["id"]}}}
        if op == "manga(":
            return {"manga": {"chapters": {"nodes": [{"id": i, "isDownloaded": True} for i in self.downloaded]}}}
        raise AssertionError(f"unexpected {op}")


class Base(unittest.TestCase):
    """A temporary data dir, the faked Suwayomi and clock, a closed breaker,
    and no real network or notifications."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.clock = Clock()
        self.fake = FakeSuwayomi(self.clock)

        def blocked(*a, **k):
            raise OSError("network disabled in tests")
        for p in (mock.patch("mangarr.config.DB_PATH", self.tmp + "/t.db"),
                  mock.patch("mangarr.config.LOCK_PATH", self.tmp + "/lock"),
                  mock.patch("mangarr.config.STAGING_ROOT", self.tmp + "/staging"),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp + "/library"),
                  mock.patch("urllib.request.urlopen", self.fake),
                  mock.patch("time.monotonic", self.clock.now),
                  mock.patch("time.sleep", self.clock.advance),
                  mock.patch("socket.create_connection", blocked),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        suwayomi._down.clear()
        self.addCleanup(suwayomi._down.clear)
        p = mock.patch.object(resolver, "SEARCHES", limits.Spacer(pause=lambda s, c=None: False))   # searches not spaced
        p.start()
        self.addCleanup(p.stop)
        downloader._unsaved = downloader._SAVED
        self.addCleanup(setattr, downloader, "_unsaved", downloader._SAVED)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        self.client = Client(API)


def one_chapter_plan(chapter_id=7001, number=1.0):
    m = SourceMatch(Source("7", "A", "en"), 7, "T", None, 0, "T", 1,
                    [Chapter(chapter_id, number, None, None, False)])
    return Plan(Series(english="T"), [m], [], [], {number: m}, candidates={number: [m]})


# -- finding 68: our ids left in Suwayomi's queue ---------------------------------------------

class LeftoverTest(Base):
    def setUp(self):
        super().setUp()
        self.retries = []
        p = mock.patch.object(downloader, "_retry_leftovers_later", self.retries.append)
        p.start()
        self.addCleanup(p.stop)

    def _fail_after_enqueue(self, mode, secs):
        self.fake.after["enqueueChapterDownloads"] = (mode, secs)
        with self.assertRaises(SuwayomiUnreachable), self.assertLogs("mangarr.downloader", "WARNING") as cm:
            downloader.download(self.client, one_chapter_plan(), only={1.0}, in_order=True)
        self.assertIn("will try again once it answers", "\n".join(cm.output))
        self.assertEqual(self.fake.queue, [7001])                  # the enqueue landed
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [])   # breaker open: nothing sent
        self.assertEqual(downloader.leftovers(), [7001])           # but remembered
        self.assertEqual(len(self.retries), 1)                     # and a background retry started

    def test_refused_until_15s_after_enqueue(self):                 # the round-2 evidence, first variant
        self._fail_after_enqueue("refuse", 15)
        self.assertEqual([how for _, how in self.fake.requests("startDownloader")], ["refused"] * 3)
        self.clock.advance(100)                                     # back up, breaker window over
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [({"ids": [7001]}, "answered")])
        self.assertEqual(downloader.leftovers(), [])

    def test_hung_until_200s(self):                                  # the round-2 evidence, hung variant
        self._fail_after_enqueue("hang", 200)
        self.assertFalse(downloader.clear_leftovers(self.client))  # still inside the breaker window
        self.assertEqual(downloader.leftovers(), [7001])
        self.clock.advance(300)
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(downloader.leftovers(), [])

    def test_next_download_takes_them_out_first(self):
        self._fail_after_enqueue("refuse", 15)
        self.clock.advance(100)
        res = downloader.download(self.client, one_chapter_plan(7002, 2.0), only={2.0}, in_order=True)
        self.assertEqual(res, {2.0: "ok"})
        ops = [(o, v) for o, v, how in self.fake.log if o in ("enqueueChapterDownloads", "dequeueChapterDownloads")]
        self.assertEqual(ops[1:], [("dequeueChapterDownloads", {"ids": [7001]}),
                                   ("enqueueChapterDownloads", {"ids": [7002]})])
        self.assertEqual(downloader.leftovers(), [])

    def test_only_our_ids_are_dequeued(self):
        downloader._remember_leftovers([7001, 8001])
        self.fake.queue = [9999, 7001]              # 9999: queued by the user in Suwayomi; 8001: gone meanwhile
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [({"ids": [7001]}, "answered")])
        self.assertEqual(self.fake.queue, [9999])
        self.assertEqual(downloader.leftovers(), [])

    def test_ids_no_longer_queued_are_just_forgotten(self):
        downloader._remember_leftovers([7001])
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [])
        self.assertEqual(downloader.leftovers(), [])

    def test_list_is_capped_and_internal(self):
        with mock.patch.object(downloader, "MAX_LEFTOVERS", 3):
            downloader._remember_leftovers([1, 2])
            downloader._remember_leftovers([3, 4])
        self.assertEqual(downloader.leftovers(), [2, 3, 4])          # the newest kept
        with db.connect() as con, self.assertRaises(KeyError):
            settings.set_many(con, {"leftover_queue_ids": []})      # never from a form or the API

    def test_enqueue_refused_by_open_breaker_leaves_nothing(self):
        class Refusing:
            dequeued = []

            def queue(self): return []
            def dequeue(self, ids): self.dequeued.append(ids)
            def enqueue(self, ids): raise CircuitOpen("Suwayomi at x unreachable (not retrying for 50 s)")
        c = Refusing()
        with self.assertRaises(CircuitOpen):
            downloader.download(c, one_chapter_plan(), only={1.0}, in_order=True)
        self.assertEqual(c.dequeued, [])
        self.assertEqual(downloader.leftovers(), [])


class LeftoverBackgroundTest(Base):
    def test_background_retry_once_suwayomi_answers(self):
        self.fake.after["enqueueChapterDownloads"] = ("refuse", 15)
        with mock.patch.multiple(downloader, LEFTOVER_RETRY_SECS=0.01, LEFTOVER_RETRY_MAX=0.02,
                                 LEFTOVER_RETRY_TRIES=500), self.assertLogs("mangarr.downloader", "INFO") as cm:
            with self.assertRaises(SuwayomiUnreachable):
                downloader.download(self.client, one_chapter_plan(), only={1.0}, in_order=True)
            self.assertEqual(self.fake.queue, [7001])
            self.clock.advance(100)                  # the retry thread keeps failing fast until here
            downloader._retrier.join(10)
        self.assertFalse(downloader._retrier.is_alive())
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(downloader.leftovers(), [])
        self.assertIn("removed chapter id(s) [7001]", "\n".join(cm.output))

    @unittest.skipIf(web is None, "web extras not installed")
    def test_a_pass_starts_by_taking_them_out(self):   # e.g. after a restart lost the retry thread
        downloader._remember_leftovers([7001])
        self.fake.queue = [9999, 7001]
        with mock.patch.object(web, "client", self.client), self.assertLogs("mangarr.downloader", "INFO"):
            web._run_pass(jobs.Job(1, "refresh-all", "all"), [], "sim")
            downloader._retrier.join(10)
        self.assertEqual(self.fake.queue, [9999])
        self.assertEqual(downloader.leftovers(), [])

    def test_nothing_left_over_starts_no_thread(self):
        with mock.patch.object(downloader, "_retry_leftovers_later") as later:
            downloader.retry_leftovers_now(self.client)
        later.assert_not_called()

    def test_background_retry_waits_for_a_running_download(self):
        with downloader.download_lock():
            with downloader._lock_if_free() as free:
                self.assertFalse(free)
        with downloader._lock_if_free() as free:
            self.assertTrue(free)

    def test_a_restore_is_let_in_while_the_retry_waits_on_a_hung_suwayomi(self):     # round 3
        from mangarr import backup
        downloader._remember_leftovers([7001])
        self.fake.queue = [7001]
        self.fake.outage = ("hang", 10**6)
        self.fake.block = threading.Event()                    # a request really blocks (real time)
        self.addCleanup(self.fake.block.set)
        with mock.patch.object(downloader, "LEFTOVER_RETRY_TRIES", 1), \
             self.assertLogs("mangarr.downloader", "WARNING"):          # "stopped retrying for now"
            retry = threading.Thread(target=downloader._retry_leftovers, args=(self.client, 0), daemon=True)
            retry.start()
            self.addCleanup(retry.join, 15)
            self.addCleanup(self.fake.block.set)               # before that join: never wait out the hang
            deadline = time.perf_counter() + 5
            while not self.fake.timeouts and time.perf_counter() < deadline:
                threading.Event().wait(0.01)                   # the retry holds the lock, its queue read hangs
            self.assertTrue(downloader._retry_holds.is_set())
            t0 = time.perf_counter()
            with backup._no_download_run():                     # not refused: the retry lets go
                took = time.perf_counter() - t0
                with downloader._lock_if_free() as free:
                    self.assertFalse(free)
            retry.join(5)
        self.assertLess(took, 3.0)
        self.assertFalse(retry.is_alive())
        self.assertEqual(downloader.leftovers(), [7001])            # still remembered for the next try

    def test_a_restore_says_what_holds_the_lock(self):
        from mangarr import backup
        with mock.patch.object(downloader, "RETRY_LET_GO_SECS", 0.3):
            with downloader._lock_if_free() as free, self.assertRaises(backup.RestoreError) as cm:
                self.assertTrue(free)                           # the retry, here stuck in this thread
                with backup._no_download_run():
                    pass
            self.assertIn("still taking entries an earlier download left in Suwayomi's queue back out",
                          str(cm.exception))
            with downloader.download_lock(), self.assertRaises(backup.RestoreError) as cm:
                with backup._no_download_run():
                    pass
            self.assertIn("a download run is in progress", str(cm.exception))
        with backup._no_download_run():
            pass

    def test_the_retry_keeps_off_the_lock_while_a_restore_wants_it(self):
        downloader._remember_leftovers([7001])
        self.fake.queue = [7001]
        with downloader.leftover_retry_held_off(), mock.patch.object(downloader, "LEFTOVER_RETRY_TRIES", 1), \
             self.assertLogs("mangarr.downloader", "WARNING"):
            downloader._retry_leftovers(self.client, first=0)
        self.assertEqual(self.fake.log, [])
        self.assertEqual(downloader.leftovers(), [7001])


class LeftoverUnsavedTest(Base):
    """The database will not take the list (busy past its timeout, round-2
    re-check): the ids are kept in memory, where leftovers() and the next
    download run see them, and the background retry saves them and takes
    them out once it can."""

    def setUp(self):
        super().setUp()
        self.retries = []
        p = mock.patch.object(downloader, "_retry_leftovers_later",
                              lambda client, first=None: self.retries.append(client))
        p.start()
        self.addCleanup(p.stop)
        with db.connect():
            pass                                                   # the database exists (WAL)
        real = sqlite3.connect                                     # busy for 0.2 s, not 30 s
        p = mock.patch.object(db.sqlite3, "connect", lambda path, timeout=5.0, **kw: real(path, timeout=0.2, **kw))
        p.start()
        self.addCleanup(p.stop)

    @contextlib.contextmanager
    def locked(self):
        """Another connection holds the write lock: reads work, writes fail."""
        c = sqlite3.connect(self.tmp + "/t.db")
        c.execute("BEGIN EXCLUSIVE")
        try:
            yield
        finally:
            c.rollback()
            c.close()

    def stored(self) -> list:
        c = sqlite3.connect(self.tmp + "/t.db")
        try:
            row = c.execute("SELECT value FROM setting WHERE key='leftover_queue_ids'").fetchone()
            return json.loads(row[0]) if row else []
        finally:
            c.close()

    def test_remembered_while_the_database_is_locked(self):       # the round-2 evidence
        self.fake.after["enqueueChapterDownloads"] = ("refuse", 15)
        with self.locked():
            with self.assertRaises(SuwayomiUnreachable), self.assertLogs("mangarr.downloader", "WARNING") as cm:
                downloader.download(self.client, one_chapter_plan(), only={1.0}, in_order=True)
            self.assertIn("could not save the chapter ids left in Suwayomi's queue (OperationalError: database is "
                          "locked)", "\n".join(cm.output))
            self.assertEqual(self.stored(), [])
            self.assertEqual(downloader.leftovers(), [7001])       # kept in memory, not dropped
            self.assertEqual(self.fake.queue, [7001])
            self.assertEqual(self.retries, [self.client])
        self.clock.advance(100)                                    # Suwayomi back, the database free again
        with self.assertLogs("mangarr.downloader", "INFO") as cm:
            downloader._retry_leftovers(self.client, first=0)      # what the background retry does
        self.assertEqual(self.fake.queue, [])
        self.assertEqual((downloader.leftovers(), self.stored()), ([], []))
        self.assertEqual(downloader._unsaved, downloader._SAVED)
        self.assertIn("removed chapter id(s) [7001]", "\n".join(cm.output))

    def test_the_next_download_run_sees_them_too(self):
        self.fake.queue = [7001]
        with self.locked(), self.assertLogs("mangarr.downloader", "WARNING"):
            downloader._remember_leftovers([7001])
        res = downloader.download(self.client, one_chapter_plan(7002, 2.0), only={2.0}, in_order=True)
        self.assertEqual(res, {2.0: "ok"})
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [({"ids": [7001]}, "answered")])
        self.assertEqual((downloader.leftovers(), self.stored()), ([], []))

    def test_a_forget_that_cannot_be_saved_is_kept_too(self):
        downloader._remember_leftovers([7001])
        self.assertEqual(self.stored(), [7001])
        with self.locked(), self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertTrue(downloader.clear_leftovers(self.client))     # 7001 is no longer queued
        self.assertEqual(self.stored(), [7001])
        self.assertEqual(downloader.leftovers(), [])               # not taken for ours again meanwhile
        self.assertEqual(self.retries, [self.client])
        downloader._retry_leftovers(self.client, first=0)
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [])


# -- finding 78: a freeze while sources are searched ------------------------------------------

@unittest.skipIf(web is None, "web extras not installed")
class FrozenPassTest(Base):
    def seed(self, n, ref=lambda i: {"anilist_id": i}):
        with db.connect() as con:
            for i in range(1, n + 1):
                db.upsert_series(con, Series(english=f"S{i}", **ref(i)))
            return [dict(r) for r in db.series_rows(con)]

    def one_search_at_a_time(self):
        # the fake clock adds up every hung request, also those that wait side by side: timing
        # checks search one site at a time (searches at once: test_freeze_while_several_sites_are_searched)
        with db.connect() as con:
            settings.set_many(con, {"search_parallel": 1})
        settings._cache.clear()

    def test_freeze_after_the_first_sources_call_ends_the_pass_in_minutes(self):   # the round-2 evidence
        self.one_search_at_a_time()
        self.fake.sources = 30
        self.fake.after["sources"] = ("hang", 10**6)
        rows = self.seed(3)
        job = jobs.Job(1, "refresh-all", "all")
        start = self.clock.now()
        with mock.patch.object(web, "client", self.client), self.assertLogs(level="ERROR"):
            with self.assertRaises(web.PassStopped):
                web._run_pass(job, rows, "sim")
        took = self.clock.now() - start
        self.assertLess(took, 300, f"pass took {took:.0f} simulated s")
        self.assertEqual(len(self.fake.requests("fetchSourceManga")), 1)       # not 30 timeouts per series
        self.assertIn(self.client.api, suwayomi._down)                           # the breaker is open
        self.assertEqual([i["state"] for i in job.items], ["error", "error", "cancelled"])
        self.assertIn("stopped answering", job.items[0]["result"])

    def test_freeze_while_several_sites_are_searched(self):
        # five searches hang side by side: one error for the series, not one per search, and the
        # rest of the sources are never asked
        self.fake.sources = 30
        self.fake.after["sources"] = ("hang", 10**6)
        rows = self.seed(3)
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(web, "client", self.client), self.assertLogs(level="ERROR"):
            with self.assertRaises(web.PassStopped):
                web._run_pass(job, rows, "sim")
        self.assertLessEqual(len(self.fake.requests("fetchSourceManga")), 5)     # the searches in flight only
        self.assertIn(self.client.api, suwayomi._down)
        self.assertEqual([i["state"] for i in job.items], ["error", "error", "cancelled"])
        self.assertIn("stopped answering", job.items[0]["result"])
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("mangarr-search-")], [])

    def test_slow_sites_are_not_an_outage(self):
        self.fake.sources = 3

        def slow_sites(req, timeout):                 # every site times out; Suwayomi itself answers
            if b"fetchSourceManga" in (req.data or b""):
                self.clock.advance(timeout)
                raise TimeoutError("timed out")
            return self.fake(req, timeout)
        with mock.patch("urllib.request.urlopen", slow_sites):
            plan = resolver.resolve(self.client, Series(english="T"), should_cancel=lambda: False)
        self.assertEqual(len(plan.unreachable), 3)
        self.assertEqual(suwayomi._down, {})

    def test_cancel_is_noticed_within_a_second_while_suwayomi_hangs(self):
        rows = self.seed(2, ref=lambda i: {})                   # manual series: straight to the sources
        self.fake.outage = ("hang", 10**6)
        self.fake.block = threading.Event()                     # a request really blocks (real time)
        self.addCleanup(self.fake.block.set)
        job = jobs.Job(1, "refresh-all", "all")
        timer = threading.Timer(0.2, lambda: setattr(job, "cancel", True))
        timer.start()
        t0 = time.perf_counter()
        with mock.patch.object(web, "client", self.client), mock.patch.object(web, "_record_error") as record:
            web._run_pass(job, rows, "sim")
        took = time.perf_counter() - t0
        self.assertLess(took, 3.0)                              # the hung call would have taken 30 s x 2
        self.assertEqual([i["state"] for i in job.items], ["cancelled", "cancelled"])
        record.assert_not_called()                              # a cancel is not an error of the series


class ResolveCancelTest(Base):
    def test_cancel_between_sources(self):
        searched, said = [], []

        class Sources:
            def search(self, src, q):
                searched.append(src.name)
                return []
        sources = [Source(str(i), f"S{i}", "en") for i in range(1, 4)]
        with db.connect() as con:
            settings.set_many(con, {"search_parallel": 1})      # at once: test_resolver_limits.ParallelSearchTest
        settings._cache.clear()
        with self.assertRaises(limits.Cancelled):
            resolver.resolve(Sources(), Series(english="T"), sources=sources,
                             should_cancel=lambda: bool(searched), progress=said.append)
        self.assertEqual(searched, ["S1"])
        self.assertEqual(said, ["searching S1 (1 of 3 sources)"])

    def test_cancel_between_search_titles(self):
        series = Series(english="T", romaji="Tee", synonyms=["Te"])
        with self.assertRaises(limits.Cancelled):
            resolver.resolve(self.client, series, sources=[Source("1", "S1", "en")],
                             should_cancel=lambda: bool(self.fake.requests("fetchSourceManga")))
        self.assertEqual(len(self.fake.requests("fetchSourceManga")), 1)    # the 2nd and 3rd titles not sent

    def test_hung_metadata_lookup_is_cut_short(self):
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=5, english="T"))
            release = threading.Event()
            self.addCleanup(release.set)
            flag = []
            threading.Timer(0.2, lambda: flag.append(1)).start()
            t0 = time.perf_counter()
            with mock.patch.object(core.metadata, "by_ref", lambda ref: release.wait(10)), \
                 self.assertRaises(limits.Cancelled):
                core.refresh_series(con, self.client, sid, should_cancel=lambda: bool(flag))
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_interruptible(self):
        self.assertEqual(limits.interruptible(lambda: 5, lambda: False), 5)
        with self.assertRaises(KeyError):
            limits.interruptible(lambda: {}["x"], lambda: False)
        with self.assertRaises(limits.Cancelled):
            limits.interruptible(lambda: 5, lambda: True)
        self.assertEqual(limits.interruptible(lambda: 6, None), 6)


# -- round-2 re-check: cancel during a download on a hung Suwayomi -------------------------------

class CancelDuringDownloadTest(Base):
    """Cancel while Suwayomi hangs in the middle of a download (round-2
    re-check: 190 simulated s without a cancel check inside start(), about
    125 s inside a chapter job's chapter list). Requests really block here,
    so the job must give up on them, not wait them out; the chunk's ids are
    taken out once Suwayomi answers again."""

    def setUp(self):
        super().setUp()
        self.retries = []
        p = mock.patch.object(downloader, "_retry_leftovers_later",
                              lambda client, first=None: self.retries.append(client))
        p.start()
        self.addCleanup(p.stop)
        self.flag: list = []
        with db.connect():
            pass                                # created and migrated now, not inside the timed part

    def cancelled(self) -> bool:
        return bool(self.flag)

    def stall(self, op) -> threading.Event:
        """Requests for op really block until the event is set (at the latest when the test ends)."""
        ev = threading.Event()
        self.addCleanup(ev.set)
        self.fake.stall[op] = ev
        return ev

    def cancel_when_sent(self, op, cancel=None):
        """Cancel once a request for op has gone out."""
        def watch():
            deadline = time.perf_counter() + 5
            while time.perf_counter() < deadline and op not in [o for o, _ in self.fake.timeouts]:
                time.sleep(0.01)
            (cancel or (lambda: self.flag.append(1)))()
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        self.addCleanup(t.join, 6)

    def test_hung_start(self):
        self.fake.after["enqueueChapterDownloads"] = ("hang", 10**6)
        self.stall("startDownloader")
        self.cancel_when_sent("startDownloader")
        t0 = time.perf_counter()
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            res = downloader.download(self.client, one_chapter_plan(), only={1.0}, should_cancel=self.cancelled,
                                      in_order=True)
        self.assertLess(time.perf_counter() - t0, 3.0)             # start() alone: 3 x 10 s real here
        self.assertEqual(res, {})
        # one short try after the cancel, not 3 x 30 s
        self.assertEqual([t for o, t in self.fake.timeouts if o == "dequeueChapterDownloads"],
                         [downloader.CANCEL_DEQUEUE_SECS])
        self.assertIn("will try again once it answers", "\n".join(cm.output))
        self.assertEqual(self.fake.queue, [7001])                   # it got no answer ...
        self.assertEqual(downloader.leftovers(), [7001])            # ... so the id is remembered
        self.assertEqual(self.retries, [self.client])
        self.fake.outage = None                                     # Suwayomi answers again
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual((self.fake.queue, downloader.leftovers()), ([], []))

    def test_hung_enqueue_that_lands_after_the_cancel(self):
        landing = self.stall("enqueueChapterDownloads")            # Suwayomi applies it, but late
        self.cancel_when_sent("enqueueChapterDownloads")
        t0 = time.perf_counter()
        with self.assertLogs("mangarr.downloader", "DEBUG") as cm:
            downloader.download(self.client, one_chapter_plan(), only={1.0}, should_cancel=self.cancelled,
                                in_order=True)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("was cut short by the cancel and may still land", "\n".join(cm.output))
        self.assertEqual(self.fake.requests("dequeueChapterDownloads"), [({"ids": [7001]}, "answered")])
        self.assertEqual(self.fake.queue, [])                       # nothing queued yet ...
        self.assertEqual(downloader.leftovers(), [7001])            # ... but it may still land
        landing.set()
        deadline = time.perf_counter() + 5
        while not self.fake.queue and time.perf_counter() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.fake.queue, [7001])                   # it did, after the dequeue
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual((self.fake.queue, downloader.leftovers()), ([], []))

    def test_chapter_job_waits_for_no_chapter_list(self):
        with db.connect() as con:
            sid = db.upsert_series(con, Series(english="T"))
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level,"
                        " author_level, chapter_count, max_chapter, is_primary, seen_at)"
                        " VALUES (?, 7, 'A', 'T', 0, 1, 1, 1, 1, ?)", (sid, db.now()))
        self.fake.outage = ("hang", 10**6)
        self.stall("manga(")
        self.cancel_when_sent("manga(")
        t0 = time.perf_counter()
        with db.connect() as con, self.assertRaises(limits.Cancelled):
            core.download_chapter(con, self.client, sid, 1.0, should_cancel=self.cancelled)
        self.assertLess(time.perf_counter() - t0, 3.0)             # the chapter list alone: 2 x 10 s real here

    def test_import_links_the_rest_when_the_name_lookup_is_cancelled(self):
        folder = os.path.join(self.tmp, "staging", "A", "T")
        os.makedirs(folder)
        for name in ("Chapter 1.cbz", "Official_S2 - Episode 5.cbz"):   # numbered, and one only Suwayomi can place
            with zipfile.ZipFile(os.path.join(folder, name), "w") as z:
                z.writestr("001.jpg", os.urandom(2000))

        class Cancelling:
            def chapters(self, manga_id):
                raise limits.Cancelled()
        with db.connect() as con, mock.patch.object(core.komga, "scan", lambda: False):
            sid = db.upsert_series(con, Series(english="T"))
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level,"
                        " author_level, chapter_count, max_chapter, is_primary, seen_at)"
                        " VALUES (?, 7, 'A', 'T', 0, 1, 1, 1, 1, ?)", (sid, db.now()))
            with self.assertLogs("mangarr.core", "INFO") as cm:
                self.assertEqual(core.import_series(con, sid, Cancelling()), 1)
        self.assertIn("cancelled before 1 unnumbered file(s)", "\n".join(cm.output))

    @unittest.skipIf(web is None, "web extras not installed")
    def test_pass_on_a_series_with_a_wanted_chapter(self):             # the round-2 evidence, end to end
        self.fake.hits = [{"id": 7, "title": "S1", "author": None, "status": "ONGOING"}]
        self.fake.entries = {7: [{"id": 7001, "name": "Chapter 1", "chapterNumber": 1, "scanlator": None,
                                  "isDownloaded": False, "uploadDate": 0}]}
        self.fake.after["enqueueChapterDownloads"] = ("hang", 10**6)
        self.stall("startDownloader")
        with db.connect() as con:
            db.upsert_series(con, Series(english="S1"))
            rows = [dict(r) for r in db.series_rows(con)]
        job = jobs.Job(1, "refresh-all", "all")
        self.cancel_when_sent("startDownloader", lambda: setattr(job, "cancel", True))
        t0 = time.perf_counter()
        with mock.patch.object(web, "client", self.client), self.assertLogs(level="WARNING"):
            web._run_pass(job, rows, "sim")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(self.fake.requests("enqueueChapterDownloads"), [({"ids": [7001]}, "answered")])
        self.assertEqual(downloader.leftovers(), [7001])
        self.fake.outage = None
        self.assertTrue(downloader.clear_leftovers(self.client))
        self.assertEqual(self.fake.queue, [])


# -- AniList breaker ----------------------------------------------------------------------------

class AniListBreakerTest(Base):
    def setUp(self):
        super().setUp()
        anilist._failures, anilist._skip_until = 0, 0.0
        self.addCleanup(setattr, anilist, "_failures", 0)
        self.addCleanup(setattr, anilist, "_skip_until", 0.0)

    def test_skips_anilist_for_a_while_after_failures(self):
        calls = []

        def down(query, variables, retries=3):
            calls.append(variables)
            raise RuntimeError("AniList unreachable: timed out")
        with mock.patch.object(anilist, "_send", down), self.assertLogs("mangarr.anilist", "WARNING"):
            for _ in range(anilist.BREAKER_AFTER):
                with self.assertRaises(RuntimeError):
                    anilist.by_id(1)
            with self.assertRaises(RuntimeError) as cm:
                anilist.by_id(1)
        self.assertEqual(len(calls), anilist.BREAKER_AFTER)          # the last one never asked AniList
        self.assertIn("not asked again", str(cm.exception))
        self.clock.advance(anilist.BREAKER_SECS + 1)
        with mock.patch.object(anilist, "_send", lambda q, v, retries=3: {"data": {"Media": None}}), \
             self.assertLogs("mangarr.anilist", "INFO") as cm:
            self.assertIsNone(anilist.by_id(1))
        self.assertIn("AniList answers again", "\n".join(cm.output))
        self.assertEqual(anilist._failures, 0)

    def test_an_http_error_answer_is_no_outage(self):
        def bad(query, variables, retries=3):
            raise urllib.error.HTTPError("https://graphql.anilist.co", 400, "Bad Request", {}, None)
        with mock.patch.object(anilist, "_send", bad):
            for _ in range(anilist.BREAKER_AFTER + 1):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    anilist.by_id(1)
                cm.exception.close()                         # as _send closes a real one
        self.assertEqual(anilist._failures, 0)

    def test_refresh_falls_back_to_the_stored_record(self):
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=5, english="Stored"))
            with mock.patch.object(anilist, "_send", side_effect=RuntimeError("AniList unreachable: x")), \
                 mock.patch.object(core, "add_series", lambda con, client, series, **kw: series), \
                 self.assertLogs("mangarr.core", "WARNING"):
                for _ in range(anilist.BREAKER_AFTER + 1):
                    self.assertEqual(core.refresh_series(con, self.client, sid).title, "Stored")


# -- health: a job that stops moving -------------------------------------------------------------

class StalledJobTest(unittest.TestCase):
    def setUp(self):
        self.runner = jobs.Runner()
        for p in (mock.patch.object(health, "_runner", self.runner), mock.patch.object(health, "_stall_logged", None)):
            p.start()
            self.addCleanup(p.stop)

    def running(self, idle_min):
        job = jobs.Job(7, "refresh-all", "all monitored series", status="running", started_at=time.time() - 7200)
        job.progress = "2/9: Solo Leveling - searching MangaDex (3 of 30 sources)"
        job.progress_at = time.time() - idle_min * 60
        self.runner.current = job
        return job

    def test_warns_after_30_minutes_without_progress(self):
        self.assertIsNone(health.stalled_job())                # nothing running
        self.running(5)
        self.assertIsNone(health.stalled_job())
        self.running(31)
        with self.assertLogs("mangarr.health", "WARNING"):
            c = health.stalled_job()
        self.assertEqual((c.level, c.name), ("warning", "Jobs"))
        self.assertIn("job #7 (refresh-all all monitored series) has made no progress for 31 min", c.detail)
        self.assertIn("searching MangaDex", c.detail)

    def test_progress_changes_are_timed(self):
        job = jobs.Job(1, "chapter", "T ch 1")
        self.assertIsNone(job.progress_at)
        job.progress = "A: chapter 1 (0 of 1 done)"
        first = job.progress_at
        job.progress_at = first - 100
        job.progress = "A: chapter 1 (0 of 1 done)"               # the same text again is no progress
        self.assertEqual(job.progress_at, first - 100)
        job.progress = "A: chapter 1 (0 of 1 done, this batch 40%)"
        self.assertGreaterEqual(job.progress_at, first)
        self.assertIn("progressAt", job.as_dict())

    def test_a_restore_is_not_said_to_be_cancellable(self):
        self.running(45).kind = "restore"
        with self.assertLogs("mangarr.health", "WARNING"):
            c = health.stalled_job()
        self.assertIn("it cannot be cancelled", c.detail)
        self.assertNotIn("cancel it", c.detail)

    def test_waiting_for_another_download_run_is_no_progress(self):   # round-2 re-check
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)                         # another process's run holds it
            try:
                job = jobs.Job(3, "chapter", "T ch 1", status="running", started_at=time.time())
                said = []

                def report(m):
                    job.progress = m
                    said.append((m, job.progress_at))
                with mock.patch("time.sleep"), self.assertRaises(limits.Cancelled), \
                        self.assertLogs("mangarr.downloader", "INFO"):
                    with downloader.download_lock(path, should_cancel=lambda: len(said) > 5, progress=report):
                        pass
            finally:
                os.close(fd)
        self.assertEqual(len(set(said)), 1, said)                 # the same words, never "progress"
        self.assertIn("waiting for another download run to finish", said[0][0])
        job.progress_at -= 31 * 60
        self.runner.current = job
        with self.assertLogs("mangarr.health", "WARNING"):
            c = health.stalled_job()
        self.assertIn("has made no progress for 31 min; last step: waiting for another download run", c.detail)

    def test_in_the_health_checks(self):
        self.running(45)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("mangarr.config.DB_PATH", tmp + "/t.db"), \
             mock.patch("mangarr.config.STAGING_ROOT", tmp), mock.patch("mangarr.config.LIBRARY_ROOT", tmp), \
             mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "x")), \
             mock.patch.object(health, "_alert", lambda out: []), \
             mock.patch("mangarr.komga.configured", return_value=False), \
             mock.patch.dict(health._cache, {"at": 0.0, "checks": []}), \
             self.assertLogs("mangarr.health", "WARNING"):
            settings._cache.clear()
            client = mock.Mock()
            client.gq.side_effect = suwayomi.SuwayomiError("offline in tests")
            checks = health._compute(client)
            settings._cache.clear()
        self.assertIn("Jobs", [c.name for c in checks if c.level == "warning"])


class LongJobProgressTest(Base):
    """Jobs that work through many slow lookups report each step (round-2
    re-check: a big staging tree was flagged as stuck while it was being
    worked through), and adopting stops on a cancel between them."""

    class NoEntries:
        def mangas_page(self, offset, first):
            return [], False

    def test_scan_reports_each_folder(self):
        for name in ("One", "Two", "Three"):
            os.makedirs(os.path.join(self.tmp, "staging", "Src", name))
        said = []
        with mock.patch.object(core.metadata, "lookup", lambda name: (None, [])):
            items = core.plan_adopt(self.NoEntries(), progress=said.append)
        self.assertEqual(len(items), 3)
        self.assertEqual(said, ["listing what Suwayomi has downloaded (0 entries so far)",
                                "identifying folder 1 of 3: One", "identifying folder 2 of 3: Three",
                                "identifying folder 3 of 3: Two"])
        looked = []
        with mock.patch.object(core.metadata, "lookup", lambda name: looked.append(name) or (None, [])), \
                self.assertRaises(limits.Cancelled):
            core.plan_adopt(self.NoEntries(), should_cancel=lambda: len(looked) >= 2)
        self.assertEqual(looked, ["One", "Three"])

    def test_a_folder_not_looked_up_does_not_stop_the_scan(self):
        """The verifier's repro: with AniList and MangaDex failing only for the 3rd folder (a network or DNS
        blip), the whole scan failed and the folders already looked up were thrown away. Now only that folder
        is not identified, and says why; a scan in which no lookup got an answer still stops with the error."""
        for name in ("One", "Two", "Three"):
            os.makedirs(os.path.join(self.tmp, "staging", "Src", name))

        def lookup(name):
            if name == "Two":
                raise core.metadata.LookupError_("AniList and MangaDex could not be reached")
            return Series(anilist_id=len(name), english=name), []
        with mock.patch.object(core.metadata, "lookup", lookup), self.assertLogs("mangarr.core", "WARNING") as cm:
            items = core.plan_adopt(self.NoEntries())
        self.assertEqual([(i.folder_name, i.series and i.series.title, i.lookup_error) for i in items],
                         [("One", "One", None), ("Three", "Three", None),
                          ("Two", None, "not looked up: AniList and MangaDex could not be reached")])
        self.assertIn("1 of 3 folder(s) could not be looked up", "\n".join(cm.output))
        if web is not None:
            with mock.patch.object(core, "plan_adopt", lambda *a, **kw: items), \
                    mock.patch.dict(web._adopt_scan, {"items": None, "gen": 0}):
                msg = web._job_adopt_scan(jobs.Job(1, "adopt-scan", "staging folders"))
                self.assertEqual(web._adopt_scan["items"], items)
            self.assertEqual(msg, "3 folders, 2 identified, 1 need a choice (1 not looked up: AniList and MangaDex "
                                  "could not be reached; scan again)")

        def down(name):
            raise core.metadata.LookupError_("AniList and MangaDex could not be reached")
        with mock.patch.object(core.metadata, "lookup", down), self.assertRaises(core.metadata.LookupError_):
            core.plan_adopt(self.NoEntries())

    @unittest.skipIf(web is None, "web extras not installed")
    def test_adopt_job_reports_each_series_and_stops_on_cancel(self):
        job = jobs.Job(1, "adopt", "3 folder(s)")
        seen = []

        def link(con, sid, client=None):
            seen.append((sid, job.progress))
            job.cancel = sid == 2
            return 1
        with mock.patch.object(core, "apply_adopt", lambda con, chosen: ([1, 2, 3], 9)), \
                mock.patch.object(core, "import_series", link):
            msg = web._job_adopt([])(job)
        self.assertEqual(seen, [(1, "linking the files of series 1 of 3"), (2, "linking the files of series 2 of 3")])
        self.assertIn("linked 2 files; cancelled before linking the other 1", msg)


    def test_text_list_sync_reports_each_line(self):
        said = []
        known = {"One": Series(anilist_id=1, english="One"), "Two": Series(anilist_id=2, english="Two")}
        with mock.patch.object(lists, "_get_text", lambda url, **kw: "One\nTwo"), \
                mock.patch.object(lists.metadata, "lookup", lambda t, **kw: (known.get(t), [])):
            lists.fetch("url_text", {"url": "http://x"}, progress=said.append)
        self.assertEqual(said, ["looking up line 1 of 2", "looking up line 2 of 2"])


class DownloadProgressTest(Base):
    def test_batch_progress_is_reported(self):
        polls = iter([[{"id": 1, "state": "DOWNLOADING", "tries": 0, "progress": 0.5}],
                      [{"id": 1, "state": "DOWNLOADING", "tries": 0, "progress": 0.5}], []])

        class Q:
            def queue(self): return next(polls)
        shares = []
        self.assertEqual(downloader._wait(Q(), [1, 2], lambda: False, every=2, moved=shares.append), "done")
        self.assertEqual(shares, [0.75])                         # 2 is done, 1 half done; reported once


if __name__ == "__main__":
    unittest.main()
