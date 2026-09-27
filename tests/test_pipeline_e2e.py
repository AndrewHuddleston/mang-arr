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

from fake_suwayomi import PassBase, add_series, chapter_id, entry, number_of, resolver_for  # noqa: E402

from mangarr import core, db, downloader, jobs, lanes, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.suwayomi import SuwayomiUnreachable  # noqa: E402

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

    def run_pass(self, fake, plans, rows, job=None, in_order=True, resolve=None):
        job = job or jobs.Job(1, "refresh-all", "all")
        with db.connect() as con:
            settings.set_many(con, {"download_in_order": in_order})
        settings._cache.clear()
        with mock.patch.object(web, "client", fake), \
             mock.patch.object(core, "resolve", resolve or resolver_for(fake, plans)):
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
        self.assertIn(f"{COMICK}: not fetched page by page (its page list has a URL that is not a Suwayomi page "
                      "path); the normal download failed instantly on every try", st[2.0][1])
        self.assertNotIn("no working pages", st[2.0][1])               # it does have them: page by page did not run
        self.assertTrue(st[3.0][1].startswith("waiting for chapter 2"))


class CompleteSeriesTest(PipelineBase):
    """A scheduled pass skips a complete finished series checked lately,
    after one status question for all of them, unless its provider says it
    goes on; a manual refresh of it always runs."""

    def test_skipped_unless_it_goes_on(self):
        fake = self.fake()
        plans = {"Done": [entry(fake, X, 1, "Done", [1, 2])], "Back": [entry(fake, Y, 2, "Back", [1, 2])],
                 "Going": [entry(fake, Z, 3, "Going", [1])]}
        with db.connect() as con:
            for t, status in (("Done", "FINISHED"), ("Back", "FINISHED"), ("Going", "RELEASING")):
                sid = db.upsert_series(con, Series(english=t, status=status, chapters=2))
                if t != "Going":
                    for n in (1.0, 2.0):
                        con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?, ?,"
                                    " 'have', ?)", (sid, n, db.now()))
            con.execute("UPDATE series SET last_resolved=?", (db.now(),))
            con.commit()
            ids = {r["title"]: r["id"] for r in db.series_rows(con)}
        asked = []

        def statuses(refs):
            asked.append(sorted(refs))
            return {"manual:Done": ("FINISHED", 2), "manual:Back": ("RELEASING", None)}
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolver_for(fake, plans)), \
                mock.patch.object(web.metadata, "statuses", statuses), self.assertLogs("mangarr.web.app", "INFO") as cm:
            msg = web._job_refresh_all(job)
        self.assertEqual(asked, [["manual:Back", "manual:Done"]])            # one question for all of them
        self.assertEqual([(i["title"], i["state"]) for i in job.items],
                         [("Going", "done"), ("Back", "done"), ("Done", "skipped")])
        self.assertEqual(job.items[2]["result"], "skipped: complete and finished; next check in about 7 day(s)")
        self.assertIn("Back: now RELEASING", "\n".join(cm.output))
        self.assertEqual(fake.kinds("enqueue", X), [])                       # Done's source never asked
        self.assertEqual(msg, "2 series, 1 downloaded, 1 imported, 0 errors, 1 complete finished series skipped")
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)) as _, \
                mock.patch.object(web, "client", fake):
            out = web._job_refresh(ids["Done"], download=False)(jobs.Job(2, "refresh", "Done"))
        self.assertEqual(out, "2 listed, 0 downloaded, 0 failed, 0 imported")   # a manual refresh always runs

    def test_the_status_check_failing_skips_them_as_planned(self):
        fake = self.fake()
        with db.connect() as con:
            db.upsert_series(con, Series(english="Done", status="CANCELLED"))
            con.execute("UPDATE series SET last_resolved=?", (db.now(),))
            con.commit()

        def statuses(refs):
            raise RuntimeError("AniList unreachable")
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(web, "client", fake), mock.patch.object(web.metadata, "statuses", statuses), \
                self.assertLogs("mangarr.web.app", "WARNING"):
            web._job_refresh_all(job)
        self.assertEqual([(i["state"], i["result"]) for i in job.items],
                         [("skipped", "skipped: complete and cancelled; next check in about 7 day(s)")])


