"""A whole refresh pass through the real code: web._run_pass resolves each
series (core.refresh_series without download; only the source search is a
stand-in that builds the plan), hands the chapters due to the download
lanes (lanes.LanePool -> downloader._download_source, page by page where
the source needs it), writes what came of them and imports them into the
library. Suwayomi is fake_suwayomi.FakeSuwayomi, which downloads in real
time in short steps; nothing reaches a real Suwayomi, AniList or Komga."""
import os
import shutil
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_suwayomi import PassBase, chapter_id, entry, number_of, resolver_for  # noqa: E402

from mangarr import core, db, downloader, jobs, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402

try:
    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    web = None

X, Y, Z = "Site X (EN)", "Site Y", "Site Z"


@unittest.skipIf(web is None, "web extras not installed")
class PipelineBase(PassBase):
    def seed(self, *titles) -> list[dict]:
        with db.connect() as con:
            for t in titles:
                db.upsert_series(con, Series(english=t))
            by_title = {r["title"]: dict(r) for r in db.series_rows(con)}
        return [by_title[t] for t in titles]

    def run_pass(self, fake, plans, rows, job=None, in_order=True):
        job = job or jobs.Job(1, "refresh-all", "all")
        with db.connect() as con:
            settings.set_many(con, {"download_in_order": in_order})
        settings._cache.clear()
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolver_for(fake, plans)):
            t0 = time.perf_counter()
            out = web._run_pass(job, rows, "test")
            self.took = time.perf_counter() - t0
        return job, out

    def statuses(self, row) -> dict:
        return {n: st for n, (st, _) in self.status(row["id"]).items()}


class ConcurrencyTest(PipelineBase):
    def two(self, lanes: int):
        with db.connect() as con:
            settings.set_many(con, {"download_lanes": lanes})
        fake = self.fake()
        plans = {"Alpha": [entry(fake, X, 1, "Alpha", [1, 2])], "Beta": [entry(fake, Y, 2, "Beta", [1, 2])]}
        fake.hold_until_other(X, Y, timeout=0.4)
        rows = self.seed("Alpha", "Beta")
        job, out = self.run_pass(fake, plans, rows)
        return fake, rows, job, out

    def test_two_series_on_different_sources_download_concurrently(self):
        fake, rows, job, out = self.two(3)
        self.assertTrue(fake.overlap_seen)
        self.assertEqual(out, (2, 4, 4, 0))
        for r in rows:
            self.assertEqual(self.statuses(r), {1.0: "have", 2.0: "have"})
        self.assertEqual([i["state"] for i in job.items], ["done", "done"])
        self.assertEqual((job.lanes, job.active_series_ids), ([], frozenset()))
        self.assertTrue(self.lock_free())

    def test_one_lane_downloads_one_series_at_a_time(self):      # the control
        fake, rows, job, out = self.two(1)
        self.assertFalse(fake.overlap_seen)
        self.assertEqual(out, (2, 4, 4, 0))


