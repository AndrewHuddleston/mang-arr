"""Suwayomi or AniList stopping in the middle of a job (review round 2,
findings 68 and 78): chapter ids a failed chunk could not take back out of
Suwayomi's queue are taken out later, a freeze while sources are searched
ends the pass within minutes, a cancel is noticed within about a second, and
a job that stops moving shows up in health. The real Client.gq runs against
a faked urlopen and clock: no network, no real sleeps beyond a few short
waits for helper threads."""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from mangarr import anilist, core, db, downloader, health, jobs, limits, resolver, settings, suwayomi
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
    starts an outage for secs once op has been answered. Every request is
    logged as (op, variables, what happened)."""

    OPS = ("enqueueChapterDownloads", "dequeueChapterDownloads", "startDownloader", "downloadStatus",
           "aboutServer", "fetchSourceManga", "fetchMangaAndChapters", "fetchChapterPages", "sources", "manga(")

    def __init__(self, clock: Clock, sources: int = 1):
        self.clock, self.sources = clock, sources
        self.queue: list[int] = []
        self.downloaded: set[int] = set()
        self.log: list[tuple] = []
        self.outage: tuple[str, float] | None = None
        self.after: dict[str, tuple[str, float]] = {}
        self.block: threading.Event | None = None     # set: a hung request really blocks until it is set

    def requests(self, op: str) -> list[tuple]:
        return [(v, how) for o, v, how in self.log if o == op]

    def __call__(self, req, timeout):
        if req.full_url.startswith("https://graphql.anilist.co"):
            return io.BytesIO(b'{"data": {"Media": null}}')
        body = json.loads(req.data)
        op = next(o for o in self.OPS if o in body["query"])
        variables = body.get("variables") or {}
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
            return {op: {"mangas": []}}
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


# -- finding 78: a freeze while sources are searched ------------------------------------------

@unittest.skipIf(web is None, "web extras not installed")
class FrozenPassTest(Base):
    def seed(self, n, ref=lambda i: {"anilist_id": i}):
        with db.connect() as con:
            for i in range(1, n + 1):
                db.upsert_series(con, Series(english=f"S{i}", **ref(i)))
            return [dict(r) for r in db.series_rows(con)]

    def test_freeze_after_the_first_sources_call_ends_the_pass_in_minutes(self):   # the round-2 evidence
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
                with self.assertRaises(urllib.error.HTTPError):
                    anilist.by_id(1)
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