class DueFirstTest(PipelineBase):
    def test_a_series_whose_first_missing_chapter_waits_for_its_retry_still_goes_first(self):
        # in order, ch 1 failed and waits for its next try; the pass still fetches 2 and 3, so the
        # series has chapters due and goes before a continuing series with nothing due
        fake = self.fake()
        plans = {"A continuing": [entry(fake, Y, 1, "A continuing", [1])],
                 "B held": [entry(fake, X, 2, "B held", [1, 2, 3])]}
        with db.connect() as con:
            a = db.upsert_series(con, Series(english="A continuing", status="RELEASING"))
            b = db.upsert_series(con, Series(english="B held", status="FINISHED"))
            con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?, 1, 'have', ?)",
                        (a, db.now()))
            con.execute("INSERT INTO chapter (series_id, number, status, next_try, reason, updated_at) VALUES"
                        " (?, 1, 'failed', '2999-01-01 00:00:00', 'broken', ?)", (b, db.now()))
            for n in (2, 3):
                con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?, ?, 'wanted', ?)",
                            (b, n, db.now()))
            con.commit()
            settings.set_many(con, {"download_in_order": True})
        settings._cache.clear()
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolver_for(fake, plans)), \
                self.assertLogs("mangarr.web.app", "INFO") as cm:
            web._job_refresh_all(job)
        self.assertEqual([i["title"] for i in job.items], ["B held", "A continuing"])
        self.assertIn("refresh pass: 2 series (1 with chapters due first, then 1 continuing)", "\n".join(cm.output))
        self.assertEqual({n: st for n, (st, _) in self.status(b).items()}, {1.0: "failed", 2.0: "have", 3.0: "have"})


