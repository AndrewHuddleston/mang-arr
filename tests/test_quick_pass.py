"""A refresh pass that does not search every source for every series every
time (core.known_entries, resolver.resolve with `known`), and the chapters
in flight (inflight.py): what the Activity page lists chapter by chapter and
the series page shows on a chapter's row. Suwayomi is faked; nothing opens a
socket."""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_suwayomi import PassBase, entry  # noqa: E402

from mangarr import core, db, downloader, inflight, jobs, resolver, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.suwayomi import Source, SuwayomiError  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    TestClient = web = None

SITES = ("Alpha", "Bravo", "Charlie", "Delta")


class QuickBase(PassBase):
    def setUp(self):
        super().setUp()
        inflight.reset()
        self.addCleanup(inflight.reset)
        resolver._unreachable.clear()
        self.addCleanup(resolver._unreachable.clear)
        self.suwayomi = self.fake(secs=0.01)
        self.sources = [Source(n, n, "en") for n in SITES]
        entry(self.suwayomi, "Alpha", 1, "T", [1, 2, 3])
        entry(self.suwayomi, "Bravo", 2, "T", [1, 2])
        self.reads: list[int] = []
        real_manga = self.suwayomi.manga

        def manga(manga_id):
            self.reads.append(manga_id)
            return real_manga(manga_id)
        self.suwayomi.manga = manga

        def resolve(client, series, **kw):             # the real one, with the fake's sources
            return resolver.resolve(client, series, sources=self.sources, **kw)
        p = mock.patch.object(core, "resolve", resolve)
        p.start()
        self.addCleanup(p.stop)

    def add(self) -> int:
        with db.connect() as con:
            return core.add_series(con, self.suwayomi, Series(english="T"), download=False).series_id

    def refresh(self, sid, **kw):
        with db.connect() as con:
            return core.refresh_series(con, self.suwayomi, sid, download=False, **kw)

    def searched(self) -> list[str]:
        return sorted({name for _, name, _ in self.suwayomi.searches})

    def forget(self):
        self.suwayomi.searches.clear()
        self.reads.clear()


