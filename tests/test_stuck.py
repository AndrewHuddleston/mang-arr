"""Series stuck behind a chapter (stuck.py): finding them in what the passes
left in the database, the evidence kept for the verdict, Skip it / Un-skip /
Keep waiting, the automatic skip (off by default, HIGH confidence only), the
background MangaDex lookups, the series page's note, the Wanted page's Stuck
filter, the Settings' Download Order group and the API. MangaDex answers are
the real cases' trimmed copies from test_verdict.py; nothing here reaches
the network, Suwayomi (a fake) or a notifier."""
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_suwayomi import PassBase, chapter_id, entry, resolver_for  # noqa: E402
from test_verdict import (  # noqa: E402
    DANGERS,
    DANGERS_MD,
    FALSE_POSITIVES,
    FREEDOM,
    NOZAKI,
    REVIEW_FALSE_POSITIVES,
    WRONG,
)

from mangarr import config, core, db, downloader, jobs, library, mangadex, resolver, settings, stuck  # noqa: E402
from mangarr.mangadex import ChapterList  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.verdict import HIGH  # noqa: E402

try:
    from fastapi.testclient import TestClient
    from test_web_security import WebBase

    from mangarr.web import app as web
    from mangarr.web import views
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = web = views = None
    WebBase = unittest.TestCase

WC, MN = "Weeb Central", "Manganato"
GONE = (f"{MN}: the source has no working pages for this chapter (failed instantly on every try) (no other source "
        "has this chapter)")
SINCE = "2026-09-26 10:00:00"