class ChapterSearchTest(PassBase):
    """A chapter's Search button (core.download_chapter without an entry)
    asks the sources in the order a resolve ranks them."""

    def search(self, title, forget_choice=False):
        fake = self.fake(page_warm={COMICK})
        plans = {title: [entry(fake, "Weeb Central", 1, title, [1, 2, 3, 4, 6]), entry(fake, COMICK, 2, title, [5]),
                         entry(fake, "MangaDex", 3, title, [5])]}
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)), db.connect() as con, \
             self.assertLogs("mangarr", "INFO"):
            sid = add_series(con, fake, title)
            if forget_choice:                           # no chapter row choice: the tiers alone
                con.execute("UPDATE chapter SET manga_id=NULL WHERE series_id=?", (sid,))
                con.commit()
            msg = core.download_chapter(con, fake, sid, 5.0)
        return fake, msg

    def test_the_chapters_chosen_source_first_and_page_by_page_last(self):
        for title, forget in (("T", False), ("U", True)):
            with self.subTest(forget_choice=forget):
                fake, msg = self.search(title, forget)
                self.assertEqual(msg, "chapter 5 downloaded from MangaDex and linked")
                self.assertEqual(fake.kinds("page"), [])        # Comick (sorted first by name) never asked
                self.assertEqual({e[2] for e in fake.kinds("enqueue")}, {"MangaDex"})


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
             mock.patch.object(web, "plan_pass", lambda r, due=None: (rows, [])), \
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
        # nothing arrived: reported like a failure, so the failed-downloads notification names it
        self.assertEqual((job.items[0]["state"], job.items[0]["result"]),
                         ("failed", "2 not started (Suwayomi's download queue was busy with other downloads)"))
        with db.connect() as con:
            self.assertEqual(db.auto_throttled(con), set())
        self.assertEqual(set(self.statuses(rows[1]).values()), {"have"})
        self.assertEqual(set(self.statuses(rows[2]).values()), {"have"})
        self.assertEqual(fake.items, [])                    # taken back out

    def test_a_chapter_not_started_goes_to_another_source(self):
        fake = self.fake(foreign={999001: X})             # Site X stays busy with someone else's download
        plans = {f"S{k}": [entry(fake, X, k, f"S{k}", [1, 2, 3]), entry(fake, Y, 10 + k, f"S{k}", [1, 2])]
                 for k in (1, 2, 3)}                      # X ranks first: it lists more
        rows = self.seed(*plans)
        with mock.patch.object(downloader, "QUEUED_CAP_SECS", 0.3), self.assertLogs("mangarr.downloader", "WARNING"):
            job, out = self.run_pass(fake, plans, rows, in_order=False)
        self.assertEqual({e[3] for e in fake.kinds("enqueue", X)}, {1})   # the others did not queue on X at all
        for r in rows:
            st = self.status(r["id"])
            self.assertEqual({n: s for n, (s, _) in st.items()}, {1.0: "have", 2.0: "have", 3.0: "wanted"})
        first, *others = rows
        self.assertEqual(self.status(first["id"])[3.0][1], downloader.UNSTARTED_REASON)
        for r in others:                                    # never queued: the reason says just that
            self.assertEqual(self.status(r["id"])[3.0][1],
                             f"not attempted: {X} is busy with other downloads; tried again next pass")
        self.assertEqual(out, (3, 6, 6, 0))
        self.assertEqual((job.items[0]["state"], job.items[0]["result"]),
                         ("done", "2 downloaded; 1 not started (Suwayomi's download queue was busy with other "
                                  "downloads)"))
        for it in job.items[1:]:
            self.assertEqual((it["state"], it["result"]),
                             ("done", f"2 downloaded; 1 not attempted ({X} busy with other downloads; tried again "
                                      "next pass)"))
        with db.connect() as con:
            self.assertEqual(db.auto_throttled(con), set())
        self.assertEqual(fake.items, [])