class SitesTest(PipelineBase):
    def test_never_two_series_on_one_site(self):
        for in_order in (True, False):
            with self.subTest(in_order=in_order):
                fake = self.fake(lanes=3)
                plans = {"S1": [entry(fake, X, 1, "S1", [1, 2, 3])], "S2": [entry(fake, X, 2, "S2", [1, 2, 3])],
                         "S3": [entry(fake, X, 3, "S3", [1, 2])], "S4": [entry(fake, Y, 4, "S4", [1, 2, 3])],
                         "S5": [entry(fake, "Site X (ALL)", 5, "S5", [1, 2])]}
                with db.connect() as con:
                    for r in db.series_rows(con):
                        db.delete_series(con, r["id"])
                for d in ("staging", "library"):               # nothing left from the first round
                    shutil.rmtree(os.path.join(self.tmp, d))
                    os.makedirs(os.path.join(self.tmp, d))
                rows = self.seed(*plans)
                job, out = self.run_pass(fake, plans, rows, in_order=in_order)
                self.assertEqual(fake.violations, [])
                self.assertEqual(out, (5, 13, 13, 0))
                for r in rows:
                    self.assertEqual(set(self.statuses(r).values()), {"have"}, r["title"])

    def test_in_order_per_series(self):
        fake = self.fake()
        plans = {"S1": [entry(fake, X, 1, "S1", [1, 2]), entry(fake, Y, 2, "S1", [3, 4])],
                 "S2": [entry(fake, Z, 3, "S2", [1, 2, 3, 4, 5])],
                 "S3": [entry(fake, X, 4, "S3", [1, 2, 3])]}
        fake.broken = {chapter_id(3, 3)}              # S2's chapter 3, on its only source
        rows = self.seed(*plans)
        job, out = self.run_pass(fake, plans, rows)
        series_of = {1: "S1", 2: "S1", 3: "S2", 4: "S3"}
        enq: dict[str, list] = {}
        for t, _, _, mid, cid in fake.kinds("enqueue"):
            enq.setdefault(series_of[mid], []).append((t, number_of(cid)))
        finished = {cid: t for t, _, _, _, cid in fake.kinds("finish")}
        for title, seq in enq.items():
            nums = [n for _, n in seq]
            self.assertEqual(nums, sorted(set(nums)), f"{title} out of order: {nums}")
            by_num = {n: t for t, n in seq}
            mids = [m for m, s in series_of.items() if s == title]
            for n, t in by_num.items():
                prev = [finished[chapter_id(m, n - 1)] for m in mids if chapter_id(m, n - 1) in finished]
                if n > 1:
                    self.assertTrue(prev and prev[0] < t, f"{title}: ch {n:g} queued before ch {n - 1:g} arrived")
        self.assertEqual([n for _, n in enq["S2"]], [1.0, 2.0, 3.0])     # 4 and 5 never queued
        s2 = self.status(rows[1]["id"])
        self.assertEqual(s2[3.0][0], "failed")
        for n in (4.0, 5.0):
            self.assertEqual(s2[n][0], "wanted")
            self.assertTrue(s2[n][1].startswith("waiting for chapter 3: chapters download in order"), s2[n][1])
        self.assertEqual(set(self.statuses(rows[0]).values()), {"have"})
        self.assertEqual(set(self.statuses(rows[2]).values()), {"have"})
        self.assertEqual([i["state"] for i in job.items], ["done", "done", "done"])


COMICK = "Comick (Unoriginal) (EN)"


class PageByPageTest(PipelineBase):
    def plans(self, fake):
        return {"Cee": [entry(fake, "Weeb Central", 1, "Cee", [1, 3]), entry(fake, COMICK, 2, "Cee", [2])],
                "Dee": [entry(fake, "Weeb Central", 3, "Dee", [1, 2, 3, 4, 5, 6])]}

    def test_page_by_page_chapter_in_a_pass(self):
        ch2 = chapter_id(2, 2)
        fake = self.fake(page_warm={COMICK}, busy_pages={(ch2, 2): 1}, page_secs=0.05)
        plans = self.plans(fake)
        rows = self.seed("Cee", "Dee")
        with self.assertLogs("mangarr.pagewarm", "INFO") as cm:
            job, out = self.run_pass(fake, plans, rows)
        self.assertIn(f"{COMICK} ch 2: 4/4 pages fetched one by one", "\n".join(cm.output))
        pages = [e for e in fake.kinds("page") if e[4] == ch2]
        enq = [e for e in fake.kinds("enqueue") if e[4] == ch2]
        self.assertEqual(len(enq), 1)
        self.assertTrue(pages and pages[-1][0] < enq[0][0])            # every page before the enqueue
        self.assertEqual([st for c, k, st in fake.page_log if c == ch2 and k == 2], ["busy", "ok"])
        self.assertEqual(self.statuses(rows[0]), {1.0: "have", 2.0: "have", 3.0: "have"})
        self.assertEqual([number_of(e[4]) for e in fake.kinds("enqueue") if e[3] in (1, 2)], [1.0, 2.0, 3.0])
        # Weeb Central went on with Dee while Comick fetched Cee's pages
        dee = [t for t, _, _, mid, _ in fake.kinds("finish") if mid == 3]
        self.assertTrue(any(pages[0][0] < t < enq[0][0] for t in dee), (pages[0][0], enq[0][0], dee))
        self.assertEqual(out, (2, 9, 9, 0))

    def test_page_warm_falls_back_on_bad_urls(self):
        ch2 = chapter_id(2, 2)
        fake = self.fake(page_warm={COMICK})
        fake.bad_urls = {ch2}
        plans = self.plans(fake)
        rows = self.seed("Cee", "Dee")
        with self.assertLogs("mangarr.pagewarm", "ERROR"):
            job, out = self.run_pass(fake, plans, rows)
        self.assertEqual(fake.kinds("page"), [])                       # the foreign URL was never requested
        self.assertEqual(len([e for e in fake.kinds("enqueue") if e[4] == ch2]), 1)
        st = self.status(rows[0]["id"])
        self.assertEqual(st[2.0][0], "failed")
        self.assertIn("the source has no working pages for this chapter", st[2.0][1])
        self.assertTrue(st[3.0][1].startswith("waiting for chapter 2"))


