"""One series, tracked once (duplicates.py). "It's Mine" was tracked as anilist:118601 and, after Library Import
looked up its folder "It’s Mine", as a MangaDex series too, so its 159 chapters were tracked twice. A MangaDex
record's link to its AniList entry now keeps a series from being added under its other reference, and the health
check names the pairs already tracked."""
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from mangarr import core, db, duplicates, health, jobs, lists, mangadex, settings, suwayomi
from mangarr.model import Series

try:                                     # run by discover (-s tests) or as tests.test_duplicates
    from test_web_security import WebBase
except ImportError:
    from tests.test_web_security import WebBase

try:
    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    web = None

MD_UUID = "0c1e7a57-4bd4-4c0e-9d5c-6a0f3f7e8b21"
MD2_UUID = "1d2f8b68-5ce5-4d1f-8e6d-7b1f4f8e9c32"
MD3_UUID = "2e3a9c79-6df6-4e2a-9f7e-8c2a5a9fad43"


def anilist_its_mine():
    return Series(anilist_id=118601, romaji="It's Mine", english="It's Mine", country="KR", format="MANGA")


def mangadex_its_mine(uuid=MD_UUID, link=118601):
    return Series(mangadex_id=uuid, english="It’s Mine", country="KR", format="MANGA", anilist_link=link)


def md_record(links):
    return {"id": MD_UUID, "attributes": {"title": {"en": "It’s Mine"}, "altTitles": [], "links": links,
                                          "originalLanguage": "ko"}, "relationships": []}


class LinkTest(unittest.TestCase):
    def test_mangadex_record_names_its_anilist_entry(self):
        s = mangadex._to_series(md_record({"al": "118601", "mu": "its-mine"}))
        self.assertEqual((s.ref, s.anilist_id, s.anilist_link), (f"mangadex:{MD_UUID}", None, 118601))
        self.assertEqual(mangadex._to_series(md_record({"al": 118601})).anilist_link, 118601)
        for links in (None, {}, {"al": None}, {"al": ""}, {"al": "0"}, {"al": "abc"}, {"al": "²"}, {"al": "1" * 10},
                      {"al": "-5"}, ["al"]):
            self.assertIsNone(mangadex._to_series(md_record(links)).anilist_link, links)


