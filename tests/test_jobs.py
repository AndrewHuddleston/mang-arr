"""The job layer's robustness: runner bounds and visibility, cancel, pass
awareness, clamped settings, short write transactions, user state kept,
Suwayomi outages and the circuit breaker, adopt paging, import keys.
Nothing here talks to a network or sleeps for real."""
import errno
import fcntl
import math
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import zipfile
from unittest import mock

from mangarr import config, core, db, downloader, jobs, limits, resolver, suwayomi
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, Client, Source, SuwayomiError, SuwayomiUnreachable

try:
    from fastapi.testclient import TestClient

    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    TestClient = web = None


def _match(name, manga_id, numbers, throttled=False):
    src = Source(str(manga_id), name, "en", throttled=throttled)
    chapters = [Chapter(manga_id * 1000 + int(n * 10), float(n), f"Chapter {n}", None, False) for n in numbers]
    return SourceMatch(src, manga_id, name, None, 0, name, 1, chapters)


def _plan(matches, series=None, unreachable=()):
    cands = {}
    for m in matches:
        for n in m.numbers:
            cands.setdefault(n, []).append(m)
    return Plan(series or Series(anilist_id=1, english="T"), matches, [], list(unreachable),
                {n: c[0] for n, c in cands.items()}, candidates=cands)


