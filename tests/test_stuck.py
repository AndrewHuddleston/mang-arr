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
    WRONG,
)

from mangarr import core, db, downloader, jobs, library, mangadex, settings, stuck  # noqa: E402
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


def names_of(con, sid, number, names, urls=None):
    con.execute("INSERT INTO stuck (series_id, number, names, urls, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(series_id, number) DO UPDATE SET names=excluded.names, urls=excluded.urls",
                (sid, number, json.dumps(names), json.dumps(urls or {}), db.now()))


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

    def test_migration_14_takes_it_from_the_first_failure_in_the_history(self):
        path = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        db.migrate(con, 13)
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
        from mangarr.suwayomi import SuwayomiError
        client = _Client({100: "https://manganato.example/ch-7.2", 101: "javascript:alert(1)",
                          102: SuwayomiError("Cannot query field \"realUrl\""), 103: "https://bato.example/7.2"})
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
            con.execute("UPDATE chapter SET name='The Duel' WHERE series_id=? AND number=7.2", (sid,))
            self.assertFalse(stuck.blockers(con, sid)[0].dismissed)          # renamed

    def test_names_learnt_after_keep_waiting_are_no_change(self):
        with db.connect() as con:
            sid = seed_dangers(con)
            con.execute("DELETE FROM stuck")
            stuck.dismiss(con, sid, 7.2)                                     # no resolve has seen it yet
            stuck.update(con, None, sid, DANGERS, _Plan({7.2: [_Match(MN, [_Chapter(1, 7.2, "Chapter 7.2")])]}))
            self.assertTrue(stuck.blockers(con, sid)[0].dismissed)


# -- the automatic skip ------------------------------------------------------------------

class AutoSkipTest(StuckBase):
    def run_update(self, series, sid, found=None):
        with db.connect() as con, self.assertNoLogs("mangarr.stuck", "WARNING"):
            stuck.update(con, None, sid, series, None, found)

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
            stuck.update(con, None, sid, FREEDOM, None)
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
        for label, series, blocker, rows, md, _ in FALSE_POSITIVES:
            with self.subTest(label), db.connect() as con:
                for r in db.series_rows(con):
                    db.delete_series(con, r["id"])
                sid = db.upsert_series(con, series)
                for c in rows:
                    add(con, sid, c.number, c.status, c.name, c.source, c.pages)
                con.commit()
                cache_md(series, blocker.number, md if md is not None else ChapterList(None))
                st = stuck.Stuck(sid, float(blocker.number), 1, "failed", None, MN, "boom", 1, None, dict(blocker.names))
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
                    stuck.update(con, None, sid, series, None, blocked)
                self.assertEqual(self.chapter(sid, n)["status"], "ignored")

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
    and the next one."""

    def setUp(self):
        super().setUp()
        for p in self.offline():
            p.start()
            self.addCleanup(p.stop)

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

    def run_pass(self, fake, plans, row):
        job = jobs.Job(1, "refresh-all", "all")
        settings._cache.clear()
        with mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolver_for(fake, plans)):
            web._run_pass(job, [row], "test")
        return job

    def enqueued(self, fake) -> list:
        return [c % 1000 / 10 for _, _, _, _, c in fake.kinds("enqueue")]

    def test_skipped_automatically_right_after_it_failed(self):
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        with self.assertLogs("mangarr.stuck", "INFO") as cm:
            self.run_pass(fake, plans, row)
        st = self.status(row["id"])
        self.assertEqual({n: s for n, (s, _) in st.items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "wanted", 5.0: "wanted"})
        self.assertEqual(st[3.5][1], "skipped automatically: probably a side story (high confidence)")
        self.assertIn("INFO:mangarr.stuck:Freedom: ch 3.5 skipped automatically: probably a side story (high "
                      "confidence); 2 later chapter(s) waited for it", cm.output)
        with db.connect() as con:
            names = json.loads(con.execute("SELECT names FROM stuck").fetchone()[0])
        self.assertEqual(names, {X: "Side Story 1"})
        before = len(self.enqueued(fake))
        self.run_pass(fake, plans, row)
        self.assertEqual(self.enqueued(fake)[before:], [4.0, 5.0])
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})

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
        self.run_pass(fake, plans, row)
        self.assertEqual(self.enqueued(fake)[before:], [4.0, 5.0])         # 3.5 not tried again: skipped first
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})

    def test_one_series_refreshed_on_its_own(self):
        # a Search Missing (or the daemon): core.refresh_series with download, no lanes
        fake, plans, row = self.scenario(auto_skip_side_stories=True)
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)), db.connect() as con:
            core.refresh_series(con, fake, row["id"], download=True)
            self.assertEqual(self.status(row["id"])[3.5][0], "ignored")
            core.refresh_series(con, fake, row["id"], download=True)
        self.assertEqual({n: s for n, (s, _) in self.status(row["id"]).items()},
                         {1.0: "have", 2.0: "have", 3.0: "have", 3.5: "ignored", 4.0: "have", 5.0: "have"})

    def test_an_uncertain_one_keeps_the_series_waiting(self):
        fake, plans, row = self.scenario("Bonus Chapter", auto_skip_side_stories=True)
        self.run_pass(fake, plans, row)
        with db.connect() as con:
            blocked = stuck.details(con, db.get_series(con, row["id"]))
        self.assertEqual([(b.number, b.verdict.kind, b.verdict.confidence) for b in blocked], [(3.5, "side_story",
                                                                                                "low")])
        self.assertEqual(self.status(row["id"])[3.5][0], "failed")


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
        self.assertIn("keeping%20waiting", r.headers["location"])
        html = self.page()
        self.assertEqual(self.note(html), "")
        self.assertIn(">Unknown</span>", re.search(r'data-number="7.2".*?</tr>', html, re.S).group(0))  # still labelled
        self.assertEqual(self.note(self.page()), "")                               # kept
        with db.connect() as con:
            names_of(con, self.sid, 7.2, {MN: "Chapter 7.2", "Bato": "Chapter 7.2"})   # another site lists it now
        self.assertIn("Only Manganato and Bato list it", self.note(self.page()))

    def test_skipped_automatically_with_an_undo(self):
        with db.connect() as con:
            sid = seed_freedom(con)
            cache_md(FREEDOM, 171.01, ChapterList(None))
            st = stuck.details(con, db.get_series(con, sid))[0]
            stuck.skip(con, sid, 171.01, "auto", st.verdict, st.waiting)
        html = self.page(f"/series/{sid}")
        self.assertIn(f'<div class="alert info compact stuck-skipped">Chapter 171.01 skipped automatically: probably a '
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
        self.assertIn(">Covered by 7?</span>", html)
        html = self.page("/wanted?filter=stuck")
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
            self.assertIsNone(con.execute("SELECT skipped FROM stuck").fetchone()[0])


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
        note = views.stuck_note(self.st(names={}, failed_since=None))
        self.assertEqual(note["body"], "It failed on every source that lists it (failed on every try).")
        many = {f"Site {i}": None for i in range(6)}
        self.assertIn("Only Site 0, Site 1, Site 2, Site 3 and 2 more list it", views.stuck_note(self.st(names=many))["body"])


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