class QuickCheckTest(QuickBase):
    def test_a_pass_reads_the_known_entries_and_searches_nothing(self):
        sid = self.add()
        self.assertEqual(self.searched(), list(SITES))                  # adding a series searches every source
        self.forget()
        self.suwayomi.listing[1] = [1.0, 2.0, 3.0, 4.0]                 # a new chapter on Alpha
        out = self.refresh(sid, quick=True)
        self.assertEqual(self.suwayomi.searches, [])
        self.assertEqual(sorted(self.reads), [1, 2])                    # one read per known entry
        self.assertIn(4.0, out.plan.wanted())                           # and the new chapter is found
        self.assertEqual(self.status(sid)[4.0][0], "wanted")
        with db.connect() as con:
            self.assertEqual(sorted(r["source_name"] for r in db.sources(con, sid)), ["Alpha", "Bravo"])

    def test_refresh_on_the_series_page_searches_every_source(self):
        sid = self.add()
        self.forget()
        entry(self.suwayomi, "Charlie", 3, "T", [1, 2, 3, 4, 5])        # the series appeared on another site
        self.refresh(sid, quick=True)
        self.assertEqual(self.searched(), [])
        with db.connect() as con:
            self.assertNotIn("Charlie", [r["source_name"] for r in db.sources(con, sid)])
        self.refresh(sid)                                               # not quick
        self.assertEqual(self.searched(), list(SITES))
        with db.connect() as con:
            self.assertIn("Charlie", [r["source_name"] for r in db.sources(con, sid)])

    def test_the_full_search_comes_back_when_it_is_due(self):
        sid = self.add()
        with db.connect() as con:
            self.assertIsNotNone(core.known_entries(con, sid))
            con.execute("UPDATE series SET last_searched=? WHERE id=?", (db.ago(8), sid))
            con.commit()
            self.assertIsNone(core.known_entries(con, sid))             # 7 days are over
        self.forget()
        self.refresh(sid, quick=True)
        self.assertEqual(self.searched(), list(SITES))
        with db.connect() as con:
            self.assertGreater(db.get_series(con, sid)["last_searched"], db.ago(1))
            self.assertIsNotNone(core.known_entries(con, sid))
            settings.set_many(con, {"full_search_days": 0})             # 0: search every source every pass
            settings._cache.clear()
            self.assertIsNone(core.known_entries(con, sid))

    def test_an_entry_that_cannot_be_read_keeps_what_is_stored_and_asks_for_a_search(self):
        sid = self.add()
        self.forget()
        real = self.suwayomi.manga

        def manga(manga_id):
            if manga_id == 2:
                raise SuwayomiError("HTTP error 500")
            return real(manga_id)
        self.suwayomi.manga = manga
        out = self.refresh(sid, quick=True)
        self.assertEqual([s.name for s, _ in out.plan.unreachable], ["Bravo"])
        with db.connect() as con:
            self.assertEqual(sorted(r["source_name"] for r in db.sources(con, sid)), ["Alpha", "Bravo"])   # kept
            self.assertIsNone(db.get_series(con, sid)["last_searched"])
            self.assertIsNone(core.known_entries(con, sid))             # the next pass searches every source
        self.assertEqual(self.searched(), [])
        self.assertEqual({n: s for n, (s, _) in self.status(sid).items()}, {1.0: "wanted", 2.0: "wanted", 3.0: "wanted"})

    def test_a_series_without_entries_is_searched(self):
        with db.connect() as con:
            sid = db.upsert_series(con, Series(english="T"))
            con.execute("UPDATE series SET last_searched=? WHERE id=?", (db.now(), sid))
            con.commit()
            self.assertIsNone(core.known_entries(con, sid))

    def test_the_migration_spreads_the_first_full_searches(self):
        with db.connect() as con:
            con.execute("ALTER TABLE series DROP COLUMN last_searched")
            for k in range(1, 15):
                con.execute("INSERT INTO series (ref, title, added_at, last_resolved) VALUES (?,?,?,?)",
                            (f"manual:{k}", f"S{k}", db.now(), "2026-09-28 12:00:00"))
            con.execute("INSERT INTO series (ref, title, added_at) VALUES ('manual:x', 'never', ?)", (db.now(),))
            db._add_last_searched(con)
            db._add_last_searched(con)                                  # a second time changes nothing
            got = {r["title"]: r["last_searched"] for r in con.execute("SELECT title, last_searched FROM series")}
        self.assertIsNone(got.pop("never"))
        self.assertEqual(len(set(got.values())), 7)
        self.assertEqual((min(got.values()), max(got.values())), ("2026-09-22 12:00:00", "2026-09-28 12:00:00"))

    @unittest.skipIf(web is None, "web extras not installed")
    def test_the_pass_is_quick_and_the_setting_is_advanced(self):
        sid = self.add()
        self.forget()
        with db.connect() as con:
            rows = [dict(r) for r in db.series_rows(con)]
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(web, "client", self.suwayomi):
            web._run_pass(job, rows, "test")
        self.assertEqual(self.suwayomi.searches, [])
        self.assertEqual({n: s for n, (s, _) in self.status(sid).items()}, {1.0: "have", 2.0: "have", 3.0: "have"})
        self.assertEqual(inflight.rows(), [])                           # nothing is left in flight after a pass
        self.assertIn("full_search_days", web.ADVANCED_SETTINGS["downloading"])


class InFlightTest(QuickBase):
    def test_queued_then_downloading_then_gone(self):
        inflight.queue(7, "T", [1, 2, 3], None, "waiting for a download lane")
        self.assertEqual([(r["number"], r["state"]) for r in inflight.rows(7)],
                         [(1.0, "queued"), (2.0, "queued"), (3.0, "queued")])
        inflight.start(7, "T", [2], "Alpha", "Alpha: chapter 2")
        inflight.say(7, "Alpha", "Alpha: chapter 2, this batch 40%")
        inflight.say(7, "Bravo", "not its source")
        first = inflight.rows()[0]
        self.assertEqual((first["number"], first["state"], first["source"], first["text"]),
                         (2.0, "downloading", "Alpha", "Alpha: chapter 2, this batch 40%"))
        inflight.finish(7, [2])
        self.assertEqual(sorted(inflight.by_number(7)), [1.0, 3.0])
        inflight.queue(8, "U", [1])
        inflight.clear(7)
        self.assertEqual([(r["series_id"], r["number"]) for r in inflight.rows()], [(8, 1.0)])
        self.assertEqual(inflight.rows(7), [])
        for bad in (None, "x"):                                         # never raises into a download
            inflight.queue(bad, "T", [1])
            inflight.start(bad, "T", ["y"], "A")
            inflight.finish(bad, [object()])
            inflight.clear(bad)

    def test_nothing_is_tracked_outside_a_series(self):
        m = entry(self.suwayomi, "Alpha", 1, "T", [1, 2])
        self.assertIsNone(inflight.current())
        ok, failed, _ = downloader._download_source(self.suwayomi, 1, m.chapters, 2, "T", "Alpha", True, lambda: False,
                                                    lambda m: None, downloader.RunMemo())
        self.assertEqual((ok, failed, inflight.rows()), ([1.0, 2.0], [], []))

    def test_a_download_shows_each_chapter_as_it_goes(self):
        m = entry(self.suwayomi, "Alpha", 1, "T", [1, 2, 3])
        self.suwayomi.broken.add(m.chapters[1].id)                      # chapter 2 fails
        seen = []

        def report(text):
            seen.append({r["number"]: (r["state"], r["source"]) for r in inflight.rows(5)})
        with inflight.tracking(5, "T"):
            self.assertEqual(inflight.current(), (5, "T"))
            ok, failed, _ = downloader._download_source(self.suwayomi, 1, m.chapters, 1, "T", "Alpha", False,
                                                        lambda: False, report, downloader.RunMemo())
        self.assertIsNone(inflight.current())
        self.assertEqual((ok, failed), ([1.0, 3.0], [2.0]))
        self.assertEqual(seen[0], {1.0: ("downloading", "Alpha"), 2.0: ("queued", "Alpha"), 3.0: ("queued", "Alpha")})
        self.assertIn({3.0: ("downloading", "Alpha")}, seen)            # 1 arrived and 2 failed: both gone
        self.assertEqual(inflight.rows(5), [])

    def test_tracking_is_per_thread(self):
        got = []
        with inflight.tracking(1, "A"):
            t = threading.Thread(target=lambda: got.append(inflight.current()))
            t.start()
            t.join()
            with inflight.tracking(2, "B"):
                got.append(inflight.current())
            got.append(inflight.current())
        self.assertEqual(got, [None, (2, "B"), (1, "A")])

    def test_a_single_series_download_clears_itself(self):
        sid = self.add()
        seen = []
        with db.connect() as con:
            out = core.refresh_series(con, self.suwayomi, sid, download=True,
                                      progress=lambda m: seen.append(len(inflight.rows(sid))))
        self.assertEqual(out.downloaded, 3)
        self.assertGreater(max(seen), 0)
        self.assertEqual(inflight.rows(), [])