class FakeClient:
    """Suwayomi's download queue: downloads every enqueued chapter except
    `broken` ones, which stay in the queue in ERROR."""

    def __init__(self, broken=(), on_start=None):
        self.broken, self.on_start = set(broken), on_start
        self.have: set[int] = set()
        self.queued: list[int] = []

    def enqueue(self, ids): self.queued += [i for i in ids if i not in self.queued]
    def dequeue(self, ids, timeout=30): self.queued = [c for c in self.queued if c not in ids]

    def start(self):
        if self.on_start:
            self.on_start(self)
        for cid in self.queued:
            if cid not in self.broken:
                self.have.add(cid)
        self.queued = [c for c in self.queued if c in self.broken]

    def queue(self):
        return [{"id": cid, "state": "ERROR", "tries": 3, "progress": 0.0} for cid in self.queued]

    def downloaded_ids(self, manga_id):
        return {cid for cid in self.have if cid // 1000 == manga_id}


class TmpData(unittest.TestCase):
    """A temporary data dir (DB, lock), sleeps disabled, fresh settings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from mangarr import settings
        self.patches = [mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/t.db"),
                        mock.patch("mangarr.config.LOCK_PATH", self.tmp.name + "/lock"),
                        mock.patch("mangarr.config.STAGING_ROOT", self.tmp.name + "/staging"),
                        mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp.name + "/library"),
                        mock.patch("time.sleep", lambda s: None)]
        for p in self.patches:
            p.start()
        settings._cache.clear()
        limits._warned.clear()

    def tearDown(self):
        from mangarr import settings
        for p in self.patches:
            p.stop()
        settings._cache.clear()
        self.tmp.cleanup()

    def seed(self, chapters, sources=(("A", 1),)):
        """A series with chapter rows {number: status} and source entries."""
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=1, english="T"))
            for n, st in chapters.items():
                con.execute("INSERT INTO chapter (series_id, number, status, source_name, updated_at)"
                            " VALUES (?,?,?,?,?)", (sid, n, st, sources[0][0], db.now()))
            for name, mid in sources:
                con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level,"
                            " author_level, chapter_count, max_chapter, is_primary, seen_at)"
                            " VALUES (?,?,?,?,0,1,1,1,0,?)", (sid, mid, name, "T", db.now()))
        return sid

    def status(self, sid):
        with db.connect() as con:
            return {r["number"]: r["status"] for r in db.chapters(con, sid)}


# -- runner ---------------------------------------------------------------------------------

class RunnerTest(unittest.TestCase):
    def test_queued_jobs_are_never_trimmed(self):          # findings 36, 64, 82
        r = jobs.Runner(history=5)
        first = r.submit("refresh", "one", lambda j: None, series_id=7)
        for i in range(400):
            r.submit("chapter", f"c{i}", lambda j: None)
        self.assertIs(r.get(first.id), first)               # still visible ...
        self.assertTrue(r.pending_for(7))                   # ... blocks a delete ...
        self.assertTrue(r.cancel(first.id))                 # ... and can be cancelled
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(len(r.jobs()), 401)

    def test_only_finished_history_is_capped(self):
        r = jobs.Runner(history=3)
        done = [r.submit("x", str(i), lambda j: None) for i in range(6)]
        for j in done:
            j.status = "done"
        r.submit("x", "new", lambda j: None)
        self.assertEqual([j.title for j in r.jobs()], ["new", "5", "4", "3"])

    def test_queue_is_bounded(self):
        r = jobs.Runner(max_queued=3)
        for i in range(3):
            r.submit("metadata", str(i), lambda j: None)
        with self.assertLogs("mangarr.jobs", "WARNING"), self.assertRaises(jobs.QueueFull):
            r.submit("metadata", "one too many", lambda j: None)

    def test_key_dedupes_active_jobs(self):
        r = jobs.Runner()
        a = r.submit("metadata", "every series", lambda j: None, key="metadata")
        self.assertIs(r.submit("metadata", "every series", lambda j: None, key="metadata"), a)
        a.status = "done"
        self.assertIsNot(r.submit("metadata", "every series", lambda j: None, key="metadata"), a)

    def test_pending_for_sees_the_series_a_pass_is_on(self):   # finding 92
        r = jobs.Runner()
        p = r.submit("refresh-all", "all", lambda j: None)
        p.status, p.active_series_id = "running", 42
        self.assertTrue(r.pending_for(42))
        self.assertFalse(r.pending_for(43))

    def test_cancel_marks_the_job_and_runner_skips_it(self):
        r = jobs.Runner()
        ran = []
        j = r.submit("x", "x", lambda job: ran.append(job.id))
        self.assertTrue(r.cancel(j.id))
        self.assertTrue(j.cancel)                          # set in both branches (no check-then-set race)
        r2 = jobs.Runner()
        r2._pending, r2._jobs = r._pending, r._jobs
        r2._pending.append((None, None))                   # sentinel: ends the loop after the job
        with self.assertRaises(AttributeError):
            r2._loop()
        self.assertEqual(ran, [])

    def test_cancel_and_resubmit_does_not_grow_the_queue(self):     # second pass, regression 5
        r = jobs.Runner(max_queued=3)
        busy = r.submit("refresh-all", "long pass", lambda j: None)
        for i in range(50):
            j = r.submit("chapter", f"c{i}", lambda j: None)
            self.assertTrue(r.cancel(j.id))
        self.assertEqual([e[0] for e in r._pending], [busy])   # cancelled ones are gone at once
        for i in range(2):
            r.submit("chapter", f"more{i}", lambda j: None)    # and never counted against the bound

    def test_cancelled_job_that_raises_is_cancelled_not_failed(self):
        r = jobs.Runner()

        def fn(job):
            job.cancel = True
            raise downloader.Cancelled()
        j = r.submit("chapter", "c", fn)
        r._pending.append((None, None))
        with self.assertRaises(AttributeError), mock.patch.object(jobs.metrics, "record_job"), \
             self.assertLogs("mangarr.jobs", "WARNING"):
            r._loop()
        self.assertEqual(j.status, "cancelled")


class SchedulerTest(unittest.TestCase):              # findings 80, 89
    def test_bad_interval_never_breaks_next_at(self):
        s = jobs.Scheduler(jobs.Runner(), lambda j: None, interval_hours=6, first_after_s=60)
        s.trigger()
        for bad in (math.inf, math.nan, -1, 1e-9):
            with mock.patch("mangarr.settings.get", lambda k, bad=bad: bad):
                s.tick()
            self.assertTrue(math.isfinite(s.next_at))
            self.assertGreaterEqual(s.interval, 0.25 * 3600)
        with mock.patch("mangarr.settings.get", lambda k: 6.0):
            s.tick()
        self.assertAlmostEqual(s.next_at, s.last_at + 6 * 3600)

    def test_refresh_all_deduped(self):
        s = jobs.Scheduler(jobs.Runner(), lambda j: None)
        self.assertIs(s.trigger(), s.trigger())


class LimitsTest(unittest.TestCase):
    def test_clamp(self):
        with self.assertLogs("mangarr.limits", "WARNING"):
            self.assertEqual(limits.clamp("throttled_delay_seconds", 1e9), 600)
        self.assertEqual(limits.clamp("throttled_delay_seconds", "nan"), 8.0)     # the default
        self.assertEqual(limits.clamp("throttled_delay_seconds", -5), 0)
        self.assertEqual(limits.clamp("refresh_hours", math.inf), 6.0)
        self.assertEqual(limits.clamp("recheck_finished_days", 7), 7)

    def test_bad_values_clamped_when_saved(self):
        from mangarr import settings
        with tempfile.TemporaryDirectory() as tmp, db.connect(tmp + "/s.db") as con:
            settings._cache.clear()
            for key, bad, saved in (("throttled_delay_seconds", "1e9", 600.0), ("refresh_hours", "inf", 6.0),
                                    ("refresh_hours", "nan", 6.0), ("recheck_finished_days", "-1", 0.0)):
                with self.assertLogs("mangarr.settings", "WARNING"):
                    settings.set_many(con, {key: bad})
                self.assertEqual(settings.all_values(con)[key], saved)
            settings.set_many(con, {"refresh_hours": "12", "throttled_delay_seconds": "0"})
            self.assertEqual(settings.all_values(con)["refresh_hours"], 12.0)
        settings._cache.clear()

    def test_stale_out_of_range_value_does_not_block_saving(self):     # second pass, regression 2
        from mangarr import settings
        with tempfile.TemporaryDirectory() as tmp, db.connect(tmp + "/s.db") as con:
            settings._cache.clear()
            # written by an older version that did not check ranges
            con.execute("INSERT INTO setting (key, value) VALUES ('refresh_hours', '0.1')")
            con.commit()
            settings.refresh(con)
            form = {k: str(v) for k, v in settings.all_values(con).items() if isinstance(v, float)}
            with self.assertLogs("mangarr.settings", "WARNING"):
                settings.set_many(con, form)                        # the whole form posted back, unchanged
            self.assertEqual(settings.all_values(con)["refresh_hours"], 0.25)
        settings._cache.clear()

    def test_out_of_range_default_is_bounded(self):
        with mock.patch.dict("mangarr.settings.DEFAULTS", {"refresh_hours": 0.01}):
            self.assertEqual(limits.bound("refresh_hours", "nan"), 0.25)

    def test_pause_honours_cancel(self):
        calls = []
        with mock.patch("time.sleep", calls.append):
            self.assertTrue(limits.pause(1e6, lambda: len(calls) >= 3))
        self.assertEqual(len(calls), 3)


# -- downloader ------------------------------------------------------------------------------

class DownloaderTest(TmpData):
    def test_dequeued_when_start_fails(self):              # finding 68
        client = FakeClient()

        def boom(c):
            raise SuwayomiError("Suwayomi at x unreachable")
        client.on_start = boom
        a = _match("A", 1, [1])
        with self.assertRaises(SuwayomiError):
            downloader.download(client, _plan([a]), only={1.0}, in_order=True)
        self.assertEqual(client.queued, [])                # nothing of ours left behind

    def test_pace_only_on_throttled_sources(self):         # finding 89
        slept = []
        with mock.patch("time.sleep", slept.append):
            downloader.download(FakeClient(), _plan([_match("A", 1, [1, 2, 3])]), only={1.0, 2.0, 3.0},
                                in_order=True)
        self.assertLessEqual(max(slept), 2)                # polls and settles only: no 8 s pace per chapter

    def test_huge_pace_is_clamped_and_cancellable(self):   # findings 80, 89
        slept = []
        with mock.patch("mangarr.settings.get", lambda k: 1e9 if k == "throttled_delay_seconds" else True), \
             mock.patch("time.sleep", slept.append), self.assertLogs("mangarr.limits", "WARNING"):
            res = downloader.download(FakeClient(), _plan([_match("A", 1, [1, 2], throttled=True)]),
                                      only={1.0, 2.0}, should_cancel=lambda: len(slept) > 20)
        self.assertLessEqual(len(slept), 22)
        self.assertEqual(res, {1.0: "ok"})                 # cancelled during the pause

    def test_download_one_can_be_cancelled(self):
        with self.assertRaises(downloader.Cancelled):
            downloader.download_one(FakeClient(broken={1010}), 1, _match("A", 1, [1]).chapters[0], "T", "A",
                                    should_cancel=lambda: True)

    def test_bad_lock_wait_env_falls_back_to_the_default(self):     # second pass, regression 3
        # parsed by the shared config.env_number now (tests/test_env.py); negative is clamped to "do not wait"
        for bad, want in (("6h", 21600), ("nan", 21600), ("inf", 21600), ("-5", 0)):
            with mock.patch.dict(os.environ, {"MANGARR_LOCK_WAIT_SECS": bad}), \
                 self.assertLogs("mangarr.config", "WARNING"):
                self.assertEqual(config.env_number("MANGARR_LOCK_WAIT_SECS", 21600, 0, 7 * 86400), want)
        with mock.patch.dict(os.environ, {"MANGARR_LOCK_WAIT_SECS": "90"}):
            self.assertEqual(config.env_number("MANGARR_LOCK_WAIT_SECS", 21600, 0, 7 * 86400), 90)

    def test_queue_outage_warns_once_not_every_poll(self):          # second pass, regression 6
        class Down:
            calls = 0

            def queue(self):
                Down.calls += 1
                if Down.calls <= 10:
                    raise SuwayomiUnreachable("Suwayomi at x unreachable")
                return []
        with self.assertLogs("mangarr.downloader", "DEBUG") as cm:
            self.assertEqual(downloader._wait(Down(), [1], lambda: False, every=2), "done")
        warnings = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1, [r.getMessage() for r in warnings])

    def test_lock_wait_polls_cancel_and_gives_up(self):     # findings 79, 85
        path = self.tmp.name + "/lock"
        with downloader.download_lock(path):
            with open(path) as f:
                self.assertIn(f"pid {os.getpid()}", f.read())
            polls = []
            with self.assertRaises(downloader.Cancelled):
                with downloader.download_lock(path, should_cancel=lambda: len(polls) > 2, progress=polls.append):
                    pass
            self.assertIn("waiting for another download run", polls[0])
            with self.assertRaises(downloader.LockBusy), self.assertLogs("mangarr.downloader", "ERROR"):
                with downloader.download_lock(path, wait_secs=0):
                    pass
        with downloader.download_lock(path, wait_secs=0):    # free again
            pass

    def test_lock_held_by_another_open_file(self):
        path = self.tmp.name + "/lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(downloader.LockBusy):
                with downloader.download_lock(path, wait_secs=4):
                    pass
        finally:
            os.close(fd)


# -- core: transactions and user state ----------------------------------------------------------

class CoreTest(TmpData):
    def test_no_write_lock_held_while_the_next_source_downloads(self):   # findings 21, 33
        sid = self.seed({5: "failed"}, sources=(("A", 1), ("B", 2)))
        a, b = _match("A", 1, [5]), _match("B", 2, [5])
        client = FakeClient()
        client.chapters = lambda mid: (a if mid == 1 else b).chapters
        other_writer = []

        def fake_one(client_, manga_id, ch, title, source, should_cancel=None, progress=None):
            if manga_id == 1:
                return False, [5.0], {5.0: "broken"}
            c = sqlite3.connect(self.tmp.name + "/t.db", timeout=0.2)     # a web request meanwhile
            try:
                c.execute("UPDATE series SET monitored=0")
                c.commit()
                other_writer.append("ok")
            except sqlite3.OperationalError as e:
                other_writer.append(str(e))
            finally:
                c.close()
            return True, [], {}
        with db.connect() as con, mock.patch.object(downloader, "download_one", fake_one), \
             mock.patch.object(core, "import_series", lambda *a, **k: 0):
            msg = core.download_chapter(con, client, sid, 5.0)
        self.assertEqual(other_writer, ["ok"])
        self.assertIn("downloaded from B", msg)

    def test_ignored_chapters_not_downloaded_or_overwritten(self):     # finding 37
        sid = self.seed({1: "wanted", 2: "ignored", 3: "wanted", 4: "wanted"})
        a = _match("A", 1, [1, 2, 3, 4])
        client = FakeClient(broken={1030})                  # ch 3 fails on the only source

        def user_ignores_3(c):                              # the user clicks 'ignore' on 3 mid-run
            if 1010 in c.queued:
                w = sqlite3.connect(self.tmp.name + "/t.db")
                w.execute("UPDATE chapter SET status='ignored' WHERE series_id=? AND number=3", (sid,))
                w.commit()
                w.close()
        client.on_start = user_ignores_3
        with db.connect() as con, mock.patch("mangarr.settings.get", lambda k: True if k == "download_in_order"
                                               else 8.0):
            res = core.download_wanted(con, client, sid, _plan([a]))
        self.assertNotIn(2020, client.have)                 # ignored before the pass: not fetched
        self.assertNotIn(3.0, res)                          # ignored during the pass: skipped ...
        self.assertEqual(res.get(4.0), "ok")                # ... and it does not block the rest
        st = self.status(sid)
        self.assertEqual((st[2], st[3]), ("ignored", "ignored"))

    def test_failed_write_back_keeps_a_status_set_meanwhile(self):
        sid = self.seed({1: "wanted"})
        a = _match("A", 1, [1])

        def fake_download(client, plan, **kw):
            w = sqlite3.connect(self.tmp.name + "/t.db")
            w.execute("UPDATE chapter SET status='ignored' WHERE series_id=?", (sid,))
            w.commit()
            w.close()
            return {1.0: "failed"}
        with db.connect() as con, mock.patch.object(downloader, "download", fake_download):
            core.download_wanted(con, FakeClient(), sid, _plan([a]))
        self.assertEqual(self.status(sid)[1], "ignored")

    def test_save_plan_keeps_an_unreachable_sources_state(self):       # finding 94
        sid = self.seed({7: "wanted"}, sources=(("A", 1), ("B", 2)))
        b = _match("B", 2, [1])
        down = [(Source("1", "A", "en"), "search failed: HTTP 403")]
        with db.connect() as con:
            db.save_plan(con, sid, _plan([b], unreachable=down), 2)
            names = {r["source_name"] for r in db.sources(con, sid)}
        self.assertEqual(names, {"A", "B"})                 # A's entry (and staging folder) kept
        self.assertEqual(self.status(sid)[7], "wanted")     # not flipped to 'unavailable'
        with db.connect() as con:                          # once A answers and no longer lists 7, it is
            db.save_plan(con, sid, _plan([b]), 2)
        self.assertEqual(self.status(sid)[7], "unavailable")

    def test_a_source_down_for_too_long_loses_its_state(self):         # second pass, regression 4
        sid = self.seed({7: "wanted"}, sources=(("A", 1), ("B", 2)))
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - (db.UNREACHABLE_KEEP_DAYS + 1) * 86400))
        with db.connect() as con:
            con.execute("UPDATE series_source SET seen_at=? WHERE series_id=? AND source_name='A'", (old, sid))
            con.commit()
        down = [(Source("1", "A", "en"), "search failed: HTTP 403")]      # e.g. blocked for good
        with db.connect() as con:
            dropped = db.save_plan(con, sid, _plan([_match("B", 2, [1])], unreachable=down), 2)
            names = {r["source_name"] for r in db.sources(con, sid)}
        self.assertEqual(dropped, ["A"])
        self.assertEqual(names, {"B"})
        self.assertEqual(self.status(sid)[7], "unavailable")

    def test_a_down_former_primary_leaves_suwayomis_library(self):     # second pass, regression 4
        sid = self.seed({}, sources=(("A", 1), ("B", 2)))
        with db.connect() as con:
            con.execute("UPDATE series_source SET is_primary=1 WHERE series_id=? AND source_name='A'", (sid,))
            con.commit()
        plan = _plan([_match("B", 2, [1])], series=Series(anilist_id=1, english="T"),
                     unreachable=[(Source("1", "A", "en"), "DNS/network: unable to resolve host")])
        flags = {}
        client = mock.MagicMock()
        client.set_in_library.side_effect = lambda mid, on, **kw: flags.__setitem__(mid, on)
        with db.connect() as con, mock.patch.object(core, "resolve", lambda *a, **kw: plan):
            core.add_series(con, client, plan.series, download=False, do_import=False, series_id=sid)
            names = {r["source_name"] for r in db.sources(con, sid)}
        self.assertEqual(names, {"A", "B"})                 # A's entry is kept for now ...
        self.assertEqual(flags, {2: True, 1: False})        # ... but only B stays in Suwayomi's library

    def test_add_series_stops_if_deleted_during_download(self):       # finding 92
        sid = self.seed({})
        a = _match("A", 1, [1])
        s = Series(anilist_id=1, english="T")

        def delete_meanwhile(con, *a_, **k):
            w = sqlite3.connect(self.tmp.name + "/t.db")
            w.execute("PRAGMA foreign_keys=ON")
            w.execute("DELETE FROM series WHERE id=?", (sid,))
            w.commit()
            w.close()
            return {1.0: "ok"}
        imported = []
        with db.connect() as con, mock.patch.object(core, "resolve", lambda *a_, **k: _plan([a], s)), \
             mock.patch.object(core, "_set_library_entries", lambda *a_: None), \
             mock.patch.object(core, "download_wanted", delete_meanwhile), \
             mock.patch.object(core, "import_series", lambda con, sid_, c=None: imported.append(sid_) or 0):
            with self.assertRaises(core.Gone):
                core.add_series(con, FakeClient(), s, series_id=sid)
        self.assertEqual(imported, [sid])                    # only the import before the download ran

    def test_adopt_scan_is_paged_and_stops_early(self):    # finding 73
        pages = []

        class C:
            def mangas_page(self, offset, first):
                pages.append(offset)
                nodes = [{"id": offset + i, "title": f"T{offset + i}", "downloadCount": 1,
                          "source": {"displayName": "A"}} for i in range(first)]
                return nodes, True                         # "more" forever: paging must stop by itself
        with mock.patch.object(core, "ADOPT_PAGE", 10):
            out = core.suwayomi_downloaded_entries(C(), {("A", "T15")})
            self.assertEqual(out[("A", "T15")], 15)
            self.assertEqual(pages, [0, 10])
            pages.clear()
            with mock.patch.object(core, "ADOPT_MAX_ENTRIES", 50), self.assertLogs("mangarr.core", "WARNING"):
                core.suwayomi_downloaded_entries(C(), {("A", "missing")})
            self.assertEqual(len(pages), 5)

    def test_mangas_page_is_one_try(self):
        seen = {}

        def gq(query, variables=None, timeout=180, retries=3):
            seen.update(timeout=timeout, retries=retries, variables=variables)
            return {"mangas": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        c = Client("http://x")
        c.gq = gq
        self.assertEqual(c.mangas_page(0, 500), ([], False))
        self.assertEqual((seen["retries"], seen["variables"]), (1, {"first": 500, "offset": 0}))


# -- Suwayomi outages -------------------------------------------------------------------------

class CoreSplitTest(TmpData):
    """download_wanted in parts, so a pass can resolve a series, hand its
    chapters to a download lane, and write the outcome later."""

    def seed_mixed(self):
        sid = self.seed({1: "have", 2: "ignored", 3: "failed", 4: "failed", 5: "wanted"})
        with db.connect() as con:
            con.execute("UPDATE chapter SET next_try='2999-01-01 00:00:00' WHERE series_id=? AND number=3", (sid,))
            con.execute("UPDATE chapter SET next_try='2000-01-01 00:00:00' WHERE series_id=? AND number=4", (sid,))
        return sid, _plan([_match("A", 1, [1, 2, 3, 4, 5, 6])])

    def test_downloads_due_filters_like_download_wanted(self):
        sid, plan = self.seed_mixed()
        asked = {}

        def fake_download(client, plan, only=None, **kw):
            asked["only"] = only
            return {}
        with db.connect() as con:
            con.execute("UPDATE series SET monitored=1 WHERE id=?", (sid,))      # a write left open
            with self.assertLogs("mangarr.core", "INFO") as cm:
                due = core.downloads_due(con, core.Outcome(sid, plan))
            self.assertFalse(con.in_transaction)
            with mock.patch.object(downloader, "download", fake_download):
                core.download_wanted(con, FakeClient(), sid, plan)
        self.assertEqual(due, [4.0, 5.0, 6.0])              # not on disk, not ignored, not failed until later
        self.assertEqual(asked["only"], set(due))
        self.assertIn("1 failed chapter(s) not due", "\n".join(cm.output))

    def test_nothing_due_without_a_usable_source(self):
        sid = self.seed({1: "wanted"})
        m = _match("A", 1, [1])
        m.note = "author differs"
        with db.connect() as con:
            self.assertEqual(core.downloads_due(con, core.Outcome(sid, _plan([m]))), [])

    def test_record_downloads(self):
        sid = self.seed({1: "wanted", 2: "wanted", 3: "wanted", 4: "wanted", 5: "wanted"})
        plan = _plan([_match("A", 1, [1, 2, 3, 4, 5])])
        with db.connect() as con:
            con.execute("UPDATE chapter SET status='ignored' WHERE series_id=? AND number=4", (sid,))  # the user, meanwhile
            con.commit()
            core.record_downloads(con, sid, plan, [1.0, 2.0, 3.0, 4.0, 5.0], {1.0: "ok", 2.0: "failed", 4.0: "failed"},
                                  {2.0: "broken", 3.0: "waiting for chapter 2: in order"}, {"A"})
            self.assertFalse(con.in_transaction)
            rows = {r["number"]: (r["status"], r["reason"]) for r in db.chapters(con, sid)}
            event = db.events(con, 1)[0]
            stats = db.source_stats(con)["A"]
        self.assertEqual(rows[2.0][0], "failed")
        self.assertTrue(rows[2.0][1].startswith("broken"), rows[2.0])
        self.assertEqual(rows[3.0], ("wanted", "waiting for chapter 2: in order"))
        self.assertEqual(rows[4.0][0], "ignored")                                   # kept
        self.assertIn("cancelled or interrupted", rows[5.0][1])
        self.assertEqual((event["kind"], event["message"]), ("downloaded", "1 chapter(s) downloaded, 2 failed: "
                                                                           "ch 2: broken; ch 4: ?"))
        self.assertEqual((stats["ok"], stats["failed"], stats["throttled"]), (1, 2, 1))
        self.assertIsNotNone(stats["last_throttled"])

    def test_download_wanted_still_records_throttling(self):
        sid = self.seed({1: "wanted"})

        def fake_download(client, plan, throttled=None, **kw):
            throttled.add("A")
            return {1.0: "ok"}
        with db.connect() as con, mock.patch.object(downloader, "download", fake_download):
            self.assertEqual(core.download_wanted(con, FakeClient(), sid, _plan([_match("A", 1, [1])])), {1.0: "ok"})
            self.assertEqual(db.auto_throttled(con), {"a"})

    def test_finish_download(self):
        sid = self.seed({1: "wanted"})
        plan = _plan([_match("A", 1, [1])])
        with db.connect() as con, mock.patch.object(core, "import_series", lambda con, sid, client=None: 2):
            out = core.finish_download(con, FakeClient(), core.Outcome(sid, plan, imported=1))
            self.assertEqual(out.imported, 3)
            db.delete_series(con, sid)
            con.commit()
            with self.assertRaises(core.Gone):
                core.finish_download(con, FakeClient(), core.Outcome(sid, plan))

    def test_import_failure_is_committed_at_once(self):
        sid = self.seed({1: "wanted"})
        with db.connect() as con, self.assertLogs("mangarr.core", "ERROR"):
            core._import_failed(con, sid, "T", 1.0, "A: cannot read Chapter 1.cbz", OSError("boom"))
            self.assertFalse(con.in_transaction)
            other = sqlite3.connect(self.tmp.name + "/t.db", timeout=0.2)     # a web request meanwhile
            try:
                other.execute("UPDATE series SET monitored=0")
                other.commit()
            finally:
                other.close()
        self.assertEqual(self.status(sid)[1], "failed")


class ImportWriteLockTest(TmpData):                    # findings 21, 33
    """import_series commits every write at once, so another connection can
    write while it checks each archive, links each file and asks Suwayomi,
    whatever happened to the files before (a link failed, a bad file could
    not be set aside, a file vanished)."""

    def setUp(self):
        super().setUp()
        self.probes: list = []
        self.con = None
        self.patches.append(mock.patch.object(core.komga, "scan", lambda: False))
        self.patches[-1].start()

    def stage(self, source, name, data=None):
        d = os.path.join(self.tmp.name, "staging", source, "T")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        if data is None:
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("001.jpg", os.urandom(2000))
        else:
            with open(p, "wb") as f:
                f.write(data)
        t = time.time() - 3600                              # settled, not still being written
        os.utime(p, (t, t))
        return p

    def probe(self, what):
        """What a web request writing right now gets, with a short busy timeout."""
        c = sqlite3.connect(self.tmp.name + "/t.db", timeout=0.5)
        try:
            c.execute("UPDATE series SET monitored=monitored")
            c.commit()
            res = "ok"
        except sqlite3.OperationalError as e:
            res = str(e)
        finally:
            c.close()
        self.probes.append((what, self.con.in_transaction, res))

    def run_import(self, sid, client=None, fail_link=(), refuse_quarantine=()):
        verify, link, quarantine = core.library.verify_archive, core.library.link_into_library, core.library.quarantine

        def probed_verify(f):
            self.probe(f"verify {f.name}")
            return verify(f)

        def probed_link(f, *a, **kw):
            self.probe(f"link {f.name}")
            if f.name in fail_link:
                raise OSError(errno.ENOSPC, "No space left on device")
            return link(f, *a, **kw)

        def maybe_quarantine(f):
            if f.name in refuse_quarantine:
                raise PermissionError(errno.EACCES, "Permission denied", f.path)
            return quarantine(f)
        with db.connect() as con, mock.patch.object(core.library, "verify_archive", probed_verify), \
                mock.patch.object(core.library, "link_into_library", probed_link), \
                mock.patch.object(core.library, "quarantine", maybe_quarantine), self.assertLogs("mangarr", "INFO"):
            self.con = con
            return core.import_series(con, sid, client)

    def assert_never_locked(self):
        self.assertTrue(self.probes)
        self.assertEqual([p for p in self.probes if p[1] or p[2] != "ok"], [])

    @unittest.skipIf(os.geteuid() == 0, "root writes through a read-only folder")
    def test_unwritable_library(self):                  # the evidence repro: the usual PUID/PGID mistake
        sid = self.seed({1: "wanted", 2: "wanted", 3: "wanted"})
        for n in (1, 2, 3):
            self.stage("A", f"Chapter {n}.cbz")
        lib = os.path.join(self.tmp.name, "library")
        os.makedirs(lib)
        os.chmod(lib, 0o555)
        try:
            self.assertEqual(self.run_import(sid), 0)
        finally:
            os.chmod(lib, 0o755)
        self.assertEqual([w for w, _, _ in self.probes], ["verify Chapter 1.cbz", "link Chapter 1.cbz",
                                                          "verify Chapter 2.cbz", "link Chapter 2.cbz",
                                                          "verify Chapter 3.cbz", "link Chapter 3.cbz"])
        self.assert_never_locked()
        self.assertEqual(self.status(sid), {1.0: "failed", 2.0: "failed", 3.0: "failed"})

    def test_every_write_is_committed_before_the_next_slow_step(self):
        sid = self.seed({n: "wanted" for n in range(1, 8)}, sources=(("A", 1), ("B", 2)))
        self.stage("A", "Chapter 1.cbz", b"PK" + b"x" * 3000)     # bad, and cannot be set aside
        self.stage("A", "Chapter 2.cbz")                            # the link fails (disk full)
        self.stage("A", "Chapter 3.cbz", b"PK" + b"x" * 3000)     # bad, set aside
        gone = self.stage("A", "Chapter 4.cbz")                     # vanishes after the folder is listed
        self.stage("A", "Chapter 5.cbz")
        self.stage("B", "Official_Special.cbz")                     # no number: Suwayomi is asked
        real_scan = core.library.StagingFolder.scan

        def scan_then_vanish(sf):
            found = real_scan(sf)
            if os.path.exists(gone):
                os.remove(gone)
            return found

        class Client:
            def chapters(client, manga_id):
                self.probe(f"chapters {manga_id}")
                return [Chapter(27, 7.0, "Special", "Official", True)]
        with mock.patch.object(core.library.StagingFolder, "scan", scan_then_vanish):
            linked = self.run_import(sid, Client(), fail_link={"Chapter 2.cbz"}, refuse_quarantine={"Chapter 1.cbz"})
        self.assertEqual(linked, 2)
        self.assertEqual([w for w, _, _ in self.probes],
                         ["verify Chapter 1.cbz", "verify Chapter 2.cbz", "link Chapter 2.cbz", "verify Chapter 3.cbz",
                          "verify Chapter 5.cbz", "link Chapter 5.cbz", "chapters 2", "verify Official_Special.cbz",
                          "link Official_Special.cbz"])
        self.assert_never_locked()
        self.assertEqual(self.status(sid), {1.0: "failed", 2.0: "failed", 3.0: "failed", 4.0: "wanted",
                                            5.0: "have", 6.0: "wanted", 7.0: "have"})


class BreakerTest(unittest.TestCase):                 # finding 78
    def setUp(self):
        suwayomi._down.clear()
        self.addCleanup(suwayomi._down.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch("mangarr.config.DB_PATH", tmp.name + "/t.db"),
                  mock.patch("mangarr.settings.all_values",
                             lambda con=None: {"unusable_sources": [], "throttled_sources": []})):
            p.start()
            self.addCleanup(p.stop)

    def test_refused_trips_the_breaker_and_fails_fast(self):
        calls = []

        def refuse(req, timeout):
            calls.append(timeout)
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        c = Client("http://suwayomi.test")
        with mock.patch("urllib.request.urlopen", refuse), mock.patch("time.sleep", lambda s: None), \
             self.assertLogs("mangarr.suwayomi", "ERROR"):
            with self.assertRaises(SuwayomiUnreachable):
                c.sources()
            self.assertEqual(len(calls), 2)                 # its own retries
            with self.assertRaises(SuwayomiUnreachable):
                Client("http://suwayomi.test").chapters(1)  # any client: no network call at all
            self.assertEqual(len(calls), 2)
        down_at, why = suwayomi._down["http://suwayomi.test/api/graphql"]
        suwayomi._down["http://suwayomi.test/api/graphql"] = (down_at - suwayomi.BREAKER_SECS, why)

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return b'{"data": {"sources": {"nodes": []}}}'
        with mock.patch("urllib.request.urlopen", lambda req, timeout: Ok()):
            self.assertEqual(c.sources(), [])               # window over: tried again, and closed
        self.assertNotIn("http://suwayomi.test/api/graphql", suwayomi._down)

    def test_slow_source_search_is_not_an_outage(self):
        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return b'{"data": {"aboutServer": {"version": "v2"}}}'

        def slow(req, timeout):                             # the website is slow; Suwayomi itself answers
            if b"fetchSourceManga" in req.data:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return Ok()
        c = Client("http://suwayomi.test")
        with mock.patch("urllib.request.urlopen", slow):
            with self.assertRaises(SuwayomiError) as cm:
                c.search(Source("1", "A", "en"), "x")
        self.assertNotIsInstance(cm.exception, SuwayomiUnreachable)
        self.assertEqual(suwayomi._down, {})


class ResolverErrorsTest(unittest.TestCase):          # finding 94
    def setUp(self):
        resolver._unreachable.clear()
        self.addCleanup(resolver._unreachable.clear)

    def _search(self, exc):
        client = mock.Mock()
        client.search.side_effect = exc
        return resolver._search_source(client, Source("9", "Weeb", "en"), Series(english="T"), ["T"], [])

    def test_one_odd_error_is_not_remembered(self):
        self.assertIsInstance(self._search(SuwayomiError("HTTP error 403")), str)
        self.assertEqual(resolver._unreachable, {})

    def test_dns_failure_is_remembered_briefly(self):
        why = self._search(SuwayomiError("Unable to resolve host \"weebcentral.com\""))
        self.assertIn("DNS", why)
        self.assertIn("9", resolver._unreachable)
        self.assertLessEqual(resolver.UNREACHABLE_TTL, 900)

    def test_suwayomi_down_aborts_the_resolve(self):
        with self.assertRaises(SuwayomiUnreachable):
            self._search(SuwayomiUnreachable("Suwayomi at x unreachable"))
        self.assertEqual(resolver._unreachable, {})


@unittest.skipIf(web is None, "web extras not installed")
class RunPassOutageTest(unittest.TestCase):
    def _run(self, fake_refresh, n=4):
        rows = [{"id": i, "title": f"S{i}"} for i in range(1, n + 1)]
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(core, "refresh_series", fake_refresh), \
             mock.patch.object(core, "describe_outcome", lambda con, sid, o: ("done", "ok")), \
             mock.patch.object(web.db, "connect", mock.MagicMock()), \
             mock.patch.object(web.limits, "pause", lambda s, c=None: False):
            try:
                return job, web._run_pass(job, rows, "test")
            except SuwayomiUnreachable as e:
                return job, e

    def test_stops_with_one_error_when_suwayomi_stays_down(self):
        calls = []

        def down(con, client, sid, **kw):
            calls.append(sid)
            raise SuwayomiUnreachable("Suwayomi at x unreachable")
        with mock.patch.object(web, "_record_error", lambda sid, e: None), self.assertLogs(web.log, "ERROR"):
            job, out = self._run(down, n=10)
        self.assertIsInstance(out, SuwayomiUnreachable)
        self.assertIn("pass stopped after 2 of 10", str(out))
        self.assertEqual(calls, [1, 2])                         # not 10 timeouts
        self.assertEqual([i["state"] for i in job.items[2:]], ["cancelled"] * 8)

    def test_refresh_all_still_notifies_when_the_pass_stops(self):   # second pass, regression 1
        class R:
            downloaded = imported = 1

        def first_then_down(con, client, sid, **kw):
            if sid == 1:
                return R()
            raise SuwayomiUnreachable("Suwayomi at x unreachable")
        rows = [{"id": i, "title": f"S{i}", "wanted": 1} for i in range(1, 6)]
        job = jobs.Job(1, "refresh-all", "all")
        sent = []
        with mock.patch.object(core, "refresh_series", first_then_down), \
             mock.patch.object(core, "describe_outcome", lambda con, sid, o: ("done", "1 downloaded")), \
             mock.patch.object(web.db, "connect", mock.MagicMock()), \
             mock.patch.object(web, "plan_pass", lambda r: (rows, 0)), \
             mock.patch.object(web, "_record_error", lambda sid, e: None), \
             mock.patch.object(web.limits, "pause", lambda s, c=None: False), \
             mock.patch.object(web.notify, "send", lambda title, body, kind: sent.append((kind, title))), \
             self.assertLogs(web.log, "ERROR"):
            with self.assertRaises(SuwayomiUnreachable) as cm:
                web._job_refresh_all(job)
        self.assertIn("pass stopped after 3 of 5", str(cm.exception))
        self.assertIn(("new", "mang-arr: 1 new chapter(s)"), sent)       # S1's chapter is still announced
        self.assertIn("failed", [k for k, _ in sent])

    def test_record_error_failure_does_not_end_the_pass(self):     # finding 33
        class R:
            downloaded = imported = 0

        def flaky(con, client, sid, **kw):
            if sid == 2:
                raise RuntimeError("boom")
            return R()

        def locked(sid, e):
            raise sqlite3.OperationalError("database is locked")
        with mock.patch.object(web, "_record_error", locked):
            job, out = self._run(flaky)
        self.assertEqual(out, (4, 0, 0, 1))
        self.assertEqual([i["state"] for i in job.items], ["done", "error", "done", "done"])

    def test_active_series_is_visible_during_the_pass(self):       # finding 92
        seen = []

        class R:
            downloaded = imported = 0

        def look(con, client, sid, **kw):
            seen.append(job_ref[0].active_series_id)
            return R()
        job_ref = []
        rows = [{"id": 5, "title": "A"}, {"id": 6, "title": "B"}]
        job = jobs.Job(1, "refresh-all", "all")
        job_ref.append(job)
        with mock.patch.object(core, "refresh_series", look), \
             mock.patch.object(core, "describe_outcome", lambda con, sid, o: ("done", "ok")), \
             mock.patch.object(web.db, "connect", mock.MagicMock()):
            web._run_pass(job, rows, "test")
        self.assertEqual(seen, [5, 6])
        self.assertIsNone(job.active_series_id)


# -- import form ------------------------------------------------------------------------------

@unittest.skipIf(TestClient is None, "web extras not installed")
class ImportApplyTest(unittest.TestCase):              # finding 91
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/t.db"),
                  mock.patch("mangarr.config.DATA_DIR", self.tmp.name)):
            p.start()
            self.addCleanup(p.stop)
        self.berserk = core.AdoptItem("A", "Berserk", "/staging/A/Berserk", {1.0: "x"}, [])
        self.bleach = core.AdoptItem("A", "Bleach", "/staging/A/Bleach", {1.0: "y"}, [])
        self.submitted = []
        p = mock.patch.object(web.runner, "submit", lambda kind, title, fn, *a, **k: self.submitted.append(fn))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(web._adopt_scan, {"items": [self.berserk], "job": None, "gen": 3})
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(web.app)

    def test_choice_follows_the_folder_not_the_position(self):
        form = {"gen": "3", f"choice_{self.berserk.key}": "manual"}
        web._adopt_scan["items"] = [self.bleach, self.berserk]      # a rescan put Bleach first
        r = self.client.post("/import/apply", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.submitted), 1)
        chosen = self.submitted[0].__closure__
        picked = [c.cell_contents for c in chosen if isinstance(c.cell_contents, list)][0]
        self.assertEqual([i.folder_name for i in picked], ["Berserk"])
        self.assertIsNone(self.berserk.series)                     # the shared scan result is not mutated

    def test_stale_form_is_refused(self):
        web._adopt_scan["gen"] = 4                                 # a scan finished after the page loaded
        with self.assertLogs(web.log, "WARNING"):
            r = self.client.post("/import/apply", data={"gen": "3", f"choice_{self.berserk.key}": "manual"},
                                 follow_redirects=False)
        self.assertIn("changed", r.headers["location"])
        self.assertEqual(self.submitted, [])

    def test_keys_are_stable_and_distinct(self):
        again = core.AdoptItem("A", "Berserk", "/staging/A/Berserk", {}, [])
        self.assertEqual(again.key, self.berserk.key)
        self.assertNotEqual(self.bleach.key, self.berserk.key)


@unittest.skipIf(TestClient is None, "web extras not installed")
class SubmitDedupeTest(unittest.TestCase):            # findings 36, 64, 82
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/t.db"),
                  mock.patch("mangarr.config.DATA_DIR", self.tmp.name),
                  mock.patch.object(web, "runner", jobs.Runner(max_queued=5))):
            p.start()
            self.addCleanup(p.stop)
        with db.connect() as con:
            self.sid = db.upsert_series(con, Series(anilist_id=1, english="T"))
        self.client = TestClient(web.app)

    def test_repeated_submissions_queue_one_job(self):
        for _ in range(20):
            self.client.post("/system/metadata-refresh", follow_redirects=False)
            self.client.post(f"/series/{self.sid}/chapter/3/search", follow_redirects=False)
            self.client.post("/api/v1/command", json={"name": "SearchWanted"})
        kinds = sorted(j.kind for j in web.runner.jobs())
        self.assertEqual(kinds, ["chapter", "metadata", "search-wanted"])

    def test_full_queue_is_429(self):
        for n in range(5):
            self.client.post(f"/series/{self.sid}/chapter/{n}/search", follow_redirects=False)
        with self.assertLogs("mangarr.jobs", "WARNING"):
            r = self.client.post(f"/api/v1/series/{self.sid}/chapter/9/search")
        self.assertEqual(r.status_code, 429)


class TimingSanityTest(unittest.TestCase):
    def test_pause_does_not_busy_loop_when_sleep_is_patched(self):
        t0 = time.monotonic()
        with mock.patch("time.sleep", lambda s: None):
            limits.pause(600, lambda: False)
        self.assertLess(time.monotonic() - t0, 1)


if __name__ == "__main__":
    unittest.main()