def add(con, sid, number, status, name=None, source=WC, pages=None, reason=None, tries=0, failed_since=None,
        path=None):
    con.execute("INSERT OR REPLACE INTO chapter (series_id, number, status, name, source_name, pages, reason, tries,"
                " failed_since, library_path, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, number, status, name, source, pages, reason, tries, failed_since, path, db.now()))


def names_of(con, sid, number, names, urls=None, failed_on=None):
    """The stuck row of a blocker as the passes leave it: the sites listing it (names) and, unless said
    otherwise, a download run that failed it on every one of them."""
    failed_on = list(names) if failed_on is None else failed_on
    con.execute("INSERT INTO stuck (series_id, number, names, urls, failed_on, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(series_id, number) DO UPDATE SET names=excluded.names, urls=excluded.urls,"
                " failed_on=excluded.failed_on", (sid, number, json.dumps(names), json.dumps(urls or {}),
                                                  json.dumps(failed_on), db.now()))


def seed_dangers(con, waiting=range(40, 60)) -> int:
    """The Dangers in My Heart, stuck behind 7.2 (only Manganato lists it, its images are gone)."""
    sid = db.upsert_series(con, DANGERS)
    for n in range(1, 40):
        add(con, sid, n, "have", pages=11)
    add(con, sid, 7.5, "have", "Twitter Extra", pages=2)
    add(con, sid, 7.2, "failed", "Chapter 7.2", MN, reason=GONE, tries=2, failed_since=SINCE)
    for n in waiting:
        add(con, sid, n, "wanted", reason=downloader.waiting_reason(7.2))
    names_of(con, sid, 7.2, {MN: "Chapter 7.2"}, {MN: "https://manganato.example/manga/ch-7.2"})
    con.commit()
    return sid


def seed_freedom(con, first="Spin-off 1") -> int:
    """Dreaming Freedom (finished at 171), stuck behind its first spin-off 171.01."""
    sid = db.upsert_series(con, FREEDOM)
    for n in range(1, 172):
        add(con, sid, n, "have", pages=20 + n % 5)
    add(con, sid, 171.01, "failed", first, MN, reason=GONE, tries=1, failed_since=SINCE)
    for i in range(2, 15):
        add(con, sid, 171 + i / 100, "wanted", f"Spin-off {i}" if i < 14 else "Afterword", MN,
            reason=downloader.waiting_reason(171.01))
    names_of(con, sid, 171.01, {MN: first})
    con.commit()
    return sid


def cache_md(series: Series, number: float, md) -> None:
    """MangaDex's answer for this series and chapter, as a lookup leaves it."""
    mangadex._cache.put(("near", series.ref, math.floor(number)), md, 3600)


def make_cbz(path: str, pages: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(pages):
            z.writestr(f"{i:03d}.jpg", os.urandom(600))
        z.writestr("ComicInfo.xml", "<ComicInfo/>")
    return path


class _Offline:
    """Stand-ins that keep a test off the network and off MangaDex: a fresh
    MangaDex cache, a Fetcher of the test's own (not started), and a MangaDex
    lookup that answers only from self.md."""

    def offline(self):
        self.md: dict[str, ChapterList] = {}          # series ref -> MangaDex's answer
        self.lookups: list = []

        def near(s, number):
            self.lookups.append((s.ref, number))
            if s.ref not in self.md:
                raise RuntimeError("MangaDex is not reachable in tests")
            return self.md[s.ref]
        self.fetcher = stuck.Fetcher()
        return [mock.patch.object(mangadex, "_cache", mangadex._Cache(mangadex.CACHE_SIZE)),
                mock.patch.object(mangadex, "_chapters_near", near),
                mock.patch.object(mangadex, "_request", side_effect=AssertionError("a test asked MangaDex")),
                mock.patch.object(stuck, "fetcher", self.fetcher)]


class StuckBase(unittest.TestCase, _Offline):
    """A temporary database and library, strict order on (the default)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.lib = os.path.join(self.tmp, "library")
        os.makedirs(self.lib)
        for p in [mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp, "t.db")),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.lib),
                  mock.patch("mangarr.config.STAGING_ROOT", os.path.join(self.tmp, "staging")),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test tried to reach the network")),
                  *self.offline()]:
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)

    def set(self, **values):
        with db.connect() as con:
            settings.set_many(con, values)
        settings._cache.clear()

    def chapter(self, sid, number):
        with db.connect() as con:
            return dict(con.execute("SELECT * FROM chapter WHERE series_id=? AND number=?", (sid, number)).fetchone())

    @staticmethod
    def ripe(con, sid, number, days=2):
        """Passes have judged the blocker one the automatic skip may act on for `days` (stuck.HIGH_DAYS: 1)."""
        con.execute("UPDATE stuck SET high_since=? WHERE series_id=? AND number=?", (db.ago(days), sid, number))
        con.commit()

    def events(self, sid):
        with db.connect() as con:
            return [(e["kind"], e["message"]) for e in con.execute("SELECT * FROM event WHERE series_id=? ORDER BY id",
                                                                    (sid,))]


# -- the database: when failures began, page counts -----------------------------

class FailedSinceTest(StuckBase):
    def test_the_first_failure_of_a_run_is_kept_until_the_chapter_is_wanted_or_on_disk(self):
        clock = iter(f"2026-09-2{d} 12:00:00" for d in range(1, 10))
        with db.connect() as con, mock.patch.object(db, "now", lambda: next(clock)):
            def row():
                return con.execute("SELECT failed_since, tries FROM chapter WHERE series_id=? AND number=1",
                                   (sid,)).fetchone()
            sid = db.upsert_series(con, Series(english="S"))
            add(con, sid, 1, "wanted")
            db.set_status(con, sid, 1, "failed", "boom")
            first = row()["failed_since"]
            con.execute("UPDATE chapter SET status='wanted' WHERE series_id=?", (sid,))   # a resolve (tries kept)
            db.set_status(con, sid, 1, "failed", "boom")
            self.assertEqual(tuple(row()), (first, 2))
            db.set_status(con, sid, 1, "wanted")                               # un-skipped, a Search: a new run
            self.assertIsNone(row()["failed_since"])
            db.set_status(con, sid, 1, "failed", "boom")
            self.assertNotEqual(row()["failed_since"], first)
            db.set_have(con, sid, 1, None, None)
            self.assertIsNone(row()["failed_since"])
        self.assertEqual(first, "2026-09-23 12:00:00")

    def test_migration_15_takes_it_from_the_first_failure_in_the_history(self):
        path = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        db.migrate(con, 14)                                                # page_probe, before the stuck table
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name='stuck'").fetchone())
        con.execute("INSERT INTO series (id, ref, title, added_at) VALUES (1, 'anilist:1', 'S', '2026-01-01')")
        for n, updated in ((7.2, "2026-09-27 08:00:00"), (8.0, "2026-09-27 09:00:00"), (9.0, "2026-09-27 10:00:00")):
            con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (1, ?, 'failed', ?)",
                        (n, updated))
        con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (1, 10, 'wanted', 'x')")
        for at, kind, msg in (("2026-09-20 00:00:00", "downloaded", "0 chapter(s) downloaded, 1 failed: ch 17.2: x"),
                              ("2026-09-25 00:00:00", "downloaded", "0 chapter(s) downloaded, 2 failed: ch 5: a; "
                                                                    "ch 7.2: gone"),
                              ("2026-09-26 00:00:00", "downloaded", "0 chapter(s) downloaded, 1 failed: ch 7.2: gone"),
                              ("2026-09-24 00:00:00", "failed", "chapter 8: Manganato: does not list it"),
                              ("2026-09-23 00:00:00", "resolved", "chapter 8: not a failure")):
            con.execute("INSERT INTO event (at, series_id, kind, message) VALUES (?, 1, ?, ?)", (at, kind, msg))
        con.commit()
        db.migrate(con)
        got = {r["number"]: r["failed_since"] for r in con.execute("SELECT number, failed_since FROM chapter")}
        self.assertEqual(got, {7.2: "2026-09-25 00:00:00", 8.0: "2026-09-24 00:00:00", 9.0: "2026-09-27 10:00:00",
                               10.0: None})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM stuck").fetchone()[0], 0)

    def test_migration_16_gives_what_an_older_version_left_its_grace(self):
        # an 'unavailable' chapter an older version made so after one resolve that missed it gets its grace back
        # (it holds the chapters after it again), an old one none; a blocker's listing comes from its stuck row
        path = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        db.migrate(con, 15)
        recent, old, resolved = db.ago(1), db.ago(30), db.ago(0.5)
        con.execute("INSERT INTO series (id, ref, title, added_at, last_resolved) VALUES (1, 'manual:S', 'S', "
                    "'2026-01-01', ?)", (resolved,))
        for n, status, source, updated in ((3.5, "unavailable", MN, recent), (4.5, "unavailable", MN, old),
                                           (7.2, "failed", MN, recent), (8, "wanted", WC, recent), (9, "have", WC, old)):
            con.execute("INSERT INTO chapter (series_id, number, status, name, source_name, updated_at) VALUES "
                        "(1, ?, ?, ?, ?, ?)", (n, status, f"Chapter {n:g}", source, updated))
        con.execute("INSERT INTO stuck (series_id, number, names, seen, updated_at) VALUES (1, 7.2, ?, ?, ?)",
                    (json.dumps({MN: "Chapter 7.2", "Bato": "Side Story"}), json.dumps({"Bato": recent}), recent))
        con.commit()
        db.migrate(con)
        rows = {r["number"]: r for r in con.execute("SELECT * FROM chapter")}
        self.assertEqual({n: (r["unlisted"], r["missed"], r["listed_at"]) for n, r in rows.items()},
                         {3.5: (1, 1, recent), 4.5: (1, db.LISTING_GRACE_RESOLVES, old), 7.2: (0, 0, resolved),
                          8.0: (0, 0, resolved), 9.0: (0, 0, None)})
        self.assertEqual([db.past_grace(rows[n]) for n in (3.5, 4.5)], [False, True])
        self.assertTrue(db.holds_in_order(rows[3.5], listed=False))
        self.assertEqual(db.listed_by(rows[7.2]), {MN: [resolved, "Chapter 7.2"], "Bato": [recent, "Side Story"]})
        self.assertEqual(db.listed_by(rows[8]), {WC: [resolved, "Chapter 8"]})
        self.assertIsNone(rows[9]["listed"])
        self.assertIsNone(con.execute("SELECT high_since FROM stuck").fetchone()[0])


class PageCountTest(StuckBase):
    def test_counts_the_images_from_the_directory_alone(self):
        good = make_cbz(os.path.join(self.lib, "S", "Chapter 1.cbz"), 5)
        self.assertEqual(library.page_count(good), 5)
        junk = os.path.join(self.lib, "S", "junk.cbz")
        with open(junk, "wb") as f:
            f.write(b"PK" + b"x" * 3000)
        tiny = os.path.join(self.lib, "S", "tiny.cbz")
        with open(tiny, "wb") as f:
            f.write(b"PK")
        link = os.path.join(self.lib, "S", "link.cbz")
        os.symlink(good, link)
        for p in (junk, tiny, link, os.path.join(self.lib, "S"), os.path.join(self.lib, "nope.cbz")):
            with self.subTest(p):
                self.assertIsNone(library.page_count(p))

    def test_an_import_keeps_the_page_count(self):
        staging = os.path.join(self.tmp, "staging", "Src (EN)", "Title")
        p = make_cbz(os.path.join(staging, "Chapter 3.cbz"), 7)
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        with db.connect() as con, mock.patch.object(core.komga, "scan", lambda: False):
            sid = db.upsert_series(con, Series(anilist_id=1, english="Title"))
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,"
                        " chapter_count, max_chapter, folder, seen_at) VALUES (?,7,'Src (EN)','Title',0,1,1,3,?,?)",
                        (sid, staging, db.now()))
            add(con, sid, 3, "wanted")
            self.assertEqual(core.import_series(con, sid), 1)
        self.assertEqual((self.chapter(sid, 3)["status"], self.chapter(sid, 3)["pages"]), ("have", 7))


# -- finding the blockers -----------------------------------------------------------

class BlockersTest(StuckBase):
    def test_a_failed_chapter_later_ones_wait_for(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            other = db.upsert_series(con, Series(english="Fine"))
            add(con, other, 1, "failed", reason="boom")
            add(con, other, 2, "wanted", reason="available on X; not downloaded yet - waiting for a download pass")
            found = stuck.blockers(con)
        self.assertEqual([(st.series_id, st.number, st.waiting) for st in found], [(sid, 7.2, 20)])
        st = found[0]
        self.assertEqual((st.status, st.name, st.source, st.reason, st.tries, st.failed_since),
                         ("failed", "Chapter 7.2", MN, GONE, 2, SINCE))
        self.assertEqual((st.names, st.urls, st.dismissed), ({MN: "Chapter 7.2"},
                                                             {MN: "https://manganato.example/manga/ch-7.2"}, False))

    def test_what_an_earlier_version_left_counts_too(self):
        old = ("waiting for chapter 171.01: chapters download in order and 171.01 failed on every source (it is retried "
               "on schedule; turn off 'download in order' to skip ahead)")
        with db.connect() as con:
            sid = db.upsert_series(con, Series(english="Old"))
            add(con, sid, 171.01, "failed", reason="boom")
            for n in (171.02, 172):
                add(con, sid, n, "wanted", reason=old)
            found = stuck.blockers(con, sid)
        self.assertEqual([(st.number, st.waiting, st.names) for st in found], [(171.01, 2, {})])

    def test_not_stuck(self):
        cases = {"strict order off": None, "the chapter is wanted again (a resolve)": "wanted",
                 "it was skipped": "ignored", "it arrived": "have"}
        for why, status in cases.items():
            with self.subTest(why), db.connect() as con:
                self.set(download_in_order=status is not None)
                for r in db.series_rows(con):
                    db.delete_series(con, r["id"])
                sid = seed_dangers(con)
                if status:
                    con.execute("UPDATE chapter SET status=? WHERE series_id=? AND number=7.2", (status, sid))
                self.assertEqual(stuck.blockers(con), [])
        self.set(download_in_order=True)


# -- evidence ---------------------------------------------------------------------

class _Chapter:
    def __init__(self, cid, number, name):
        self.id, self.number, self.name = cid, number, name


class _Match:
    def __init__(self, source, chapters):
        self.source = type("Src", (), {"name": source})()
        self.chapters = chapters


class _Plan:
    def __init__(self, candidates):
        self.candidates = candidates


class _Client:
    def __init__(self, urls):
        self.urls, self.asked = urls, []

    def chapter_url(self, cid):
        self.asked.append(cid)
        u = self.urls[cid]
        if isinstance(u, Exception):
            raise u
        return u


class EvidenceTest(StuckBase):
    def plan(self, **names):
        """A plan whose candidates for 7.2 are these sites (name -> its title), best first."""
        return _Plan({7.2: [_Match(src.replace("_", " "), [_Chapter(100 + i, 7.2, name), _Chapter(900 + i, 8, "x")])
                            for i, (src, name) in enumerate(names.items())]})

    def test_names_and_pages_on_the_sites_from_the_plan(self):
        from mangarr.suwayomi import QueryError
        client = _Client({100: "https://manganato.example/ch-7.2", 101: "javascript:alert(1)",
                          102: QueryError("Cannot query field \"realUrl\""), 103: "https://bato.example/7.2"})
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("DELETE FROM stuck")
            plan = self.plan(Manganato="Chapter 7.2", Weeb_Central="Vol.1 Chapter 7.2", Old_Site=None, Bato="7.2")
            with self.assertNoLogs("mangarr.stuck", "WARNING"):
                stuck.update(con, client, sid, DANGERS, plan)
            st = stuck.blockers(con, sid)[0]
            self.assertEqual(st.names, {MN: "Chapter 7.2", WC: "Vol.1 Chapter 7.2", "Old Site": None, "Bato": "7.2"})
            # the first URL_SITES sites are asked, each once; a link that is not http(s) is never kept
            self.assertEqual(st.urls, {MN: "https://manganato.example/ch-7.2", WC: "", "Old Site": ""})
            self.assertEqual(client.asked, [100, 101, 102])
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(client.asked, [100, 101, 102])
            stuck.update(con, None, sid, DANGERS, None)                   # no plan, no client: kept as it was
            self.assertEqual(stuck.blockers(con, sid)[0].names[WC], "Vol.1 Chapter 7.2")

    def test_a_site_missing_from_one_resolve_keeps_its_name(self):
        # Bato's real title holds a certain side story back; a resolve without Bato (it did not answer, or
        # its search missed) must not forget it and skip the chapter
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con, first="Side Story 1")
            names_of(con, sid, 171.01, {MN: "Side Story 1"}, failed_on=[MN, "Bato"])
            con.commit()
        cache_md(FREEDOM, 171.01, ChapterList(None))
        side = _Match(MN, [_Chapter(1, 171.01, "Side Story 1")])
        both = _Plan({171.01: [side, _Match("Bato", [_Chapter(2, 171.01, "Chapter 171.01: The Duel")])]})
        with db.connect() as con:
            stuck.update(con, None, sid, FREEDOM, both)
            self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")        # Bato's title: unknown
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [side]}))
            self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
            self.assertEqual(stuck.blockers(con, sid)[0].names, {MN: "Side Story 1", "Bato": "Chapter 171.01: The Duel"})
            # a site no resolve has seen list it for KEEP_DAYS no longer lists it
            old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - (stuck.KEEP_DAYS + 1) * 86400))
            seen = json.loads(con.execute("SELECT seen FROM stuck").fetchone()[0])
            con.execute("UPDATE stuck SET seen=?", (json.dumps({**seen, "Bato": old}),))
            self.ripe(con, sid, 171.01)
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [side]}))
        self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")

    def test_a_page_link_suwayomi_did_not_answer_for_is_asked_again(self):
        from mangarr.suwayomi import SuwayomiUnreachable
        client = _Client({100: SuwayomiUnreachable("Suwayomi is not answering")})
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("DELETE FROM stuck")
            con.commit()
            plan = self.plan(Manganato="Chapter 7.2")
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {})
            client.urls[100] = TimeoutError("timed out")
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {})
            client.urls[100] = "https://manganato.example/ch-7.2"
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {MN: "https://manganato.example/ch-7.2"})
            stuck.update(con, client, sid, DANGERS, plan)                 # kept: not asked again
        self.assertEqual(client.asked, [100, 100, 100])

    def test_a_page_link_is_asked_again_after_any_error_but_an_answer(self):
        # Review: an HTTP error (a proxy's 502 while Suwayomi restarts) or an answer that is not JSON stored ''
        # as if the site had no page, and the Open button was gone for good
        from mangarr.suwayomi import SuwayomiError
        client = _Client({100: SuwayomiError("Suwayomi at http://x/api/graphql failed: HTTPError: HTTP Error 502: "
                                             "Bad Gateway")})
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("DELETE FROM stuck")
            con.commit()
            plan = self.plan(Manganato="Chapter 7.2")
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {})
            client.urls[100] = SuwayomiError("Suwayomi at http://x/api/graphql failed: JSONDecodeError: Expecting value")
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {})
            client.urls[100] = "https://manganato.example/ch-7.2"
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {MN: "https://manganato.example/ch-7.2"})
            self.assertEqual(client.asked, [100, 100, 100])
            # Suwayomi's own answer that it has no such field (or chapter): kept, not asked again
            from mangarr.suwayomi import QueryError
            con.execute("DELETE FROM stuck")
            con.commit()
            client.urls[100] = QueryError('Cannot query field "realUrl" on type "ChapterType".')
            stuck.update(con, client, sid, DANGERS, plan)
            stuck.update(con, client, sid, DANGERS, plan)
            self.assertEqual(stuck.blockers(con, sid)[0].urls, {MN: ""})
        self.assertEqual(client.asked, [100, 100, 100, 100])

    def test_suwayomi_s_error_answers_are_told_from_no_answer(self):
        import urllib.error

        from mangarr.suwayomi import Client, QueryError, SuwayomiError
        c = Client("http://suwayomi.invalid")
        with mock.patch.object(Client, "_send", return_value={"errors": [{"message": "Cannot query field \"realUrl\""}]}):
            with self.assertRaises(QueryError):
                c.chapter_url(1)
        for e in (urllib.error.HTTPError("http://suwayomi.invalid/api/graphql", 502, "Bad Gateway", {}, None),
                  ValueError("Expecting value: line 1 column 1 (char 0)")):
            with self.subTest(e), mock.patch.object(Client, "_send", side_effect=e):
                with self.assertRaises(SuwayomiError) as cm:
                    c.chapter_url(1)
                self.assertNotIsInstance(cm.exception, QueryError)

    def test_the_listing_site_s_whole_chapters_are_kept(self):
        spin = Series(anilist_id=424244, romaji="Tensei Shitara Slime Datta Ken: Tensura Nikki",
                      english="The Slime Diaries")
        site = [_Chapter(1000 + n, float(n), f"Tensura Nikki Gaiden Chapter {n}") for n in range(1, 40)]
        plan = _Plan({12.5: [_Match(MN, [*site, _Chapter(1125, 12.5, "Tensura Nikki Gaiden Chapter 12.5")]),
                             _Match("Bato", [_Chapter(2000 + n, float(n), f"Chapter {n}") for n in (12, 12.5, 13)])]})
        with db.connect() as con:
            sid = self.seed_spin_off(con, spin)
            stuck.update(con, None, sid, spin, plan)
            st = stuck.blockers(con, sid)[0]
            self.assertEqual(st.wholes, {MN: {"12": "Tensura Nikki Gaiden Chapter 12",
                                              "13": "Tensura Nikki Gaiden Chapter 13",
                                              "11": "Tensura Nikki Gaiden Chapter 11"}, "Bato": {}})
            # a resolve without Manganato keeps them with its name; one without the site's list keeps them too
            stuck.update(con, None, sid, spin, _Plan({12.5: [plan.candidates[12.5][1]]}))
            stuck.update(con, None, sid, spin, None)
            self.assertEqual(stuck.blockers(con, sid)[0].wholes[MN]["12"], "Tensura Nikki Gaiden Chapter 12")

    @staticmethod
    def seed_spin_off(con, spin) -> int:
        """A spin-off stuck behind 12.5: your chapters are Weeb Central's ("Chapter N"), and only Manganato
        lists 12.5, named as it names every chapter."""
        sid = db.upsert_series(con, spin)
        for n in range(1, 40):
            add(con, sid, n, "have", f"Chapter {n}", WC, pages=30)
        add(con, sid, 12.5, "failed", "Tensura Nikki Gaiden Chapter 12.5", MN, reason=GONE, tries=1,
            failed_since=SINCE)
        for n in range(40, 50):
            add(con, sid, n, "wanted", reason=downloader.waiting_reason(12.5))
        con.commit()
        return sid

    def test_what_was_decided_outlasts_a_resolve_that_missed_it(self):
        # Review: a resolve in which the only site listing the blocker missed it (not unreachable: its search
        # answered without it) made it 'unavailable', and the stuck row went with everything decided about it.
        # Now such a resolve leaves it failed (db.past_grace), and both are kept until its grace is over
        def resolve(status, gone=False):
            con.execute("UPDATE chapter SET status=?, missed=?, listed_at=? WHERE series_id=? AND number=7.2",
                        (status, db.LISTING_GRACE_RESOLVES if gone else 1, db.ago(3 if gone else 0), sid))
            stuck.update(con, None, sid, DANGERS, None, found=[])

        def count(table):
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.dismiss(con, sid, 7.2)
            resolve("failed")                                                 # one resolve that missed it
            self.assertEqual((count("stuck"), count("stuck_choice")), (1, 1))
            resolve("unavailable")                                            # an older version's, within its grace
            self.assertEqual((count("stuck"), count("stuck_choice")), (1, 1))
            resolve("unavailable", gone=True)                                 # its grace is over:
            self.assertEqual((count("stuck"), count("stuck_choice")), (0, 1))    # the evidence goes, not the choice
            resolve("failed")
            st = stuck.blockers(con, sid)[0]
            self.assertEqual((st.keep_waiting, st.dismissed), (True, True))
            self.assertIsNone(con.execute("SELECT gone_since FROM stuck_choice").fetchone()[0])
            # gone for good: no site has listed it for GONE_DAYS after its grace
            resolve("unavailable", gone=True)
            since = con.execute("SELECT gone_since FROM stuck_choice").fetchone()[0]
            self.assertIsNotNone(since)
            # Review: a resolve in which it was junk counted as gone too, although the sites still list it: a
            # decision was dropped a week later. Listed as junk it is not gone; a junk chapter past its grace is
            resolve("junk")
            self.assertIsNone(con.execute("SELECT gone_since FROM stuck_choice").fetchone()[0])
            self.assertEqual((count("stuck"), count("stuck_choice")), (0, 1))
            resolve("junk", gone=True)
            since = con.execute("SELECT gone_since FROM stuck_choice").fetchone()[0]
            self.assertIsNotNone(since)
            resolve("unavailable", gone=True)
            self.assertEqual(con.execute("SELECT gone_since FROM stuck_choice").fetchone()[0], since)
            old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - (stuck.GONE_DAYS + 1) * 86400))
            con.execute("UPDATE stuck_choice SET gone_since=?", (old,))
            resolve("unavailable", gone=True)
            self.assertEqual(count("stuck_choice"), 0)
            # on disk: nothing left to decide
            resolve("failed")
            stuck.dismiss(con, sid, 7.2)
            db.set_have(con, sid, 7.2, None, None)
            stuck.update(con, None, sid, DANGERS, None, found=[])
            self.assertEqual((count("stuck"), count("stuck_choice")), (0, 0))
            # a skip is kept while the chapter is skipped, and an un-skip (declined) like Keep waiting
            add(con, sid, 7.2, "failed", "Chapter 7.2", MN, reason=GONE)
            stuck.skip(con, sid, 7.2)
            stuck.update(con, None, sid, DANGERS, None, found=[])
            stuck.unskip(con, sid, 7.2)
            resolve("unavailable")
            resolve("failed")
            con.execute("UPDATE chapter SET reason=? WHERE series_id=? AND status='wanted'",
                        (downloader.waiting_reason(7.2), sid))                  # the next pass holds them again
            self.assertTrue(stuck.blockers(con, sid)[0].declined)

    def test_what_a_download_run_failed_it_on_is_kept(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.note_failures(con, sid, {7.2: "failed", 8.0: "ok", 9.0: "ok"},
                                [(MN, 7.2, "failed"), ("Bato", 7.2, "failed"), (MN, 7.2, "failed"), (WC, 8.0, "ok"),
                                 ("Bato", 9.0, "failed"), (WC, 9.0, "ok")])
            con.commit()
            self.assertEqual(stuck.blockers(con, sid)[0].failed_on, [MN, "Bato"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stuck").fetchone()[0], 1)    # 9 arrived after all
            self.set(download_in_order=False)
            stuck.note_failures(con, sid, {10.0: "failed"}, [(MN, 10.0, "failed")])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stuck").fetchone()[0], 1)    # nothing is held back

    def test_safe_url(self):
        for u in ("https://a.example/x", "http://a.example:8080/x?y=1"):
            self.assertEqual(stuck.safe_url(u), u)
        for u in ("javascript:alert(1)", "data:text/html,x", "ftp://a.example/x", "https://", "https://a b/",
                  "https://a.example/\nx", "https://a.example/\x7f", "//a.example/x", None, 5,
                  "https://a.example/" + "x" * 3000):
            with self.subTest(u):
                self.assertIsNone(stuck.safe_url(u))

    def test_pages_the_verdict_compares_are_counted_from_the_library(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("UPDATE chapter SET pages=NULL WHERE series_id=?", (sid,))
            for n in (5, 6, 7, 8, 9, 10):
                add(con, sid, n, "have", path=make_cbz(os.path.join(self.lib, "D", f"Chapter {n}.cbz"), 11))
            add(con, sid, 11, "have", path=make_cbz(os.path.join(self.tmp, "elsewhere", "Chapter 11.cbz"), 11))
            con.commit()
            self.assertEqual(stuck.fill_pages(con, sid, 7.2), 6)            # not the file outside the library
            self.assertEqual(stuck.fill_pages(con, sid, 7.2), 0)            # counted once
            self.assertIsNone(self.chapter(sid, 11)["pages"])
            cache_md(DANGERS, 7.2, DANGERS_MD)
            v = stuck.details(con, db.get_series(con, sid))[0].verdict
        self.assertIn("Your chapter 7 has 11 pages, like MangaDex's chapter 7 (11).", v.evidence)

    def test_what_no_longer_matters_is_forgotten(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.dismiss(con, sid, 7.2)
            con.execute("UPDATE chapter SET status='wanted' WHERE series_id=? AND number=7.2", (sid,))   # resolved
            stuck.update(con, None, sid, DANGERS, None, found=[])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stuck").fetchone()[0], 1)     # still failing
            db.set_have(con, sid, 7.2, None, None)
            stuck.update(con, None, sid, DANGERS, None, found=[])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stuck").fetchone()[0], 0)

    def test_each_site_s_list_is_read_once_per_update(self):
        # Review: the side-story words of every site's whole chapters were worked out again for every chapter with
        # evidence (rows x sites x chapters, twice a pass): 3.6 s for 2000 chapters and 100 rows
        with db.connect() as con:
            sid = seed_dangers(con)
            fractions = [n + 0.5 for n in range(100, 140)]
            sites = [_Match(f"Site {k}", [_Chapter(k * 10000 + n, n, f"Chapter {n} Extra" if n % 50 == 0 else
                                                   f"Chapter {n}") for n in [*range(1, 400), *fractions]])
                     for k in range(3)]
            for n in fractions:                                         # skipped ones keep their evidence
                add(con, sid, n, "ignored", "Side Story", "Site 0")
                names_of(con, sid, n, {"Site 0": "Side Story"})
            con.commit()
            plan = _Plan({n: sites for n in [7.2, *fractions]})
            real = stuck.verdict.site_marks
            calls = []
            with mock.patch.object(stuck.verdict, "site_marks", lambda *a: calls.append(1) or real(*a)):
                stuck.update(con, None, sid, DANGERS, plan)
            self.assertEqual(len(calls), 3)                             # once per site, not per chapter
            wholes = json.loads(con.execute("SELECT wholes FROM stuck WHERE number=120.5").fetchone()[0])
        alone = stuck.verdict.site_words([(c.number, c.name) for c in sites[1].chapters], DANGERS, 120.5)
        self.assertEqual(wholes["Site 1"], {f"{k:g}": v for k, v in alone.items()})     # as worked out on its own
        self.assertIn("100", wholes["Site 1"])

    def test_it_never_raises(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            with mock.patch.object(stuck, "_names", side_effect=RuntimeError("boom")), \
                    self.assertLogs("mangarr.stuck", "WARNING") as cm:
                stuck.update(con, None, sid, DANGERS, self.plan(Manganato="x"))
        self.assertIn("could not update the chapters the series is stuck behind: RuntimeError: boom", cm.output[0])


# -- Skip it, Un-skip, Keep waiting --------------------------------------------------

class ActionsTest(StuckBase):
    def test_skip_and_unskip(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            self.assertTrue(stuck.skip(con, sid, 7.2, waiting=20))
            c = self.chapter(sid, 7.2)
            self.assertEqual((c["status"], c["reason"]), ("ignored", "skipped: the chapters after it download without "
                                                                     "it (un-skip to want it again)"))
            self.assertEqual(stuck.blockers(con, sid), [])
            self.assertEqual(stuck.skipped(con, sid), [{"number": 7.2, "how": "manual", "verdict": None,
                                                        "names": {MN: "Chapter 7.2"}, "changed": False}])
            self.assertFalse(stuck.skip(con, sid, 7.2))                     # already skipped
            self.assertFalse(stuck.skip(con, sid, 3))                       # on disk
            self.assertFalse(stuck.unskip(con, sid, 3))
            self.assertTrue(stuck.unskip(con, sid, 7.2))
            c = self.chapter(sid, 7.2)
            self.assertEqual((c["status"], c["reason"], c["tries"], c["failed_since"]), ("wanted", None, 0, None))
            self.assertEqual(stuck.skipped(con, sid), [])
        self.assertEqual(self.events(sid), [("skip", "chapter 7.2 skipped; it held back 20 chapters"),
                                            ("skip", "chapter 7.2 un-skipped: wanted again")])

    def test_skip_tells_the_chapters_that_waited_for_it(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.skip(con, sid, 7.2)
            self.assertEqual(self.chapter(sid, 40)["reason"], "no longer waits for chapter 7.2, which was skipped: the "
                                                              "next pass downloads it")
            stuck.unskip(con, sid, 7.2)
            self.assertEqual(self.chapter(sid, 40)["reason"], "not downloaded yet - waiting for a download pass")

    def test_keep_it_skipped_as_it_is_listed_now(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            self.assertFalse(stuck.keep_skipped(con, sid, 7.2))              # not skipped
            stuck.skip(con, sid, 7.2)
            names_of(con, sid, 7.2, {MN: "The Duel"})
            con.commit()
            self.assertTrue(stuck.skipped(con, sid)[0]["changed"])
            self.assertTrue(stuck.keep_skipped(con, sid, 7.2))
            self.assertFalse(stuck.skipped(con, sid)[0]["changed"])
            self.assertEqual(self.chapter(sid, 7.2)["status"], "ignored")
        self.assertEqual(self.events(sid)[-1], ("skip", "chapter 7.2 kept skipped, as it is listed now"))

    def test_a_site_that_did_not_answer_changes_nothing(self):
        # the chapter's state is its names on the sites: one that does not list it this time is no change
        both = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")]), _Match("Bato", [_Chapter(2, 7.2, None)])]})
        only = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")])]})
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.update(con, None, sid, DANGERS, both)
            stuck.dismiss(con, sid, 7.2)
            stuck.update(con, None, sid, DANGERS, only)
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)
            stuck.skip(con, sid, 7.2)
            stuck.update(con, None, sid, DANGERS, only)
            stuck.update(con, None, sid, DANGERS, both)
            self.assertFalse(stuck.skipped(con, sid)[0]["changed"])
        then = {"name": "x", "names": {MN: "a", "Bato": "b"}}
        self.assertTrue(stuck.same_state(then, {"name": "x", "names": {MN: "a"}}))
        self.assertFalse(stuck.same_state(then, {"name": "x", "names": {MN: "a", "Bato": "c"}}))
        self.assertFalse(stuck.same_state(then, {"name": "x", "names": {MN: "a", "Bato": "b", "Comick": None}}))
        self.assertTrue(stuck.same_state(then, {"name": "y", "names": {MN: "a"}}))     # the row's name: a site's
        # while one side knows no site's name, the row's name is what there is
        self.assertTrue(stuck.same_state({"name": "a", "names": {}}, {"name": "b", "names": {MN: "a", "Bato": "b"}}))
        self.assertFalse(stuck.same_state({"name": "a", "names": {}}, {"name": "c", "names": {MN: "c"}}))
        self.assertTrue(stuck.same_state({"name": "b", "names": {MN: "a", "Bato": "b"}}, {"name": "a", "names": {}}))
        self.assertFalse(stuck.same_state({"name": "b", "names": {MN: "b"}}, {"name": "c", "names": {}}))
        self.assertFalse(stuck.same_state({"name": "a", "names": {}}, {"name": "c", "names": {}}))

    def test_two_sites_naming_it_differently_are_no_change_without_one(self):
        # Review: the chapter row's name is copied from whichever site is assigned, so a resolve without one of
        # two sites that name it differently renamed the row, and that counted as a change
        both = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")]),
                            _Match("Bato", [_Chapter(2, 7.2, "Side Story: Picnic")])]})
        bato = _Plan({7.2: [_Match("Bato", [_Chapter(2, 7.2, "Side Story: Picnic")])]})

        def resolve(plan, name):
            con.execute("UPDATE chapter SET name=? WHERE series_id=? AND number=7.2", (name, sid))    # save_plan
            stuck.update(con, None, sid, DANGERS, plan)
        with db.connect() as con:
            sid = seed_dangers(con)
            resolve(both, "Chapter 7.2")
            stuck.dismiss(con, sid, 7.2)
            resolve(bato, "Side Story: Picnic")
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)
            resolve(both, "Chapter 7.2")
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)
            stuck.skip(con, sid, 7.2)
            resolve(bato, "Side Story: Picnic")
            self.assertFalse(stuck.skipped(con, sid)[0]["changed"])
            resolve(both, "Chapter 7.2")
            self.assertFalse(stuck.skipped(con, sid)[0]["changed"])

    def test_a_skipped_chapter_that_changes_is_offered_back(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            stuck.skip(con, sid, 7.2)
            plan = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "The Duel")])]})
            stuck.update(con, None, sid, DANGERS, plan)
            got = stuck.skipped(con, sid)
            self.assertEqual((got[0]["changed"], got[0]["names"]), (True, {MN: "The Duel"}))
            self.assertEqual(self.chapter(sid, 7.2)["status"], "ignored")        # a manual skip is only offered back

    def test_keep_waiting_until_the_chapter_changes(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            self.assertTrue(stuck.dismiss(con, sid, 7.2))
            self.assertFalse(stuck.dismiss(con, sid, 8))                     # not stuck behind 8
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)
            # a pass: the resolve makes it wanted again, its download fails it again the same way
            blocked = stuck.blockers(con, sid)
            con.execute("UPDATE chapter SET status='wanted' WHERE series_id=? AND number=7.2", (sid,))
            same = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")])]})
            stuck.update(con, None, sid, DANGERS, same, blocked)
            con.execute("UPDATE chapter SET status='failed' WHERE series_id=? AND number=7.2", (sid,))
            stuck.update(con, None, sid, DANGERS, same)
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)
            # another site lists it now
            more = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")]), _Match("Bato", [_Chapter(2, 7.2, None)])]})
            stuck.update(con, None, sid, DANGERS, more)
            self.assertFalse(stuck.blockers(con, sid)[0].dismissed)
            stuck.dismiss(con, sid, 7.2)
            renamed = _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "The Duel")]), _Match("Bato", [_Chapter(2, 7.2, None)])]})
            stuck.update(con, None, sid, DANGERS, renamed)
            st = stuck.blockers(con, sid)[0]
            self.assertEqual((st.dismissed, st.keep_waiting), (False, True))  # renamed: the note is back

    def test_names_learnt_after_keep_waiting_are_no_change(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("DELETE FROM stuck")
            stuck.dismiss(con, sid, 7.2)                                     # no resolve has seen it yet
            stuck.update(con, None, sid, DANGERS, _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")])]}))
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)


# -- the automatic skip ------------------------------------------------------------------

def every_site(con, sid) -> "_Plan":
    """A plan in which every site listing the series' blockers (as their stuck rows have them) answered and
    lists them under those names."""
    return _Plan({r["number"]: [_Match(src, [_Chapter(i, r["number"], name)])
                                for i, (src, name) in enumerate(json.loads(r["names"]).items())]
                  for r in con.execute("SELECT number, names FROM stuck WHERE series_id=?", (sid,))})


class AutoSkipTest(StuckBase):
    def run_update(self, series, sid, found=None, ripe=True):
        """A pass's update in which every site listing the blockers answered; ripe: passes have judged them so
        for a day already."""
        with db.connect() as con, self.assertNoLogs("mangarr.stuck", "WARNING"):
            if ripe:
                for n in [r[0] for r in con.execute("SELECT number FROM stuck WHERE series_id=?", (sid,))]:
                    self.ripe(con, sid, n)
            stuck.update(con, None, sid, series, every_site(con, sid), found)

    def test_off_by_default(self):
        self.assertFalse(settings.get("auto_skip_side_stories"))
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")

    def test_a_certain_side_story(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))         # MangaDex was asked: no entry is tied to the series
        with db.connect() as con, self.assertLogs("mangarr.stuck", "INFO") as cm:
            self.ripe(con, sid, 171.01)
            stuck.update(con, None, sid, FREEDOM, every_site(con, sid))
        self.assertEqual([x for x in cm.output if not x.startswith("INFO:")], [])
        c = self.chapter(sid, 171.01)
        self.assertEqual((c["status"], c["reason"]),
                         ("ignored", "skipped automatically: probably a side story (high confidence)"))
        self.assertIn("INFO:mangarr.stuck:Dreaming Freedom: ch 171.01 skipped automatically: probably a side story "
                      "(high confidence); 13 later chapter(s) waited for it", cm.output)
        self.assertEqual(self.events(sid), [("skip", "chapter 171.01 skipped automatically: probably a side story "
                                                     "(high confidence); it held back 13 chapters")])
        with db.connect() as con:
            self.assertEqual(stuck.skipped(con, sid), [{"number": 171.01, "how": "auto", "verdict": "Probably a side "
                                                        "story", "names": {MN: "Spin-off 1"}, "changed": False}])
            self.assertEqual(stuck.blockers(con, sid), [])

    def test_certainly_covered(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_dangers(con)
        cache_md(DANGERS, 7.2, DANGERS_MD)
        self.run_update(DANGERS, sid)
        self.assertEqual(self.chapter(sid, 7.2)["reason"], "skipped automatically: probably already covered by "
                                                           "chapter 7 you have (high confidence)")

    def test_it_waits_for_mangadex(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        self.run_update(FREEDOM, sid)                         # not looked up yet: the lookup is queued
        self.assertEqual((self.chapter(sid, 171.01)["status"], self.fetcher.pending()), ("failed", 1))
        cache_md(FREEDOM, 171.01, None)                       # looked up, but MangaDex did not answer
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        self.assertEqual(self.lookups, [])                    # never from the pass itself

    def test_only_with_strict_order(self):
        self.set(auto_skip_side_stories=True, download_in_order=False)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")

    def test_never_a_low_confidence_or_other_verdict(self):
        self.set(auto_skip_side_stories=True)
        cases = [("bonus", FREEDOM, 171.01, "Bonus Chapter"), ("unknown", WRONG, 15.5, "Chapter 15.5"),
                 ("rest of chapter 1", NOZAKI, 1.1, "Chapter 1.1"), ("a whole chapter", FREEDOM, 171, "Side Story")]
        for why, series, n, name in cases:
            with self.subTest(why), db.connect() as con:
                for r in db.series_rows(con):
                    db.delete_series(con, r["id"])
                sid = db.upsert_series(con, series)
                for k in range(1, 40):
                    add(con, sid, k, "have", pages=30)
                if series is NOZAKI:
                    add(con, sid, 1, "have", pages=15)
                add(con, sid, n, "failed", name, MN, reason=GONE)
                add(con, sid, 900, "wanted", reason=downloader.waiting_reason(n))
                names_of(con, sid, n, {MN: name})
                con.commit()
                cache_md(series, n, ChapterList(None))
                with self.assertNoLogs("mangarr.stuck", "WARNING"):
                    stuck.update(con, None, sid, series, None)
                self.assertEqual(self.chapter(sid, n)["status"], "failed")
                self.assertFalse(stuck.details(con, db.get_series(con, sid))[0].verdict.auto_skip)

    def test_none_of_the_false_positives(self):
        # the adversarial check's real story chapters (test_verdict.FALSE_POSITIVES), through the automatic skip
        self.set(auto_skip_side_stories=True)
        for label, series, blocker, rows, md, _ in FALSE_POSITIVES + REVIEW_FALSE_POSITIVES:
            with self.subTest(label), db.connect() as con:
                for r in db.series_rows(con):
                    db.delete_series(con, r["id"])
                sid = db.upsert_series(con, series)
                for c in rows:
                    add(con, sid, c.number, c.status, c.name, c.source, c.pages)
                con.commit()
                cache_md(series, blocker.number, md if md is not None else ChapterList(None))
                st = stuck.Stuck(sid, float(blocker.number), 1, "failed", None, MN, "boom", 1, None, dict(blocker.names),
                                 failed_on=list(blocker.names), wholes=dict(blocker.wholes))
                with self.assertNoLogs("mangarr.stuck", "WARNING"):
                    stuck.update(con, None, sid, series, None, [st])
                self.assertNotEqual(self.chapter(sid, float(blocker.number))["status"], "ignored")
                self.assertEqual([e for e in self.events(sid) if e[0] == "skip"], [])

    def test_the_real_cases_go_the_same_way(self):
        # the control for the test above: the same path skips the spec's real side story and covered piece
        self.set(auto_skip_side_stories=True)
        for series, seed, n, md in ((DANGERS, seed_dangers, 7.2, DANGERS_MD), (FREEDOM, seed_freedom, 171.01,
                                                                                  ChapterList(None))):
            with self.subTest(series.title), db.connect() as con:
                sid = seed(con)
                blocked = stuck.blockers(con, sid)
                self.assertEqual(blocked[0].verdict, None)
                cache_md(series, n, md)
                self.assertEqual(stuck.details(con, db.get_series(con, sid))[0].verdict.confidence, HIGH)
                with self.assertNoLogs("mangarr.stuck", "WARNING"):
                    stuck.update(con, None, sid, series, every_site(con, sid), blocked)
                    self.assertEqual(self.chapter(sid, n)["status"], "failed")      # the first pass that says so
                    self.ripe(con, sid, n)                                          # ... a day ago
                    stuck.update(con, None, sid, series, every_site(con, sid), stuck.blockers(con, sid))
                self.assertEqual(self.chapter(sid, n)["status"], "ignored")

    def fail_again(self, sid, n):
        """What the next pass does to a wanted chapter that still fails on every source listing it."""
        with db.connect() as con:
            db.set_status(con, sid, n, "failed", GONE)
            con.execute("UPDATE chapter SET reason=? WHERE series_id=? AND status='wanted'",
                        (downloader.waiting_reason(n), sid))
            con.commit()

    def test_an_undone_automatic_skip_is_never_repeated(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")
        with db.connect() as con:
            self.assertTrue(stuck.unskip(con, sid, 171.01))                  # undo
        self.fail_again(sid, 171.01)
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        with db.connect() as con:
            self.assertTrue(stuck.blockers(con, sid)[0].declined)
        self.assertEqual([m for k, m in self.events(sid) if k == "skip"][1:],
                         ["chapter 171.01 un-skipped: wanted again"])
        # a manual skip undone before the automatic skip was turned on counts the same
        with db.connect() as con:
            other = seed_dangers(con)
            stuck.skip(con, other, 7.2)
            stuck.unskip(con, other, 7.2)
        self.fail_again(other, 7.2)
        cache_md(DANGERS, 7.2, DANGERS_MD)
        self.run_update(DANGERS, other)
        self.assertEqual(self.chapter(other, 7.2)["status"], "failed")

    def test_keep_waiting_keeps_it_off_for_good(self):
        with db.connect() as con:
            sid = seed_freedom(con)
            self.assertTrue(stuck.dismiss(con, sid, 171.01))
        self.set(auto_skip_side_stories=True)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        # the site renames it: the note is back, but you chose to wait for this chapter: only you skip it
        with db.connect() as con:
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "Spin-off 1: "
                                                                                                "Picnic")])]}))
            st = stuck.details(con, db.get_series(con, sid))[0]
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        self.assertEqual((st.dismissed, st.keep_waiting, st.verdict.auto_skip), (False, True, True))
        self.assertEqual(stuck.why_not_auto(st, st.verdict), "waiting")

    def test_not_a_word_the_listing_site_gives_every_chapter(self):
        # Review: only Manganato lists 12.5 and names every chapter "Tensura Nikki Gaiden Chapter N"; your
        # chapters come from Weeb Central ("Chapter N"), so they did not show that "Gaiden" is the series' word
        self.set(auto_skip_side_stories=True)
        spin = Series(anilist_id=424244, romaji="Tensei Shitara Slime Datta Ken: Tensura Nikki",
                      english="The Slime Diaries")
        for label, named in (("the spin-off's word", "Tensura Nikki Gaiden Chapter {}"), ("a plain site", "Chapter {}")):
            with self.subTest(label), db.connect() as con:
                for r in db.series_rows(con):
                    db.delete_series(con, r["id"])
                sid = EvidenceTest.seed_spin_off(con, spin)
                names_of(con, sid, 12.5, {MN: "Tensura Nikki Gaiden Chapter 12.5"})
                con.commit()
                cache_md(spin, 12.5, ChapterList(None))
                site = [_Chapter(1000 + n, float(n), named.format(n)) for n in range(1, 40)]
                plan = _Plan({12.5: [_Match(MN, [*site, _Chapter(1125, 12.5, "Tensura Nikki Gaiden Chapter 12.5")])]})
                self.ripe(con, sid, 12.5)
                with self.assertNoLogs("mangarr.stuck", "WARNING"):
                    stuck.update(con, None, sid, spin, plan)
                if named.startswith("Chapter"):             # the control: a site's own side-story word decides
                    self.assertEqual(self.chapter(sid, 12.5)["status"], "ignored")
                    continue
                self.assertEqual(self.chapter(sid, 12.5)["status"], "failed")
                v = stuck.details(con, db.get_series(con, sid))[0].verdict
                self.assertEqual((v.kind, v.confidence), ("unknown", "low"))
                self.assertIn("but whole chapters on Manganato have that word in their names too", " ".join(v.evidence))

    def test_mangadex_not_answering_holds_it_and_the_note_says_so(self):
        # Review: while MangaDex's failure is kept, a HIGH verdict from the name alone said "the next pass skips
        # it automatically", but no pass skips anything before MangaDex has answered
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, None)                        # asked, and MangaDex did not answer
        before = time.time()
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        with db.connect() as con:
            st = stuck.details(con, db.get_series(con, sid))[0]
        self.assertEqual((st.verdict.kind, st.verdict.confidence, st.md_wait, st.checking),
                         ("side_story", HIGH, "failed", False))
        self.assertEqual(stuck.why_not_auto(st, st.verdict), "mangadex")
        self.assertTrue(before + mangadex.FAILED_TTL - 60 < st.md_retry <= time.time() + mangadex.FAILED_TTL)
        self.assertEqual(self.fetcher.pending(), 0)            # nothing to look up before the failure expires
        if views is not None:
            self.assertEqual(views.auto_skip_clause(st, True), "not skipped automatically while MangaDex does not "
                                                               "answer: its chapter list may change this verdict")
            self.assertEqual(views.mangadex_line(st), "MangaDex did not answer, so its chapter list is not in this "
                             "verdict yet: it is asked again after "
                             f"{time.strftime('%H:%M', time.localtime(st.md_retry))}.")
        # not looked up yet: the same, and the Fetcher is asked
        mangadex._cache.clear()
        with db.connect() as con:
            st = stuck.details(con, db.get_series(con, sid))[0]
        self.assertEqual((st.md_wait, st.checking), ("pending", True))
        if views is not None:
            self.assertEqual(views.auto_skip_clause(st, True), "not skipped automatically before MangaDex's chapter "
                                                               "list is checked, which may change this verdict")
            self.assertEqual(views.mangadex_line(st), "")
        # a manual series has no MangaDex list to wait for
        self.assertIsNone(mangadex.failed_until(Series(english="By hand"), 1.5))

    def test_a_site_that_just_started_listing_it_is_tried_first(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_dangers(con)
            blocked = stuck.blockers(con, sid)
            # what save_plan does: Bato lists it now, so this pass tries it there
            con.execute("UPDATE chapter SET status='wanted', source_name='Bato' WHERE series_id=? AND number=7.2", (sid,))
            con.commit()
        cache_md(DANGERS, 7.2, DANGERS_MD)
        plan = _Plan({7.2: [_Match("Bato", [_Chapter(5, 7.2, "Chapter 7.2")]), _Match(MN, [_Chapter(1, 7.2, "Chapter "
                                                                                                      "7.2")])]})
        with db.connect() as con:
            self.ripe(con, sid, 7.2)
            stuck.update(con, None, sid, DANGERS, plan, blocked)
            self.assertEqual(self.chapter(sid, 7.2)["status"], "wanted")
            # the download fails it on Bato too: now it failed on every site listing it
            db.set_status(con, sid, 7.2, "failed", GONE)
            stuck.note_failures(con, sid, {7.2: "failed"}, [("Bato", 7.2, "failed"), (MN, 7.2, "failed")])
            con.commit()
            stuck.update(con, None, sid, DANGERS, plan)
        self.assertEqual(self.chapter(sid, 7.2)["status"], "ignored")

    def test_never_on_the_first_resolve_that_says_so(self):
        # Review: a chapter was skipped automatically in the pass that first failed it, on what that one resolve saw
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        for _ in range(2):
            self.run_update(FREEDOM, sid, ripe=False)
            self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        with db.connect() as con:
            st = stuck.details(con, db.get_series(con, sid))[0]
            self.assertEqual((st.verdict.confidence, stuck.why_not_auto(st, st.verdict)), (HIGH, "young"))
            since = st.high_since
            self.assertIsNotNone(since)
            # a verdict that no longer holds starts the day again
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "The Duel")])]}))
            self.assertIsNone(stuck.blockers(con, sid)[0].high_since)
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "Spin-off 1")])]}))
            self.assertGreaterEqual(stuck.blockers(con, sid)[0].high_since, since)
        self.run_update(FREEDOM, sid)                           # a day on
        self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")

    def test_not_while_a_site_that_listed_it_does_not_answer(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
            names_of(con, sid, 171.01, {MN: "Spin-off 1", "Bato": "Spin-off 1"})
            self.ripe(con, sid, 171.01)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        only = _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "Spin-off 1")])]})
        with db.connect() as con:
            with self.assertLogs("mangarr.stuck", "DEBUG") as cm:
                stuck.update(con, None, sid, FREEDOM, only)              # Bato did not answer, or missed the series
            self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
            self.assertIn("ch 171.01 not skipped automatically (silent: Bato)", "\n".join(cm.output))
            stuck.update(con, None, sid, FREEDOM, every_site(con, sid))
        self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")

    def test_keep_waiting_pressed_while_the_pass_judges_it_wins(self):
        # Review: the decision was read once before judging, and skip() cleared the Keep waiting pressed meanwhile
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        real = stuck.judge

        def judge(*a, **k):
            with db.connect() as other:
                self.assertTrue(stuck.dismiss(other, sid, 171.01))
            return real(*a, **k)
        with mock.patch.object(stuck, "judge", judge):
            self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")
        with db.connect() as con:
            k = con.execute("SELECT skipped, dismissed FROM stuck_choice").fetchone()
            self.assertEqual((k["skipped"], bool(k["dismissed"])), (None, True))
            self.assertEqual(stuck.blockers(con, sid)[0].keep_waiting, True)
        self.assertEqual([e for e in self.events(sid) if e[0] == "skip"], [])
        # an un-skip, and a skip of yours, landing the same way win too
        for decided in ("UPDATE stuck_choice SET dismissed=NULL, declined='2026-09-28 10:00:00'",
                        "UPDATE stuck_choice SET dismissed=NULL, declined=NULL, skipped='manual'"):
            with db.connect() as con:
                con.execute(decided)
                con.commit()
                self.assertFalse(stuck.skip(con, sid, 171.01, "auto"))
            self.assertEqual(self.chapter(sid, 171.01)["status"], "failed")

    def test_mangadex_answering_during_the_check_is_not_missed(self):
        self.set(auto_skip_side_stories=True)
        series = Series(anilist_id=5, english="Race", status="RELEASING")
        with db.connect() as con:
            sid = db.upsert_series(con, series)
            for k in range(1, 30):
                add(con, sid, k, "have", pages=30)
            add(con, sid, 12.5, "failed", "Side Story", MN, reason=GONE)
            for k in range(30, 50):
                add(con, sid, k, "wanted", reason=downloader.waiting_reason(12.5))
            names_of(con, sid, 12.5, {MN: "Side Story"})
            con.commit()
        answer = ChapterList("u", 50, {12.0: mangadex.MdChapter(12.0, "Twelve", 30),
                                       12.5: mangadex.MdChapter(12.5, "The Duel", 30)}, following=13.0)
        real = mangadex.looked_up

        def lands_now(s, number):                   # the Fetcher stores the answer right at this moment
            cache_md(s, number, answer)
            return real(s, number)
        with mock.patch.object(mangadex, "looked_up", lands_now):
            self.run_update(series, sid)
        self.assertEqual(self.chapter(sid, 12.5)["status"], "failed")
        with db.connect() as con:
            self.assertEqual(stuck.details(con, db.get_series(con, sid))[0].verdict.kind, "unknown")

    def test_one_skipped_automatically_is_wanted_again_when_it_changes(self):
        self.set(auto_skip_side_stories=True)
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, ChapterList(None))
        self.run_update(FREEDOM, sid)
        self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")
        with db.connect() as con, self.assertLogs("mangarr.stuck", "INFO") as cm:
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "Spin-off 1")])]}))
            self.assertEqual(self.chapter(sid, 171.01)["status"], "ignored")        # the same: stays skipped
            stuck.update(con, None, sid, FREEDOM, _Plan({171.01: [_Match(MN, [_Chapter(1, 171.01, "Reunion")])]}))
        self.assertEqual(self.chapter(sid, 171.01)["status"], "wanted")
        self.assertIn("ch 171.01 wanted again: its name on the sites changed since it was skipped automatically",
                      "\n".join(cm.output))
        self.assertEqual(self.events(sid)[-1], ("skip", "chapter 171.01 un-skipped: wanted again (its name on the "
                                                        "sites changed since it was skipped automatically)"))


# -- MangaDex in the background ------------------------------------------------------------

class FetcherTest(StuckBase):
    def test_requests_are_deduplicated_and_bounded(self):
        f = stuck.Fetcher(lookup=lambda s, n: None)
        self.assertTrue(f.request(1, DANGERS, 7.2))
        self.assertTrue(f.request(1, DANGERS, 7.5))            # the same lookup (MangaDex's list around 7)
        self.assertFalse(f.request(3, Series(english="Manual"), 1.5))
        self.assertFalse(f.request(1, DANGERS, float("nan")))
        with mock.patch.object(stuck.Fetcher, "MAX_PENDING", 2):
            self.assertTrue(f.request(2, FREEDOM, 171.01))
            self.assertFalse(f.request(4, NOZAKI, 1.1))
        self.assertEqual(f.pending(), 2)

    def test_one_at_a_time_in_the_background(self):
        self.md[DANGERS.ref] = DANGERS_MD
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("UPDATE chapter SET pages=NULL WHERE series_id=? AND number=7", (sid,))
            con.execute("UPDATE chapter SET library_path=? WHERE series_id=? AND number=7",
                        (make_cbz(os.path.join(self.lib, "D", "Chapter 7.cbz"), 11), sid))
        self.assertTrue(self.fetcher.request(sid, DANGERS, 7.2))
        self.assertTrue(self.fetcher.request(sid, WRONG, 15.5))           # MangaDex fails for this one
        self.fetcher.start()
        self.assertTrue(self.fetcher.wait_idle(10))
        self.assertEqual(self.lookups, [(DANGERS.ref, 7.2), (WRONG.ref, 15.5)])
        self.assertIs(mangadex.english_chapters(DANGERS, 7.2, fetch=False), DANGERS_MD)
        self.assertTrue(mangadex.looked_up(WRONG, 15.5))                  # a failure is kept for FAILED_TTL too
        self.assertEqual(self.chapter(sid, 7)["pages"], 11)
        self.assertTrue(self.fetcher.request(sid, DANGERS, 7.2))          # asked again: answered from the cache
        self.assertTrue(self.fetcher.wait_idle(10))
        self.assertEqual(len(self.lookups), 2)

    def test_a_failing_lookup_does_not_stop_it(self):
        seen = []

        def lookup(s, n):
            seen.append(s.ref)
            if s is DANGERS:
                raise RuntimeError("boom")
        f = stuck.Fetcher(lookup=lookup)
        f.request(1, DANGERS, 7.2)
        f.request(2, FREEDOM, 171.01)
        with self.assertLogs("mangarr.stuck", "WARNING") as cm:
            f.start()
            self.assertTrue(f.wait_idle(10))
        self.assertEqual(seen, [DANGERS.ref, FREEDOM.ref])
        self.assertIn("MangaDex lookup for The Dangers in My Heart ch 7.2 failed: RuntimeError: boom", cm.output[0])


# -- a whole refresh pass --------------------------------------------------------------------

X = "Site X (EN)"


@unittest.skipIf(web is None, "web extras not installed")
class PassTest(PassBase, _Offline):
    """web._run_pass with the fake Suwayomi: the pass that fails the chapter,
    and the next ones (later() runs the next pass that much later)."""

    def setUp(self):
        super().setUp()
        self.days = 0.0
        real = time.time

        def clock():
            return real() + self.days * 86400
        for p in [*self.offline(), mock.patch("time.time", clock),
                  mock.patch.object(db, "now", lambda: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock())))]:
            p.start()
            self.addCleanup(p.stop)

    def later(self, days=1.1):
        """The passes from now on run `days` later."""
        self.days += days

    def scenario(self, name="Side Story 1", **values):
        with db.connect() as con:
            settings.set_many(con, {"download_in_order": True, **values})
            sid = db.upsert_series(con, Series(english="Freedom"))            # by hand: no MangaDex
            row = dict(db.get_series(con, sid))
        settings._cache.clear()
        fake = self.fake()
        m = entry(fake, X, 1, "Freedom", [1, 2, 3, 3.5, 4, 5])
        next(c for c in m.chapters if c.number == 3.5).name = name
        fake.broken = {chapter_id(1, 3.5)}
        return fake, {"Freedom": [m]}, row

    def run_pass(self, fake, plans, row, down=(), pages=None):
        """One pass; `down`: the entries of sites that do not answer in it (plan.unreachable). With `pages`
        ({chapter id: its page count; 20 for any other}) the resolve judges the copies of fractional chapters by
        their pages, as a real one does (resolver._prune_junk, with the page counts kept between passes)."""
        job = jobs.Job(1, "refresh-all", "all")
        settings._cache.clear()
        plain = resolver_for(fake, plans)

        class Counter:
            def page_count(self, cid):
                return pages.get(cid, 20)

        def resolve(client, series, **kw):
            plan = plain(client, series, **kw)
            plan.unreachable = [(m.source, "timed out") for m in down]
            if pages is not None:
                resolver._prune_junk(Counter(), plan, None, kw.get("counts"))
            return plan
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolve):
            web._run_pass(job, [row], "test")
        return job

    def enqueued(self, fake) -> list:
        return [c % 1000 / 10 for _, _, _, _, c in fake.kinds("enqueue")]

    def test_skipped_automatically_a_day_after_it_failed(self):
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        self.run_pass(fake, plans, row)                                    # the first pass to call it a side story
        st = self.status(row["id"])
        self.assertEqual({n: s for n, (s, _) in st.items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "failed", 4.0: "wanted", 5.0: "wanted"})
        with db.connect() as con:
            b = stuck.details(con, db.get_series(con, row["id"]))[0]
        self.assertEqual((b.verdict.confidence, stuck.why_not_auto(b, b.verdict)), (HIGH, "young"))
        self.later()
        before = len(self.enqueued(fake))
        with self.assertLogs("mangarr.stuck", "INFO") as cm:
            self.run_pass(fake, plans, row)                                # a day later: skipped before the downloads
        st = self.status(row["id"])
        self.assertEqual({n: s for n, (s, _) in st.items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})
        self.assertEqual(st[3.5][1], "skipped automatically: probably a side story (high confidence)")
        self.assertIn("INFO:mangarr.stuck:Freedom: ch 3.5 skipped automatically: probably a side story (high "
                      "confidence); 2 later chapter(s) waited for it", cm.output)
        self.assertEqual(self.enqueued(fake)[before:], [4.0, 5.0])
        with db.connect() as con:
            names = json.loads(con.execute("SELECT names FROM stuck").fetchone()[0])
        self.assertEqual(names, {X: "Side Story 1"})

    def test_a_blocker_an_earlier_pass_left_is_skipped_before_the_downloads(self):
        fake, plans, row = self.scenario()
        self.run_pass(fake, plans, row)                                    # automatic skipping off
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0]), ("failed", "wanted"))
        self.assertTrue(st[4.0][1].startswith("waiting for chapter 3.5: chapters download in order"))
        with db.connect() as con:
            blocked = stuck.blockers(con, row["id"])
            self.assertEqual([(b.number, b.waiting, b.names, b.urls) for b in blocked],
                             [(3.5, 2, {X: "Side Story 1"}, {X: "https://site1.example/chapter/1035"})])
            settings.set_many(con, {"auto_skip_side_stories": True})
        before = len(self.enqueued(fake))
        self.later()
        self.run_pass(fake, plans, row)
        self.assertEqual(self.enqueued(fake)[before:], [4.0, 5.0])         # 3.5 not tried again: skipped first
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})

    def test_one_series_refreshed_on_its_own(self):
        # a Search Missing (or the daemon): core.refresh_series with download, no lanes
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)), db.connect() as con:
            core.refresh_series(con, fake, row["id"], download=True)
            self.assertEqual(self.status(row["id"])[3.5][0], "failed")
            self.later()
            core.refresh_series(con, fake, row["id"], download=True)
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})

    def skip_events(self, sid) -> list:
        with db.connect() as con:
            return [r[0] for r in con.execute("SELECT message FROM event WHERE series_id=? AND kind='skip' ORDER BY id",
                                              (sid,))]

    def test_an_undone_automatic_skip_stays_undone(self):
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        self.run_pass(fake, plans, row)
        self.later()
        self.run_pass(fake, plans, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
        with db.connect() as con:
            self.assertTrue(stuck.unskip(con, row["id"], 3.5))
        for i in range(2):
            self.later()
            self.run_pass(fake, plans, row)
            self.assertEqual(self.status(row["id"])[3.5][0], "failed", i)
        self.assertEqual(self.skip_events(row["id"]), [
            "chapter 3.5 skipped automatically: probably a side story (high confidence); it held back 2 chapters",
            "chapter 3.5 un-skipped: wanted again"])

    def test_strict_order_holds_the_series_until_the_chapter_s_next_try(self):
        fake, plans, row = self.scenario()
        for i in range(3):
            self.run_pass(fake, plans, row)
            st = self.status(row["id"])
            self.assertEqual({n: s for n, (s, _) in st.items()}, {1.0: "have", 2.0: "have", 3.0: "have",
                                                                  3.5: "failed", 4.0: "wanted", 5.0: "wanted"}, i)
            self.assertEqual(st[4.0][1], downloader.waiting_reason(3.5))
            with db.connect() as con:
                self.assertEqual([(b.number, b.waiting) for b in stuck.blockers(con, row["id"])], [(3.5, 2)], i)
        with db.connect() as con:
            tries = con.execute("SELECT tries FROM chapter WHERE series_id=? AND number=3.5", (row["id"],)).fetchone()[0]
        self.assertEqual(tries, 2)                  # the third pass did not try it: its next try is a day off
        self.assertNotIn(4.0, self.enqueued(fake))

    def test_a_site_that_starts_listing_it_is_tried_before_it_is_skipped(self):
        fake, plans, row = self.scenario()
        self.run_pass(fake, plans, row)                                    # it fails on Site X
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [3.5])
        y.chapters[0].name = "Side Story 1"
        plans["Freedom"].append(y)
        with db.connect() as con:
            settings.set_many(con, {"auto_skip_side_stories": True})
        before = len(self.enqueued(fake))
        self.run_pass(fake, plans, row)
        self.assertIn(3.5, self.enqueued(fake)[before:])
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "have", 4.0: "have", 5.0: "have"})
        self.assertEqual(self.skip_events(row["id"]), [])

    def test_a_site_missing_from_one_pass_changes_nothing(self):
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [1, 2, 3, 3.5, 4, 5])
        next(c for c in y.chapters if c.number == 3.5).name = "Side Story 1"
        fake.broken.add(chapter_id(2, 3.5))
        both, only = {"Freedom": [plans["Freedom"][0], y]}, {"Freedom": [plans["Freedom"][0]]}
        self.run_pass(fake, both, row)
        self.later()
        self.run_pass(fake, both, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
        tried = self.enqueued(fake).count(3.5)
        self.run_pass(fake, only, row)                                     # Site Y did not answer
        self.later()
        self.run_pass(fake, both, row)
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})
        self.assertEqual(self.enqueued(fake).count(3.5), tried)            # never taken back and tried again
        self.assertEqual(len(self.skip_events(row["id"])), 1)

    def test_an_undo_outlasts_a_pass_in_which_the_site_missed_it(self):
        # Review: one pass in which the only site listing it answered without it made it 'unavailable', its stuck
        # row was deleted with the undo, and when the site listed it again it was skipped automatically again
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [1, 2, 3])
        both, only = {"Freedom": [plans["Freedom"][0], y]}, {"Freedom": [y]}
        self.run_pass(fake, both, row)
        self.later()
        self.run_pass(fake, both, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
        with db.connect() as con:
            self.assertTrue(stuck.unskip(con, row["id"], 3.5))
        self.run_pass(fake, both, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")
        self.run_pass(fake, only, row)                                     # Site X's search missed it
        st = self.status(row["id"])[3.5]
        self.assertEqual(st[0], "failed")                                   # still waited for, and says so
        self.assertTrue(st[1].endswith("; not listed by Site X (EN) in the last check; still waiting for it"), st[1])
        self.later(3)
        self.run_pass(fake, both, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")
        self.assertEqual(self.skip_events(row["id"]), [
            "chapter 3.5 skipped automatically: probably a side story (high confidence); it held back 2 chapters",
            "chapter 3.5 un-skipped: wanted again"])

    def test_keep_waiting_outlasts_a_pass_in_which_the_site_missed_it(self):
        fake, plans, row = self.scenario()
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [1, 2, 3])
        both, only = {"Freedom": [plans["Freedom"][0], y]}, {"Freedom": [y]}
        self.run_pass(fake, both, row)
        with db.connect() as con:
            self.assertTrue(stuck.dismiss(con, row["id"], 3.5))
            settings.set_many(con, {"auto_skip_side_stories": True})
        self.run_pass(fake, only, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")                # still waited for
        self.later(3)
        self.run_pass(fake, both, row)
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0]), ("failed", "wanted"))
        with db.connect() as con:
            b = stuck.blockers(con, row["id"])[0]
        self.assertEqual((b.number, b.keep_waiting, b.dismissed), (3.5, True, True))
        self.assertEqual(self.skip_events(row["id"]), [])

    def test_two_sites_naming_it_differently_and_a_pass_without_one(self):
        # Review: the row's name flipped with the site assigned, and each flip took the automatic skip back, tried
        # the chapter again and held the series back for another pass
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [1, 2, 3, 3.5, 4, 5])
        next(c for c in y.chapters if c.number == 3.5).name = "Side Story: Picnic"
        fake.broken.add(chapter_id(2, 3.5))
        both, only = {"Freedom": [plans["Freedom"][0], y]}, {"Freedom": [y]}
        self.run_pass(fake, both, row)
        self.later()
        self.run_pass(fake, both, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
        tried = self.enqueued(fake).count(3.5)
        self.run_pass(fake, only, row)
        with db.connect() as con:
            self.assertEqual(db.chapters_by_number(con, row["id"], [3.5])[3.5]["name"], "Side Story: Picnic")
        self.later()
        self.run_pass(fake, both, row)
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})
        self.assertEqual(self.enqueued(fake).count(3.5), tried)
        self.assertEqual(len(self.skip_events(row["id"])), 1)

    def test_not_a_word_the_only_listing_site_gives_every_chapter(self):
        # your chapters come from Site A ("Chapter N"); only Site X lists 3.5, and it names every chapter
        # "Freedom Gaiden Chapter N": that is the series' word, not a side story's
        fake, plans, row = self.scenario("Freedom Gaiden Chapter 3.5", auto_skip_side_stories=True)
        x = plans["Freedom"][0]
        for c in x.chapters:
            c.name = f"Freedom Gaiden Chapter {c.number:g}"
        a = entry(fake, "Site A (EN)", 3, "Freedom", [1, 2, 3, 4, 5, 6, 7])  # more chapters: ranked first
        self.run_pass(fake, {"Freedom": [x, a]}, row)
        st = self.status(row["id"])
        self.assertEqual((st[3.0][0], st[3.5][0], st[4.0][0]), ("have", "failed", "wanted"))
        with db.connect() as con:
            self.assertEqual(db.chapters_by_number(con, row["id"], [3.0])[3.0]["name"], "Chapter 3")
            blocked = stuck.details(con, db.get_series(con, row["id"]))
        self.assertEqual([(b.number, b.verdict.kind, b.verdict.confidence) for b in blocked], [(3.5, "unknown", "low")])
        self.assertEqual(self.skip_events(row["id"]), [])

    def test_an_uncertain_one_keeps_the_series_waiting(self):
        fake, plans, row = self.scenario("Bonus Chapter", auto_skip_side_stories=True)
        self.run_pass(fake, plans, row)
        with db.connect() as con:
            blocked = stuck.details(con, db.get_series(con, row["id"]))
        self.assertEqual([(b.number, b.verdict.kind, b.verdict.confidence) for b in blocked], [(3.5, "side_story",
                                                                                                "low")])
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")

    def site_y(self, fake, numbers=(1, 2, 3, 3.5, 4, 5), name="Chapter 3.5: The Duel"):
        """Site Y: lists 3.5 under a title of its own, and its copy is broken too."""
        y = entry(fake, "Site Y (EN)", 2, "Freedom", numbers)
        for c in y.chapters:
            if c.number == 3.5:
                c.name = name
                fake.broken.add(chapter_id(2, 3.5))
        return y

    def test_not_skipped_on_first_sight_while_the_site_with_its_title_does_not_answer(self):
        # Review: the pass that first failed 3.5 judged it from Site X's "Side Story 1" alone while Site Y, which
        # gives it a real title, did not answer; it was skipped at once and 4 and 5 downloaded past it
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        x, y = plans["Freedom"][0], self.site_y(fake)
        self.run_pass(fake, {"Freedom": [x]}, row, down=[y])
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
        self.later()
        self.run_pass(fake, {"Freedom": [x, y]}, row)                     # Y answers: its title
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
        with db.connect() as con:
            b = stuck.details(con, db.get_series(con, row["id"]))[0]
        self.assertEqual((b.names, b.verdict.auto_skip), ({X: "Side Story 1", "Site Y (EN)": "Chapter 3.5: The Duel"},
                                                          False))
        self.assertIsNone(b.high_since)
        self.assertEqual(self.skip_events(row["id"]), [])
        self.assertNotIn(4.0, self.enqueued(fake))

    def test_not_skipped_while_a_site_that_matched_the_series_does_not_answer(self):
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        x, y = plans["Freedom"][0], self.site_y(fake, (1, 2, 3, 4, 5))     # Y does not list 3.5 (yet)
        self.run_pass(fake, {"Freedom": [x, y]}, row)
        self.later()
        self.run_pass(fake, {"Freedom": [x]}, row, down=[y])               # Y may list it now: nothing is skipped
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")
        self.run_pass(fake, {"Freedom": [x, y]}, row)                     # it answers, still without it
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("ignored", "have", "have"))

    def test_the_evidence_outlasts_a_resolve_in_which_no_site_listed_it(self):
        # Review: one resolve in which no site listed 3.5 made it 'unavailable' and deleted its stuck row with
        # Site Y's real title; the next resolve, with only Site X's "Side Story 1", skipped it automatically
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        x, y = plans["Freedom"][0], self.site_y(fake)
        z = entry(fake, "Site Z (EN)", 3, "Freedom", [1, 2, 3])
        self.run_pass(fake, {"Freedom": [x, y]}, row)
        self.run_pass(fake, {"Freedom": [z]}, row)                         # X's and Y's searches missed it
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
        self.assertTrue(st[3.5][1].endswith("; not listed by Site X (EN) or Site Y (EN) in the last check; still "
                                            "waiting for it") or
                        st[3.5][1].endswith("; not listed by Site Y (EN) or Site X (EN) in the last check; still "
                                            "waiting for it"), st[3.5][1])
        with db.connect() as con:
            names = json.loads(con.execute("SELECT names FROM stuck").fetchone()[0])
        self.assertEqual(names, {X: "Side Story 1", "Site Y (EN)": "Chapter 3.5: The Duel"})
        self.later()
        self.run_pass(fake, {"Freedom": [x, z]}, row)                      # Y missed again
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
        self.assertEqual(self.skip_events(row["id"]), [])

    def test_strict_order_holds_when_the_blocker_s_only_site_misses_it(self):
        # Review: one resolve in which the only site listing the blocker missed the series made it 'unavailable',
        # and every later chapter another site listed downloaded in that pass: the gap strict order prevents, and
        # Keep waiting was stored while nothing waited
        fake, plans, row = self.scenario()
        x = plans["Freedom"][0]
        y = entry(fake, "Site Y (EN)", 2, "Freedom", [1, 2, 3, 4, 5, 6, 7])
        self.run_pass(fake, {"Freedom": [x, y]}, row)
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["failed", "wanted", "wanted", "wanted", "wanted"])
        with db.connect() as con:
            self.assertTrue(stuck.dismiss(con, row["id"], 3.5))
        before = len(self.enqueued(fake))
        self.run_pass(fake, {"Freedom": [y]}, row)                          # X's search missed the series
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["failed", "wanted", "wanted", "wanted", "wanted"])
        self.assertEqual(self.enqueued(fake)[before:], [])
        self.assertEqual([st[n][1] for n in (4, 5, 6, 7)], [downloader.waiting_reason(3.5)] * 4)
        self.assertTrue(st[3.5][1].endswith("; not listed by Site X (EN) in the last check; still waiting for it"))
        with db.connect() as con:
            self.assertEqual([(b.number, b.keep_waiting) for b in stuck.blockers(con, row["id"])], [(3.5, True)])
            self.assertEqual(db.chapters_due(con, True).get(row["id"], 0), 0)
        self.later(3)
        self.run_pass(fake, {"Freedom": [x, y]}, row)
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["failed", "wanted", "wanted", "wanted", "wanted"])
        with db.connect() as con:
            self.assertEqual([(b.number, b.keep_waiting) for b in stuck.blockers(con, row["id"])], [(3.5, True)])
        # gone from X for its whole grace: given up, and the series goes on
        for _ in range(db.LISTING_GRACE_RESOLVES):
            self.run_pass(fake, {"Freedom": [entry(fake, X, 1, "Freedom", [1, 2, 3]), y]}, row)
        self.later(db.LISTING_GRACE_DAYS + 0.1)
        self.run_pass(fake, {"Freedom": [entry(fake, X, 1, "Freedom", [1, 2, 3]), y]}, row)
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["unavailable", "have", "have", "have", "have"])


    # -- junk is a property of a site's copy (resolver._prune_junk), and a junk chapter is still listed --

    def site(self, fake, name, mid, numbers, title=None, broken=True):
        """Another site listing 3.5 under `title` (with `numbers`), its copy broken unless said otherwise."""
        m = entry(fake, name, mid, "Freedom", numbers)
        for c in m.chapters:
            if c.number == 3.5:
                c.name = title
                if broken:
                    fake.broken.add(chapter_id(mid, 3.5))
        return m

    @staticmethod
    def new_copy(*entries, when="2026-09-20"):
        """The sites put up a new copy of 3.5 (another upload date): its pages are counted again."""
        for m in entries:
            next(ch for ch in m.chapters if ch.number == 3.5).uploaded = when

    def listed(self, sid, n):
        with db.connect() as con:
            return db.listed_by(db.chapters_by_number(con, sid, [n])[n])

    def test_a_junk_resolve_keeps_every_site_that_lists_the_chapter(self):
        # Review: a resolve that dropped 3.5 as junk (its best copy had 4 pages) recorded only that site, not Site D,
        # which had just started to list it under a title of its own; the next pass, which D's search missed,
        # skipped 3.5 automatically as a side story from the other sites' "Side Story 1"
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        x, d = plans["Freedom"][0], entry(fake, "Site D (EN)", 3, "Freedom", [1, 2, 3, 4, 5])
        c = self.site(fake, "Site C (EN)", 2, [1, 2, 3, 3.5, 4, 5], "Side Story 1")
        self.run_pass(fake, {"Freedom": [x, c, d]}, row, pages={})         # 3.5 fails on X and C
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")
        self.later(0.6)
        d = self.site(fake, "Site D (EN)", 3, [1, 2, 3, 3.5, 4, 5], "Chapter 3.5: The Duel")
        self.new_copy(x, c)                                                # X and C put up short ones
        short = {chapter_id(1, 3.5): 4, chapter_id(2, 3.5): 3, chapter_id(3, 3.5): 2}
        self.run_pass(fake, {"Freedom": [x, c, d]}, row, pages=short)      # every copy short: junk this time
        self.assertEqual(self.status(row["id"])[3.5][0], "junk")
        self.assertEqual(set(self.listed(row["id"], 3.5)), {X, "Site C (EN)", "Site D (EN)"})
        with db.connect() as con:
            names = json.loads(con.execute("SELECT names FROM stuck WHERE number=3.5").fetchone()[0])
        self.assertEqual(names["Site D (EN)"], "Chapter 3.5: The Duel")
        self.later(0.6)
        c = self.site(fake, "Site C (EN)", 2, [1, 2, 3, 3.5, 4, 5, 6], "Side Story 1")     # ranked first now
        self.new_copy(c, when="2026-09-21")                                                # at full length
        self.run_pass(fake, {"Freedom": [x, c]}, row, pages={chapter_id(1, 3.5): 4})    # D's search missed it
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[6.0][0]), ("failed", "have", "wanted"))   # (4, 5: past junk)
        self.assertEqual(self.skip_events(row["id"]), [])
        with db.connect() as con:
            b = stuck.details(con, db.get_series(con, row["id"]))[0]
        self.assertEqual(b.names["Site D (EN)"], "Chapter 3.5: The Duel")
        self.assertFalse(b.verdict.auto_skip)
        self.assertEqual(set(b.short), {X, "Site D (EN)"})                 # never tried: not waited for
        self.assertEqual(b.untried, [])

    def test_what_you_decided_outlasts_a_week_as_junk(self):
        # Review: after a week as junk (its copies short while both sites still listed it) your un-skip was
        # dropped, and once a copy was long enough again the automatic skip took the chapter over it
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        x = plans["Freedom"][0]
        c = self.site(fake, "Site C (EN)", 2, [1, 2, 3, 3.5, 4, 5], "Side Story 1")
        both = {"Freedom": [x, c]}
        self.run_pass(fake, both, row, pages={})
        self.later(1.1)
        self.run_pass(fake, both, row, pages={})
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")        # skipped automatically
        with db.connect() as con:
            self.assertTrue(stuck.unskip(con, row["id"], 3.5))
        self.later(0.2)
        self.run_pass(fake, both, row, pages={})
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")
        short = {chapter_id(1, 3.5): 4, chapter_id(2, 3.5): 3}
        for i, days in enumerate((0.5, 4, 4, 4)):                          # junk for 12 days, still listed
            self.later(days)
            self.new_copy(x, c, when=f"2026-10-0{i + 1}")                  # a new copy each time: counted again
            self.run_pass(fake, both, row, pages=short)
            self.assertEqual(self.status(row["id"])[3.5][0], "junk", i)
            with db.connect() as con:
                k = con.execute("SELECT * FROM stuck_choice WHERE number=3.5").fetchone()
                evidence = con.execute("SELECT names FROM stuck WHERE number=3.5").fetchone()
            self.assertTrue(k["declined"], i)
            self.assertIsNone(k["gone_since"], i)
            self.assertEqual(set(json.loads(evidence[0])), {X, "Site C (EN)"}, i)
        self.later(0.5)
        self.new_copy(c, when="2026-10-20")                                # C's copy is whole again
        self.run_pass(fake, both, row, pages={chapter_id(1, 3.5): 4})
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")          # tried on C, never skipped again
        self.assertEqual(self.skip_events(row["id"]), [
            "chapter 3.5 skipped automatically: probably a side story (high confidence); it held back 2 chapters",
            "chapter 3.5 un-skipped: wanted again"])

    def test_a_short_best_copy_does_not_let_the_series_past_the_chapter(self):
        # Review: with its best-ranked copy a 4-page placeholder, 3.5 was junk though Site C had it at full length,
        # and strict order downloaded 4 and 5 past it; it could not be downloaded at all
        fake, plans, row = self.scenario("Chapter 3.5: The Duel")
        x = plans["Freedom"][0]
        c = self.site(fake, "Site C (EN)", 2, [3.5], "Chapter 3.5: The Duel")
        pages = {chapter_id(1, 3.5): 4}
        for _ in range(2):
            self.run_pass(fake, {"Freedom": [x, c]}, row, pages=pages)
            st = self.status(row["id"])
            self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
            self.later(1.1)
        self.assertNotIn(4.0, self.enqueued(fake))
        self.assertNotIn(chapter_id(1, 3.5), [e[4] for e in fake.kinds("enqueue")])   # the placeholder: never
        fake.broken.discard(chapter_id(2, 3.5))                                       # C's copy works again
        self.run_pass(fake, {"Freedom": [x, c]}, row, pages=pages)
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "have", 4.0: "have", 5.0: "have"})
        self.assertEqual(self.enqueued(fake)[-3:], [3.5, 4.0, 5.0])

    def test_not_junk_while_the_site_that_has_it_does_not_answer(self):
        fake, plans, row = self.scenario("Chapter 3.5: The Duel")
        x = plans["Freedom"][0]
        c = self.site(fake, "Site C (EN)", 2, [1, 2, 3, 3.5, 4, 5], "Chapter 3.5: The Duel")
        pages = {chapter_id(1, 3.5): 4}
        self.run_pass(fake, {"Freedom": [x, c]}, row, pages=pages)         # C has it in full; it fails there
        self.later(1.1)
        self.run_pass(fake, {"Freedom": [x]}, row, down=[c], pages=pages)  # only X's placeholder this time
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("failed", "wanted", "wanted"))
        self.assertTrue(st[3.5][1].endswith("; not listed by Site C (EN) in the last check; still waiting for it"),
                        st[3.5][1])
        self.assertNotIn(4.0, self.enqueued(fake))
        for _ in range(3):                                                 # C stays away for more than a week:
            self.later(3)                                                  # its listing no longer counts, and
            self.run_pass(fake, {"Freedom": [x]}, row, down=[c], pages=pages)     # 3.5 is junk
        st = self.status(row["id"])
        self.assertEqual((st[3.5][0], st[4.0][0], st[5.0][0]), ("junk", "have", "have"))

    def test_a_bad_file_for_a_skipped_chapter_leaves_it_skipped(self):
        # Review: an import that found a corrupt file of a chapter you skipped made it 'failed', and strict order
        # held the series behind it again
        fake, plans, row = self.scenario()
        self.run_pass(fake, plans, row)
        with db.connect() as con:
            self.assertTrue(stuck.skip(con, row["id"], 3.5, "manual"))
        self.run_pass(fake, plans, row)
        path = os.path.join(config.STAGING_ROOT, library.safe_title(X), "Freedom", "Chapter 3.5.cbz")
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04" + bytes(3000))
        os.utime(path, (time.time() - 3600, time.time() - 3600))
        self.run_pass(fake, plans, row)
        self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
        self.assertTrue(os.path.exists(path + ".corrupt") or not os.path.exists(path))   # still set aside
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT skipped FROM stuck_choice WHERE number=3.5").fetchone()[0], "manual")

    def take_back_at(self, fake, sid, n, at):
        """You un-skip chapter n when the pass queues chapter `at` (Site X's)."""
        queue = fake.enqueue

        def enqueue(ids):
            if chapter_id(1, at) in ids:
                with db.connect() as con:
                    self.assertTrue(stuck.unskip(con, sid, n))
            queue(ids)
        fake.enqueue = enqueue

    def test_a_chapter_taken_back_during_the_download_holds_the_ones_after_it(self):
        # Review: an un-skip of 3.5 while the lanes downloaded 4-7 did not stop them: a gap once 3.5 then failed
        fake, plans, row = self.scenario()
        m = entry(fake, X, 1, "Freedom", [1, 2, 3, 3.5, 4, 5, 6, 7])
        plans = {"Freedom": [m]}
        self.run_pass(fake, plans, row)
        with db.connect() as con:
            self.assertTrue(stuck.skip(con, row["id"], 3.5, "manual"))
        self.take_back_at(fake, row["id"], 3.5, 4)
        self.run_pass(fake, plans, row)
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["wanted", "have", "wanted", "wanted", "wanted"])
        self.assertEqual(st[5.0][1], downloader.taken_back_reason(3.5))
        self.assertEqual(self.enqueued(fake)[-1], 4.0)
        fake.broken.clear()
        self.run_pass(fake, plans, row)                                    # the next pass: 3.5 first
        self.assertEqual(self.enqueued(fake)[-4:], [3.5, 5.0, 6.0, 7.0])

    def test_a_chapter_taken_back_during_a_refresh_s_download_holds_the_ones_after_it(self):
        fake, plans, row = self.scenario()
        plans = {"Freedom": [entry(fake, X, 1, "Freedom", [1, 2, 3, 3.5, 4, 5, 6, 7])]}
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)), db.connect() as con:
            core.refresh_series(con, fake, row["id"], download=True)
            self.assertTrue(stuck.skip(con, row["id"], 3.5, "manual"))
            self.take_back_at(fake, row["id"], 3.5, 4)
            core.refresh_series(con, fake, row["id"], download=True)
        st = self.status(row["id"])
        self.assertEqual([st[n][0] for n in (3.5, 4, 5, 6, 7)], ["wanted", "have", "wanted", "wanted", "wanted"])
        self.assertEqual(st[7.0][1], downloader.taken_back_reason(3.5))