class DbBase(unittest.TestCase):
    """A temporary database; no Suwayomi, no notifications."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for p in (mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp, "t.db")),
                  mock.patch("mangarr.config.STAGING_ROOT", os.path.join(self.tmp, "staging")),
                  mock.patch("mangarr.config.LIBRARY_ROOT", os.path.join(self.tmp, "library")),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)

    def track(self, *series):
        """Tracked as a release without this check left them (upsert_series does not ask)."""
        with db.connect() as con:
            ids = [db.upsert_series(con, s) for s in series]
            con.commit()
        return ids

    def count(self):
        with db.connect() as con:
            return con.execute("SELECT COUNT(*) FROM series").fetchone()[0]


class StoredLinkTest(DbBase):
    def test_kept_with_the_series_and_read_again_on_refresh(self):
        sid, = self.track(mangadex_its_mine())
        with db.connect() as con:
            row = db.get_series(con, sid)
            self.assertEqual((row["ref"], row["anilist_id"], row["anilist_link"]),
                             (f"mangadex:{MD_UUID}", None, 118601))
            s = db.series_to_model(row)
            self.assertEqual((s.ref, s.anilist_link), (f"mangadex:{MD_UUID}", 118601))   # never its identity
            db.upsert_series(con, mangadex_its_mine(link=None))      # MangaDex dropped the link
            self.assertIsNone(db.get_series(con, sid)["anilist_link"])


class TrackedAsTest(DbBase):
    def test_mangadex_series_linked_to_a_tracked_anilist_series(self):
        self.track(anilist_its_mine())
        with db.connect() as con:
            self.assertEqual(duplicates.refusal(con, mangadex_its_mine()), "already tracked as It's Mine")
            self.assertEqual(duplicates.refusal(con, anilist_its_mine()), "already tracked")
            self.assertIsNone(duplicates.refusal(con, mangadex_its_mine(link=None)))
            self.assertIsNone(duplicates.refusal(con, mangadex_its_mine(link=5)))
            self.assertIsNone(duplicates.refusal(con, Series(anilist_id=5, english="Other")))

    def test_anilist_series_whose_mangadex_record_is_tracked(self):
        self.track(mangadex_its_mine())
        with db.connect() as con:
            self.assertEqual(duplicates.refusal(con, anilist_its_mine()), "already tracked as It’s Mine")
            # another MangaDex record of it (MangaDex has some twice) links to the same AniList id
            self.assertEqual(duplicates.refusal(con, mangadex_its_mine(MD2_UUID)), "already tracked as It’s Mine")
            with self.assertRaises(duplicates.AlreadyTracked) as cm:
                duplicates.check_new(con, anilist_its_mine())
            self.assertEqual(str(cm.exception), "It's Mine: already tracked as It’s Mine")
            duplicates.check_new(con, mangadex_its_mine())            # its own reference: not a second series

    def test_add_is_refused_before_any_source_search(self):
        # two adds queued under both references before either ran: the second one stops at once
        self.track(anilist_its_mine())
        with db.connect() as con, mock.patch.object(core, "resolve", side_effect=AssertionError("searched")), \
                self.assertRaises(duplicates.AlreadyTracked) as cm:
            core.add_series(con, mock.Mock(), mangadex_its_mine())
        self.assertEqual(str(cm.exception), "It’s Mine: already tracked as It's Mine")
        self.assertEqual(self.count(), 1)

    def test_import_list_counts_it_as_tracked(self):
        self.track(mangadex_its_mine())
        submitted = []
        fetched = [anilist_its_mine(), Series(anilist_id=7, english="New", format="MANGA")]
        with db.connect() as con:
            lid = lists.add_list(con, "test", "url_text", {"url": "http://x"})
            row = lists.get_list(con, lid)
            with mock.patch.dict(lists.FETCHERS, {"url_text": lambda p: (list(fetched), [])}):
                msg = lists.sync(con, row, lambda s, d, m: submitted.append(s.ref))
        self.assertEqual(submitted, ["anilist:7"])
        self.assertEqual(msg, "2 fetched, 1 added, 1 already tracked")


class AdoptTest(DbBase):
    @staticmethod
    def items():
        return [core.AdoptItem("Weeb Central", "It’s Mine", "/staging/Weeb Central/It’s Mine", {1.0: "/staging/a.cbz"},
                               [], series=mangadex_its_mine(), manga_id=41)]

    def test_scan_proposes_the_tracked_series(self):
        self.track(anilist_its_mine())
        items = self.items()
        with db.connect() as con, self.assertLogs("mangarr.duplicates", "INFO") as logs:
            duplicates.mark_tracked(con, items)
        self.assertTrue(items[0].tracked)
        self.assertEqual((items[0].series.ref, items[0].series.title), ("anilist:118601", "It's Mine"))
        self.assertIn(f"It’s Mine is mangadex:{MD_UUID}, already tracked as It's Mine (anilist:118601)",
                      logs.output[0])

    @unittest.skipIf(web is None, "web extras not installed")
    def test_import_page_scan(self):
        self.track(anilist_its_mine())
        items = self.items()
        with mock.patch.object(core, "plan_adopt", lambda *a, **kw: items), \
                mock.patch.dict(web._adopt_scan, {"items": None, "gen": 0}), self.assertLogs("mangarr.duplicates"):
            msg = web._job_adopt_scan(jobs.Job(1, "adopt-scan", "staging folders"))
        self.assertEqual(msg, "1 folders, 0 identified, 0 need a choice")
        self.assertTrue(items[0].tracked)
        self.assertEqual(items[0].series.ref, "anilist:118601")

    def test_adopting_it_all_the_same_adds_the_folder_to_the_tracked_series(self):
        # the CLI adopts every identified folder; the Import page a candidate chosen for a folder
        sid, = self.track(anilist_its_mine())
        with db.connect() as con:
            self.assertEqual(core.apply_adopt(con, self.items()), ([sid], 1))
            self.assertEqual([r["manga_id"] for r in db.sources(con, sid)], [41])
            self.assertEqual([c["number"] for c in db.chapters(con, sid)], [1.0])
        self.assertEqual(self.count(), 1)


class HealthTest(DbBase):
    def test_pairs_tracked_already_are_named(self):
        a, m, m2, _, _ = self.track(anilist_its_mine(), mangadex_its_mine(), mangadex_its_mine(MD2_UUID),
                                    Series(anilist_id=5, english="Other"), mangadex_its_mine(MD3_UUID, link=None))
        with db.connect() as con:
            self.assertEqual([(x["id"], y["id"]) for x, y in duplicates.pairs(con)], [(a, m), (a, m2)])
        c = health.duplicate_series()
        self.assertEqual((c.level, c.name), ("warning", "Duplicate series"))
        self.assertEqual(c.detail, "2 series tracked twice, as the same AniList series (MangaDex links its record "
                                   f"to it): It's Mine (anilist:118601) and It’s Mine (mangadex:{MD_UUID}); It's "
                                   f"Mine (anilist:118601) and It’s Mine (mangadex:{MD2_UUID}). Both of a pair "
                                   "download and link the same chapters; delete the second one with its library files "
                                   "(the first keeps its own).")

    def test_two_mangadex_series_and_many_pairs(self):
        self.track(mangadex_its_mine(), mangadex_its_mine(MD2_UUID))
        with db.connect() as con:
            self.assertEqual([(x["ref"], y["ref"]) for x, y in duplicates.pairs(con)],
                             [(f"mangadex:{MD_UUID}", f"mangadex:{MD2_UUID}")])      # the older one first
        self.track(*[Series(anilist_id=i, english=f"S{i}") for i in range(10, 17)],
                   *[Series(mangadex_id=f"{i:08d}-0000-4000-8000-000000000000", english=f"S{i}", anilist_link=i)
                     for i in range(10, 17)])
        detail = health.duplicate_series().detail
        self.assertTrue(detail.startswith("8 series tracked twice"), detail)
        self.assertIn("; and 3 more. Both of a pair", detail)

    def test_nothing_tracked_twice(self):
        self.track(anilist_its_mine(), mangadex_its_mine(link=None))
        self.assertIsNone(health.duplicate_series())

    def test_in_the_health_checks(self):
        self.track(anilist_its_mine(), mangadex_its_mine())
        client = mock.Mock()
        client.gq.side_effect = suwayomi.SuwayomiError("offline in tests")
        with mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "x")), \
                mock.patch.object(health, "_alert", lambda out: []), \
                mock.patch("mangarr.komga.configured", return_value=False), \
                mock.patch.dict(health._cache, {"at": 0.0, "checks": []}), self.assertLogs("mangarr.health", "WARNING"):
            checks = health._compute(client)
        self.assertIn("Duplicate series", [c.name for c in checks if c.level == "warning"])


class CleanupTest(DbBase):
    def test_deleting_the_second_leaves_the_shared_entry_in_suwayomis_library(self):
        a, m = self.track(anilist_its_mine(), mangadex_its_mine())
        with db.connect() as con:
            for sid, manga_id in ((a, 41), (m, 41), (m, 42)):
                con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level,"
                            " author_level, chapter_count, max_chapter, seen_at) VALUES (?,?,'S','T',0,0,1,1,'2020')",
                            (sid, manga_id))
            con.commit()
            client = mock.Mock()
            with self.assertLogs("mangarr.core", "INFO") as logs:
                core.delete_series(con, client, m)
            self.assertEqual([r["manga_id"] for r in db.sources(con, a)], [41])
        client.set_in_library.assert_called_once_with(42, False, retries=1, timeout=10)
        self.assertIn("It’s Mine: S entry left in Suwayomi's library: another series uses it", "\n".join(logs.output))


class WebTest(WebBase):
    def setUp(self):
        super().setUp()
        with db.connect() as con:
            self.sid = db.upsert_series(con, anilist_its_mine())
            con.commit()

    def test_add_page_links_the_mangadex_candidate_to_the_tracked_series(self):
        with mock.patch("mangarr.metadata.lookup", lambda term: (None, [mangadex_its_mine()])):
            html = self.client.get("/add", params={"term": "It’s Mine"}).text
        self.assertIn(f'href="/series/{self.sid}" aria-label="Open It’s Mine"', html)
        self.assertIn("Already tracked", html)
        self.assertNotIn("data-add", html.split('class="search-results"', 1)[1])

    def test_add_form_and_api_refuse_it(self):
        with mock.patch("mangarr.metadata.by_ref", lambda ref: mangadex_its_mine()), \
                mock.patch.object(web.runner, "submit", side_effect=AssertionError("queued")):
            r = self.client.post("/add", data={"ref": f"mangadex:{MD_UUID}"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn(urllib.parse.quote("It’s Mine: already tracked as It's Mine"), r.headers["location"])
            r = self.client.post("/api/v1/series", json={"ref": f"mangadex:{MD_UUID}"}, headers={"X-Api-Key": self.key})
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "It’s Mine: already tracked as It's Mine"))
        from mangarr.web import lists_routes
        self.assertEqual(lists_routes._submit_add(mangadex_its_mine(), True, True, "L"), "already tracked as It's Mine")