@unittest.skipIf(web is None, "web extras not installed")
class InFlightPagesTest(QuickBase):
    def setUp(self):
        super().setUp()
        from mangarr import health
        for p in (mock.patch.object(web, "client", self.suwayomi), mock.patch.object(web, "ui_client", self.suwayomi),
                  mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "stub")),
                  mock.patch.dict(health._cache, {"at": 0.0, "checks": []})):
            p.start()
            self.addCleanup(p.stop)
        self.suwayomi.gq = lambda q, **kw: {"downloadStatus": {"state": "STOPPED", "queue": []}}
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.sid = self.add()

    def test_the_api_the_activity_page_and_the_series_page(self):
        self.assertEqual(self.client.get("/api/v1/queue/chapters").json(), [])
        self.assertNotIn('id="chapter-queue"', self.client.get("/activity").text)
        html = self.client.get(f"/series/{self.sid}").text
        self.assertIn(f'data-series-id="{self.sid}"', html)
        self.assertNotIn("in-flight", html)
        inflight.queue(self.sid, "T", [1, 3], "Alpha", "next from Alpha")
        inflight.start(self.sid, "T", [2], "Alpha", "Alpha: chapter 2 (1 of 3 done)")
        inflight.queue(99, "Other <b>", [7], None, "waiting for a download lane")
        got = self.client.get("/api/v1/queue/chapters").json()
        self.assertEqual([(c["seriesId"], c["number"], c["state"]) for c in got],
                         [(self.sid, 2.0, "downloading"), (self.sid, 1.0, "queued"), (self.sid, 3.0, "queued"),
                          (99, 7.0, "queued")])
        mine = self.client.get("/api/v1/queue/chapters", params={"seriesId": self.sid}).json()
        self.assertEqual([c["number"] for c in mine], [2.0, 1.0, 3.0])
        html = self.client.get("/activity").text
        for needle in ("Chapters: 1 downloading, 3 queued", "Chapter 2", "Alpha: chapter 2 (1 of 3 done)",
                       ">Downloading<", ">Queued<", "Other &lt;b&gt;", f'href="/series/{self.sid}"'):
            self.assertIn(needle, html)
        html = self.client.get(f"/series/{self.sid}").text
        self.assertRegex(html, r'<tr class="episode-row wanted in-flight" data-number="2" data-status="wanted" '
                               r'data-inflight="downloading">')
        self.assertRegex(html, r'data-number="1" data-status="wanted" data-inflight="queued">')
        self.assertIn("inflight-label", html)
        self.assertIn("Alpha: chapter 2 (1 of 3 done)", html)
        # a chapter on disk is never shown as in flight
        with db.connect() as con:
            con.execute("UPDATE chapter SET status='have' WHERE series_id=? AND number=2", (self.sid,))
        html = self.client.get(f"/series/{self.sid}").text
        self.assertRegex(html, r'<tr class="episode-row have" data-number="2" data-status="have">')


if __name__ == "__main__":
    unittest.main()