# -- the pages and the API --------------------------------------------------------------------

@unittest.skipIf(TestClient is None, "web extras not installed")
class PagesTest(WebBase, _Offline):
    def setUp(self):
        super().setUp()
        for p in self.offline():
            p.start()
            self.addCleanup(p.stop)
        with db.connect() as con:
            self.sid = seed_dangers(con)
            self.plain = db.upsert_series(con, Series(english="Plain Wanted"))
            add(con, self.plain, 1, "wanted", reason="available on X; not downloaded yet - waiting for a download pass")

    def page(self, path=None):
        r = self.client.get(path or f"/series/{self.sid}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.text

    def note(self, html) -> str:
        m = re.search(r'<div class="alert warning stuck-note".*?>Keep waiting</button></form>', html, re.S)
        return m.group(0) if m else ""

    def test_the_note_at_the_top(self):
        cache_md(DANGERS, 7.2, DANGERS_MD)
        html = self.page()
        note = self.note(html)
        self.assertTrue(note)
        self.assertLess(html.index("stuck-note"), html.index('class="series-header"'))
        for text in ("<b>Stuck behind chapter 7.2 - 20 chapters waiting.</b> Only Manganato lists it and its images "
                     "are gone (failed on every try since 2026-09-26).",
                     "<b>Probably already covered by chapter 7 you have</b>", "(high confidence)",
                     "<li>MangaDex&#39;s English chapter list has 7 and 7.5 but no 7.2; you have both.</li>",
                     "<li>Weeb Central, where your chapter 8 is from, does not list 7.2.</li>",
                     '<a class="external button" href="https://manganato.example/manga/ch-7.2" target="_blank" '
                     'rel="noopener noreferrer"', "Open 7.2 on Manganato",
                     f'action="/series/{self.sid}/chapter/7.2/skip"', ">Skip it</button>",
                     f'title="{views.SKIP_DISCLAIMER}"', f'<div class="popover-text">{views.SKIP_DISCLAIMER}</div>',
                     f'action="/series/{self.sid}/chapter/7.2/keep-waiting"', ">Keep waiting</button>"):
            self.assertIn(text, note)
        self.assertNotIn("checking MangaDex", html)
        self.assertNotIn("data-reload", html)
        row = re.search(r'<tr class="episode-row failed" data-number="7.2".*?</tr>', html, re.S).group(0)
        self.assertIn(">Covered by 7?</span>", row)
        self.assertIn('title="Holds back 20 chapters.\nProbably already covered by chapter 7 you have (high confidence)',
                      row)
        self.assertEqual(self.fetcher.pending(), 0)

    def test_checking_mangadex_until_the_answer_is_in(self):
        self.md[DANGERS.ref] = DANGERS_MD
        html = self.page()
        self.assertIn("checking MangaDex... (this page reloads by itself)", html)
        self.assertIn('<main id="main" class="content" data-reload="15000">', html)
        self.assertIn("Unknown: no clear sign either way", self.note(html))
        self.assertEqual((self.fetcher.pending(), self.lookups), (1, []))     # the page itself never asks
        self.fetcher.start()
        self.assertTrue(self.fetcher.wait_idle(10))
        html = self.page()
        self.assertNotIn("checking MangaDex", html)
        self.assertNotIn("data-reload", html)
        self.assertIn("<b>Probably already covered by chapter 7 you have</b>", self.note(html))

    def test_mangadex_not_answering_is_said(self):
        cache_md(DANGERS, 7.2, None)
        with db.connect() as con:
            settings.set_many(con, {"auto_skip_side_stories": True})
        settings._cache.clear()
        html = self.page()
        self.assertIn("MangaDex did not answer, so its chapter list is not in this verdict yet: it is asked again "
                      "after ", self.note(html))
        self.assertNotIn("checking MangaDex", html)
        self.assertNotIn("data-reload", html)
        self.assertEqual(self.fetcher.pending(), 0)
        # a certain side story from its name alone: the note does not promise a skip no pass makes
        with db.connect() as con:
            sid = seed_freedom(con)
        cache_md(FREEDOM, 171.01, None)
        note = self.note(self.page(f"/series/{sid}"))
        self.assertIn("<b>Probably a side story</b> <span class=\"muted\">(high confidence: not skipped automatically "
                      "while MangaDex does not answer: its chapter list may change this verdict)</span>", note)
        self.assertNotIn("the next pass skips it", note)
        self.assertIn("MangaDex did not answer", note)

    def test_a_link_from_another_site_looks_nothing_up(self):
        for path in (f"/series/{self.sid}", "/wanted", f"/api/v1/series/{self.sid}"):
            with self.subTest(path):
                r = self.client.get(path, headers={"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("checking MangaDex", r.text)
                self.assertEqual(self.fetcher.pending(), 0)
        self.assertIn("checking MangaDex", self.page())
        self.assertEqual(self.fetcher.pending(), 1)

    def test_skip_it_and_un_skip(self):
        cache_md(DANGERS, 7.2, DANGERS_MD)
        r = self.client.post(f"/series/{self.sid}/chapter/7.2/skip", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("chapter%207.2%20skipped", r.headers["location"])
        html = self.page()
        self.assertEqual(self.note(html), "")
        row = re.search(r'<tr class="episode-row ignored" data-number="7.2".*?</tr>', html, re.S).group(0)
        self.assertIn(">Skipped</span>", row)
        self.assertIn(f'action="/series/{self.sid}/chapter/7.2/unskip"', row)
        self.assertIn(">Un-skip</button>", row)
        self.assertIn("chapter 7.2 skipped (verdict: probably already covered by chapter 7 you have); it held back 20 "
                      "chapters", html)
        r = self.client.post(f"/series/{self.sid}/chapter/7.2/unskip", follow_redirects=False)
        self.assertIn("un-skipped", r.headers["location"])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT status FROM chapter WHERE series_id=? AND number=7.2",
                                         (self.sid,)).fetchone()[0], "wanted")
        self.assertIn("chapter 7.2 un-skipped: wanted again", self.page())

    def test_keep_waiting(self):
        r = self.client.post(f"/series/{self.sid}/chapter/7.2/keep-waiting", follow_redirects=False)
        self.assertIn("the%20series%20keeps%20waiting%20for%20chapter%207.2", r.headers["location"])
        html = self.page()
        self.assertEqual(self.note(html), "")
        self.assertIn(">Unknown</span>", re.search(r'data-number="7.2".*?</tr>', html, re.S).group(0))  # still labelled
        self.assertEqual(self.note(self.page()), "")                               # kept
        with db.connect() as con:
            names_of(con, self.sid, 7.2, {MN: "Chapter 7.2", "Bato": "Chapter 7.2"})   # another site lists it now
        self.assertIn("Only Manganato and Bato list it", self.note(self.page()))

    def test_what_the_automatic_skip_will_do_is_said(self):
        cache_md(DANGERS, 7.2, DANGERS_MD)
        with db.connect() as con:
            settings.set_many(con, {"auto_skip_side_stories": True})
        settings._cache.clear()
        note = self.note(self.page())
        self.assertIn("(high confidence: skipped automatically once passes have judged it so for a day, in a pass in "
                      "which every site that lists it answers, unless you keep waiting)", note)
        with db.connect() as con:
            StuckBase.ripe(con, self.sid, 7.2)
        note = self.note(self.page())
        self.assertIn("(high confidence: the next pass in which every site that lists it answers skips it "
                      "automatically, unless you keep waiting)", note)
        self.assertIn('title="Fold this note away and keep waiting for the chapter: the note comes back if the chapter '
                      'changes (another title, or another site lists it), and it is never skipped automatically">Keep '
                      'waiting', note)
        with db.connect() as con:
            names_of(con, self.sid, 7.2, {MN: "Chapter 7.2", "Bato": "Chapter 7.2"}, failed_on=[MN])
        self.assertIn("(high confidence: skipped automatically once a pass has tried it on Bato too)",
                      self.note(self.page()))
        with db.connect() as con:
            con.execute("INSERT INTO stuck_choice (series_id, number, declined, updated_at) VALUES (?, 7.2, ?, ?)",
                        (self.sid, db.now(), db.now()))
        self.assertIn("(high confidence: not skipped automatically: you un-skipped it or wanted it again)",
                      self.note(self.page()))
        self.client.post(f"/series/{self.sid}/chapter/7.2/keep-waiting")
        self.assertIn("not skipped automatically: you un-skipped it", self.page())
        with db.connect() as con:
            con.execute("UPDATE stuck_choice SET declined=NULL")
        self.assertIn("(high confidence: not skipped automatically: you chose to keep waiting for it)", self.page())

    def test_keep_waiting_folds_the_note(self):
        self.client.post(f"/series/{self.sid}/chapter/7.2/keep-waiting")
        html = self.page()                                  # MangaDex not looked up yet: checking
        folded = re.search(r'<details class="alert info compact stuck-note stuck-dismissed" id="stuck-7.2">.*?'
                           r'</details>\s*</div>\s*</details>', html, re.S)
        self.assertTrue(folded, html[:3000])
        folded = folded.group(0)
        self.assertIn('Stuck behind chapter 7.2 - 20 chapters waiting. <span class="muted">You keep waiting for it',
                      folded)
        self.assertIn(f'action="/series/{self.sid}/chapter/7.2/skip"', folded)     # Skip it and its (i) stay
        self.assertIn(f'<div class="popover-text">{views.SKIP_DISCLAIMER}</div>', folded)
        self.assertNotIn("keep-waiting", folded)
        self.assertNotIn("data-reload", html)                # nothing on the page says it is checking
        self.assertNotIn("checking MangaDex", html)
        row = re.search(r'<tr class="episode-row failed" data-number="7.2".*?</tr>', html, re.S).group(0)
        self.assertIn(f'action="/series/{self.sid}/chapter/7.2/skip"', row)       # a skip, not a plain ignore
        self.assertNotIn("/ignore", row)
        self.assertIn(f'<a href="/series/{self.sid}#stuck-7.2"', self.page("/wanted"))
        self.assertIn("(20 waiting; you keep waiting)", self.page("/wanted"))

    def test_a_skip_has_its_un_skip_at_the_top_and_its_row_shown(self):
        html = self.page()
        self.assertIn('<div id="group-block-1" class="season-episodes" >', html)    # the blocker's group is open
        self.client.post(f"/series/{self.sid}/chapter/7.2/skip")
        html = self.page()
        top = (f'<div class="alert info compact stuck-skipped" id="skipped-7.2">Chapter 7.2 skipped: the chapters '
               f'after it download without it - <form method="post" action="/series/{self.sid}/chapter/7.2/unskip" '
               f'class="inline"><button class="linkbtn" type="submit">Un-skip</button></form></div>')
        self.assertIn(top, html)
        self.assertLess(html.index(top), html.index('class="series-header"'))
        self.assertIn('<div id="group-block-1" class="season-episodes" >', html)
        self.client.post(f"/series/{self.sid}/chapter/7.2/unskip")
        self.assertNotIn("stuck-skipped", self.page())

    def test_keep_it_skipped(self):
        self.client.post(f"/series/{self.sid}/chapter/7.2/skip")
        with db.connect() as con:
            names_of(con, self.sid, 7.2, {MN: "The Duel"})
        html = self.page()
        self.assertIn(f'action="/series/{self.sid}/chapter/7.2/keep-skipped"', html)
        r = self.client.post(f"/series/{self.sid}/chapter/7.2/keep-skipped", follow_redirects=False)
        self.assertIn("chapter%207.2%20stays%20skipped", r.headers["location"])
        html = self.page()
        self.assertNotIn("listed differently now", html)
        self.assertIn("Chapter 7.2 skipped: the chapters after it download without it", html)
        self.assertEqual(self.client.post(f"/api/v1/series/{self.sid}/chapter/7.2/keep-skipped").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/series/{self.sid}/chapter/3/keep-skipped").status_code, 409)

    def test_skipped_automatically_with_an_undo(self):
        with db.connect() as con:
            sid = seed_freedom(con)
            cache_md(FREEDOM, 171.01, ChapterList(None))
            st = stuck.details(con, db.get_series(con, sid))[0]
            stuck.skip(con, sid, 171.01, "auto", st.verdict, st.waiting)
        html = self.page(f"/series/{sid}")
        self.assertIn(f'<div class="alert info compact stuck-skipped" id="skipped-171.01">Chapter 171.01 skipped '
                      f'automatically: probably a '
                      f'side story - <form method="post" action="/series/{sid}/chapter/171.01/unskip" class="inline">'
                      f'<button class="linkbtn" type="submit">undo</button></form></div>', html)
        row = re.search(r'<tr class="episode-row ignored" data-number="171.01".*?</tr>', html, re.S).group(0)
        self.assertIn('title="Skipped automatically: Probably a side story">automatically</span>', row)
        self.client.post(f"/series/{sid}/chapter/171.01/unskip")
        self.assertNotIn("stuck-skipped", self.page(f"/series/{sid}"))

    def test_a_skipped_chapter_that_changed_is_offered_back(self):
        self.client.post(f"/series/{self.sid}/chapter/7.2/skip")
        with db.connect() as con:
            names_of(con, self.sid, 7.2, {MN: "The Duel"})
        html = self.page()
        self.assertIn("Chapter 7.2, which you skipped, is listed differently now (Manganato: &#34;The Duel&#34;): it "
                      "may be a chapter of the story after all.", html)
        self.assertIn('class="alert warning compact stuck-skipped"', html)

    def test_wanted_stuck_filter(self):
        cache_md(DANGERS, 7.2, DANGERS_MD)
        html = self.page("/wanted")
        self.assertIn('data-value="stuck" aria-checked="false"><span>Stuck (1)</span>', html)
        self.assertIn(f'<tr data-id="{self.sid}" data-failed="1" data-stuck="1" >', html)
        self.assertIn(f'<tr data-id="{self.plain}" data-failed="0" data-stuck="0" >', html)
        self.assertIn(f'<a href="/series/{self.sid}#stuck-7.2"', html)
        self.assertIn("(20 waiting)", html)
        # judged only for the Stuck filter: that reads every chapter of every stuck series
        self.assertIn('id="missing-table" data-verdicts="0"', html)
        self.assertNotIn(">Covered by 7?</span>", html)
        with mock.patch.object(stuck, "judge", side_effect=AssertionError("judged")):
            self.page("/wanted")
        html = self.page("/wanted?filter=stuck")
        self.assertIn('id="missing-table" data-verdicts="1"', html)
        self.assertIn(">Covered by 7?</span>", html)
        self.assertIn('data-value="stuck" aria-checked="true"', html)
        self.assertIn(f'<tr data-id="{self.plain}" data-failed="0" data-stuck="0" hidden>', html)
        self.assertIn(f'<tr data-id="{self.sid}" data-failed="1" data-stuck="1" >', html)

    def test_settings_download_order(self):
        html = self.page("/settings")
        self.assertIn(">Download Order</label>", html)
        self.assertNotIn(">Download In Order</label>", html)
        self.assertIn('id="download_in_order" checked data-hides="order-warning" data-enables="auto-skip-option"',
                      html)
        self.assertIn("Strict download in order</label>", html)
        self.assertRegex(html, r'id="auto_skip_side_stories"\s+data-shows="auto-skip-warning">')
        self.assertIn("Automatically skip likely side stories that block downloads</label>", html)
        self.assertIn('id="auto-skip-warning" hidden>Chapters judged to be side stories or already-covered pieces are '
                      "skipped automatically when they block a series. The judgement is a best guess from chapter "
                      "titles and other sites' chapter lists; you might miss parts of the story.</div>", html)
        self.assertIn('<div class="help-text text-warning" data-when-off hidden>Only with strict download in order',
                      html)
        self.client.post("/settings", data={"download_in_order": ["0", "1"], "auto_skip_side_stories": ["0", "1"]})
        settings._cache.clear()
        self.assertTrue(settings.get("auto_skip_side_stories"))
        html = self.page("/settings")
        self.assertIn('id="auto_skip_side_stories" checked data-shows="auto-skip-warning">', html)
        self.assertIn('id="auto-skip-warning" >', html)
        # strict order off: the option is greyed out and not sent, so it keeps its value
        self.client.post("/settings", data={"download_in_order": "0"})
        settings._cache.clear()
        self.assertEqual((settings.get("download_in_order"), settings.get("auto_skip_side_stories")), (False, True))
        html = self.page("/settings")
        self.assertIn('<div class="order-option off" id="auto-skip-option"><input type="hidden" '
                      'name="auto_skip_side_stories" value="0" disabled>', html)
        self.assertRegex(html, r'id="auto_skip_side_stories"\s+checked data-shows="auto-skip-warning" '
                               r'disabled="disabled">')
        self.assertIn('<div class="help-text text-warning" data-when-off >Only with strict download in order', html)
        self.assertIn('id="order-warning" >Off: chapters are fetched from whichever source has them', html)
        self.assertIn('id="auto-skip-warning" hidden>', html)          # nothing is skipped: no warning

    def test_api(self):
        cache_md(DANGERS, 7.2, DANGERS_MD)
        d = self.client.get(f"/api/v1/series/{self.sid}").json()
        self.assertEqual(d["skipped"], [])
        st = d["stuck"][0]
        self.assertEqual({k: st[k] for k in ("number", "waiting", "status", "tries", "failedSince", "sources", "urls",
                                             "dismissed", "checkingMangaDex")},
                         {"number": 7.2, "waiting": 20, "status": "failed", "tries": 2, "failedSince": SINCE,
                          "sources": {MN: "Chapter 7.2"}, "urls": {MN: "https://manganato.example/manga/ch-7.2"},
                          "dismissed": False, "checkingMangaDex": False})
        self.assertEqual((st["failedOn"], st["declined"]), ([MN], False))
        self.assertEqual({k: st["verdict"][k] for k in ("kind", "confidence", "skippable", "autoSkip")},
                         {"kind": "covered", "confidence": "high", "skippable": True, "autoSkip": True})
        self.assertIn("MangaDex's English chapter list has 7 and 7.5 but no 7.2; you have both.",
                      st["verdict"]["evidence"])
        self.assertEqual(self.client.post(f"/api/v1/series/{self.sid}/chapter/7.2/keep-waiting").status_code, 200)
        self.assertTrue(self.client.get(f"/api/v1/series/{self.sid}").json()["stuck"][0]["dismissed"])
        r = self.client.post(f"/api/v1/series/{self.sid}/chapter/7.2/skip")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "ignored"))
        d = self.client.get(f"/api/v1/series/{self.sid}").json()
        self.assertEqual((d["stuck"], [k["number"] for k in d["skipped"]]), ([], [7.2]))
        for path, code in (("7.2/skip", 409), ("3/skip", 409), ("3/unskip", 409), ("99/skip", 404),
                           ("7.2/keep-waiting", 409)):
            with self.subTest(path):
                self.assertEqual(self.client.post(f"/api/v1/series/{self.sid}/chapter/{path}").status_code, code)
        self.assertEqual(self.client.post("/api/v1/series/9999/chapter/1/skip").status_code, 404)
        r = self.client.post(f"/api/v1/series/{self.sid}/chapter/7.2/unskip")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "wanted"))

    def test_the_want_button_forgets_a_skip(self):
        self.client.post(f"/series/{self.sid}/chapter/7.2/skip")
        self.client.post(f"/series/{self.sid}/chapter/7.2/unignore")
        with db.connect() as con:
            self.assertEqual(stuck.skipped(con, self.sid), [])
            self.assertIsNone(con.execute("SELECT skipped FROM stuck_choice").fetchone()[0])
            self.assertIsNotNone(con.execute("SELECT declined FROM stuck_choice").fetchone()[0])