class EndTest(PipelineBase):
    """However a pass ends, it ends only once every lane has: nothing of it
    writes afterwards, and the download lock is held until then."""

    def test_a_pass_the_resolve_step_stopped_waits_for_a_lane_still_importing(self):
        fake = self.fake()
        plans = {"S1": [entry(fake, X, 1, "S1", [1])]}
        rows = self.seed("S1", "S2", "S3")
        importing, go, seen = threading.Event(), threading.Event(), []
        real_import, found = core.import_series, resolver_for(fake, plans)

        def release():                                  # a slow import on NAS storage, then done
            seen.append((self.lock_free(), [t.name for t in threading.enumerate()
                                            if t.name.startswith("mangarr-lane-")]))
            go.set()

        def slow_import(con, sid, client=None):
            if threading.current_thread().name.startswith("mangarr-lane-") and not importing.is_set():
                importing.set()
                threading.Timer(1.0, release).start()
                go.wait(10)
            return real_import(con, sid, client)

        def resolve(client, series, **kw):
            if series.title == "S1":
                return found(client, series, **kw)
            importing.wait(10)                          # Suwayomi goes away while S1's lane imports
            raise SuwayomiUnreachable("Suwayomi at http://fake unreachable: connection refused")
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(core, "import_series", slow_import), mock.patch.object(lanes, "SHUTDOWN_JOIN_SECS", 0.1), \
             self.assertLogs(level="WARNING") as cm, self.assertRaises(web.PassStopped) as stop:
            self.run_pass(fake, plans, rows, job=job, resolve=resolve)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("mangarr-lane-")], [])
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0][0])                    # the lock was still the pass's while the lane imported
        self.assertTrue(seen[0][1])
        self.assertRegex("\n".join(cm.output), r"mangarr-lane-\d is still busy 0 s after the pass ended \(an import "
                                                r"is not cut short\); waiting for it")
        self.assertEqual((job.items[0]["state"], job.items[0]["result"]), ("done", "1 downloaded"))
        self.assertEqual(self.statuses(rows[0]), {1.0: "have"})
        self.assertEqual(stop.exception.counts, (3, 1, 1, 2))   # S1's chapter is in the pass totals
        self.assertTrue(str(stop.exception).startswith("pass stopped after 3 of 3 series: Suwayomi is not "
                                                       "answering (Suwayomi at http://fake unreachable"),
                        str(stop.exception))
        self.assertEqual((job.lanes, job.active_series_ids), ([], frozenset()))
        self.assertTrue(self.lock_free())

    def test_lanes_stopping_after_every_series_was_checked(self):
        with db.connect() as con:
            settings.set_many(con, {"download_lanes": 1})
        fake = self.fake(secs=0.05)
        plans = {f"S{k}": [entry(fake, f"Site {k}", k, f"S{k}", [1, 2, 3])] for k in range(1, 7)}
        rows = self.seed(*plans)
        job = jobs.Job(1, "refresh-all", "all")
        seen = []

        def on_finish(f, cid):                          # down for good, once the resolve step is done
            seen.append(cid)
            if len(seen) == 2:
                deadline = time.perf_counter() + 10
                while any(i["state"] == "queued" or i["result"] == "checking sources" for i in job.items) \
                        and time.perf_counter() < deadline:
                    threading.Event().wait(0.002)
                f.set_down()
        fake.on_finish = on_finish
        with self.assertLogs(level="WARNING"), self.assertRaises(web.PassStopped) as cm:
            self.run_pass(fake, plans, rows, job=job)
        cut = [i for i in job.items if i["state"] == "cancelled"]
        self.assertGreaterEqual(len(cut), 3)
        for it in cut:
            self.assertEqual(it["result"], "pass stopped: Suwayomi is not answering")
        self.assertTrue(str(cm.exception).startswith(f"pass stopped after 6 of 6 series, {len(cut)} not "
                                                     "downloaded: Suwayomi is not answering ("), str(cm.exception))


class StartTest(PipelineBase):
    def refused(self, which):
        real = threading.Thread.start

        def start(t):
            if which(t.name):
                raise RuntimeError("can't start new thread")
            return real(t)
        fake = self.fake()
        plans = {"S1": [entry(fake, X, 1, "S1", [1, 2])], "S2": [entry(fake, Y, 2, "S2", [1, 2])]}
        rows = self.seed(*plans)
        job, out = jobs.Job(1, "refresh-all", "all"), []
        with mock.patch.object(threading.Thread, "start", start), self.assertLogs(level="WARNING") as cm:
            th = threading.Thread(target=lambda: out.append(self.run_pass(fake, plans, rows, job=job)[1]),
                                  daemon=True)
            th.start()
            th.join(20)
            if th.is_alive():                           # do not leave it running
                job.cancel = True
                th.join(10)
                self.fail("the pass did not end")
        self.assertTrue(self.lock_free())
        return fake, rows, job, out[0], "\n".join(cm.output)

    def test_the_pass_goes_on_with_the_lanes_it_got(self):
        fake, rows, job, out, logs = self.refused(lambda name: name == "mangarr-lane-2")
        self.assertIn("could start only 1 of 3 download lane(s)", logs)
        self.assertEqual(out, (2, 4, 4, 0))
        self.assertEqual([i["state"] for i in job.items], ["done", "done"])

    def test_no_lane_at_all(self):
        fake, rows, job, out, logs = self.refused(lambda name: name.startswith("mangarr-lane-"))
        self.assertEqual(out, (2, 0, 0, 2))
        self.assertEqual([(i["state"], i["result"]) for i in job.items],
                         [("error", "RuntimeError: can't start new thread")] * 2)
        self.assertEqual(fake.kinds("enqueue"), [])


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