class StopTest(PipelineBase):
    def many(self, fake, n, chapters=4):
        sites = [f"Site {k}" for k in range(1, n + 1)]
        return {f"S{k}": [entry(fake, sites[k - 1], k, f"S{k}", range(1, chapters + 1))] for k in range(1, n + 1)}

    def test_cancel_stops_every_lane(self):
        fake = self.fake(secs=0.05)
        plans = self.many(fake, 4)
        rows = self.seed(*plans)
        job = jobs.Job(1, "refresh-all", "all")
        seen, at = [], []

        def on_finish(f, cid):
            seen.append(cid)
            if len(seen) == 2:
                at.append(time.perf_counter() - f.t0)
                job.cancel = True
        fake.on_finish = on_finish
        job, out = self.run_pass(fake, plans, rows, job=job)
        self.assertLess(self.took, 10)
        late = [e for e in fake.kinds("enqueue") if e[0] > at[0]]
        # a lane already past its last cancel check may still queue its chapter; it is taken out at once
        self.assertLessEqual(len(late), 3)
        for e in late:
            self.assertIn(e[4], [d[4] for d in fake.kinds("dequeue") if d[0] >= e[0]])
        self.assertEqual(fake.items, [])                               # nothing of ours left in the queue
        self.assertEqual(job.items[3]["state"], "cancelled")           # never started: 3 lanes, 4 sites
        self.assertEqual(job.items[3]["result"], "pass cancelled")
        for it in job.items[:3]:
            self.assertEqual(it["state"], "cancelled")
            self.assertTrue(it["result"].startswith("pass cancelled"), it["result"])
        for r in rows:
            for n, (st, why) in self.status(r["id"]).items():
                self.assertIn(st, ("have", "wanted"), (r["title"], n, why))
        started = [r for r in rows[:3] if "wanted" in self.statuses(r).values()]
        for r in started:
            reasons = {why for st, why in self.status(r["id"]).values() if st == "wanted"}
            self.assertTrue(all(w.startswith("not attempted") for w in reasons), reasons)
        self.assertTrue(self.lock_free())

    def test_suwayomi_down_stops_the_pass(self):
        fake = self.fake(secs=0.05)
        plans = self.many(fake, 8, chapters=3)
        rows = self.seed(*plans)
        job = jobs.Job(1, "refresh-all", "all")
        seen, at = [], []

        def on_finish(f, cid):
            seen.append(cid)
            if len(seen) == 3:
                at.append(f.set_down())
        fake.on_finish = on_finish
        sent = []
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolver_for(fake, plans)), \
             mock.patch.object(web, "plan_pass", lambda r: (rows, 0)), \
             mock.patch.object(web.notify, "send", lambda title, body, kind: sent.append(kind)), \
             self.assertLogs(level="ERROR"):
            with self.assertRaises(web.PassStopped) as cm:
                web._job_refresh_all(job)
        msg = str(cm.exception)
        self.assertIn("pass stopped after", msg)
        self.assertIn("unreachable", msg)
        self.assertIn("failed", sent)
        self.assertEqual([e for e in fake.kinds("enqueue") if e[0] > at[0]], [])
        for cid in seen[:3]:                            # arrived before the outage (the fake downloads on)
            n = number_of(cid)
            row = rows[cid // 1000 - 1]
            self.assertEqual(self.statuses(row)[n], "have")
        states = [i["state"] for i in job.items]
        self.assertIn("error", states)
        stopped = [i for i in job.items if i["state"] == "cancelled"]
        self.assertTrue(stopped)
        for it in stopped:
            self.assertTrue(it["result"].startswith("pass stopped"), it["result"])
        self.assertTrue(self.lock_free())

    def test_one_blip_seen_by_all_lanes_counts_once(self):
        fake = self.fake(secs=0.05)
        plans = self.many(fake, 6, chapters=3)
        rows = self.seed(*plans)
        seen = []

        def on_finish(f, cid):
            seen.append(cid)
            if len(seen) == 2:
                f.down = True
                threading.Timer(0.15, setattr, (f, "down", False)).start()
        fake.on_finish = on_finish
        with self.assertLogs("mangarr.lanes", "WARNING"):
            job, out = self.run_pass(fake, plans, rows)
        done, downloaded, imported, errors = out            # no PassStopped: one outage, held once
        self.assertGreaterEqual(errors, 1)
        self.assertLessEqual(errors, 3)
        self.assertEqual(done, 6)
        ok = [i for i in job.items if i["state"] == "done"]
        self.assertGreaterEqual(len(ok), 3)
        for it in ok:
            self.assertEqual(set(self.statuses({"id": it["series_id"]}).values()), {"have"})

    def test_unstarted_chapter_is_not_throttled(self):
        fake = self.fake(foreign={999001: X})             # someone else's download keeps Site X busy
        plans = {"S1": [entry(fake, X, 1, "S1", [1, 2])], "S2": [entry(fake, Y, 2, "S2", [1, 2])],
                 "S3": [entry(fake, Z, 3, "S3", [1, 2])]}
        rows = self.seed(*plans)
        with mock.patch.object(downloader, "QUEUED_CAP_SECS", 0.3), self.assertLogs("mangarr.downloader", "WARNING"):
            job, out = self.run_pass(fake, plans, rows)
        s1 = self.status(rows[0]["id"])
        self.assertEqual(s1, {1.0: ("wanted", downloader.UNSTARTED_REASON), 2.0: ("wanted", downloader.UNSTARTED_REASON)})
        with db.connect() as con:
            self.assertEqual(db.auto_throttled(con), set())
        self.assertEqual(set(self.statuses(rows[1]).values()), {"have"})
        self.assertEqual(set(self.statuses(rows[2]).values()), {"have"})
        self.assertEqual(fake.items, [])                    # taken back out


class LockTest(PipelineBase):
    def test_another_run_holding_the_lock_ends_the_pass_with_one_error(self):
        import fcntl
        fake = self.fake()
        plans = {"S1": [entry(fake, X, 1, "S1", [1])], "S2": [entry(fake, Y, 2, "S2", [1])]}
        rows = self.seed(*plans)
        fd = os.open(self.tmp + "/lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(downloader, "LOCK_WAIT_SECS", 0), self.assertLogs("mangarr.downloader", "ERROR"), \
             self.assertRaises(downloader.LockBusy):
            self.run_pass(fake, plans, rows, job=job)
        self.assertEqual([i["state"] for i in job.items], ["error", "cancelled"])
        self.assertEqual(job.items[1]["result"], "pass stopped: another download run holds the download lock")
        self.assertEqual(fake.kinds("enqueue"), [])


if __name__ == "__main__":
    unittest.main()