@unittest.skipIf(views is None, "web extras not installed")
class NoteTextTest(unittest.TestCase):
    def st(self, **kw):
        base = dict(series_id=1, number=7.2, waiting=372, status="failed", name=None, source=MN, reason=GONE, tries=3,
                    failed_since=SINCE, names={MN: "Chapter 7.2"})
        return stuck.Stuck(**{**base, **kw})

    def test_the_spec_s_wording(self):
        self.assertEqual(views.stuck_note(self.st()),
                         {"head": "Stuck behind chapter 7.2 - 372 chapters waiting.",
                          "body": "Only Manganato lists it and its images are gone (failed on every try since "
                                  "2026-09-26)."})

    def test_other_cases(self):
        note = views.stuck_note(self.st(waiting=1, reason="Bato: download made no progress", tries=1,
                                        names={MN: None, "Bato": "x"}))
        self.assertEqual(note, {"head": "Stuck behind chapter 7.2 - 1 chapter waiting.",
                                "body": "Only Manganato and Bato list it and it could not be downloaded (failed on "
                                        "2026-09-26)."})
        # before a resolve saw which sites list it (a blocker an earlier version left): from its row
        note = views.stuck_note(self.st(names={}, failed_since=None))
        self.assertEqual(note["body"], "Only Manganato lists it and its images are gone (failed on every try).")
        note = views.stuck_note(self.st(names={}, reason="Manganato: the source has no working pages; Bato: x"))
        self.assertEqual(note["body"], "It failed on Manganato and every other source that lists it: its images are "
                                       "gone (failed on every try since 2026-09-26).")
        note = views.stuck_note(self.st(names={}, source=None, failed_since=None))
        self.assertEqual(note["body"], "It failed on every source that lists it (failed on every try).")
        many = {f"Site {i}": None for i in range(6)}
        self.assertIn("Only Site 0, Site 1, Site 2, Site 3 and 2 more list it", views.stuck_note(self.st(names=many))["body"])


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
