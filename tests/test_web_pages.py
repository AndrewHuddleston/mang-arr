"""Every page renders (200) against a seeded temporary database, with no
Suwayomi and no network; plus the chapter grouping the series page uses."""
import os
import tempfile
import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = None

from mangarr import model
from mangarr.web import views


def _row(number, status, name=None, uploaded=None, reason=None):
    return {"number": number, "status": status, "name": name, "uploaded": uploaded, "reason": reason}


class GroupChaptersTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(views.group_chapters([]), [])

    def test_blocks_of_twenty_newest_first_and_open(self):
        rows = [_row(n, "have") for n in range(1, 46)] + [_row(12.5, "wanted"), _row(46, "wanted")]
        groups = views.group_chapters(rows)
        self.assertEqual([g["name"] for g in groups], ["Chapters 41-60", "Chapters 21-40", "Chapters 1-20"])
        self.assertEqual([g["open"] for g in groups], [True, False, False])
        first = groups[0]
        self.assertEqual((first["have"], first["total"], first["wanted"]), (5, 6, 1))
        self.assertEqual([c["number"] for c in first["chapters"]], [46, 45, 44, 43, 42, 41])
        block1 = groups[2]
        self.assertIn(12.5, [c["number"] for c in block1["chapters"]])          # fractional joins its integer
        self.assertEqual(block1["chapters"][0]["number"], 20)                     # newest first inside a group
        self.assertEqual(block1["pct"], round(100 * 20 / 21))

    def test_chapter_zero_lands_in_first_block(self):
        groups = views.group_chapters([_row(0, "have"), _row(0.5, "junk"), _row(1, "have")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "Chapters 1-20")
        self.assertEqual((groups[0]["have"], groups[0]["total"]), (2, 2))          # junk is not counted

    def test_seasons_when_every_named_chapter_is_season_numbered(self):
        rows = [_row(1, "have", "S1 - Episode 1"), _row(2, "have", "S1 - Episode 2"),
                _row(3, "wanted", "S2 - Episode 1"), _row(4, "failed", "S2 - Episode 2"), _row(5, "have")]
        groups = views.group_chapters(rows)
        self.assertEqual([g["name"] for g in groups], ["Season 2", "Season 1", "Unnumbered"])
        self.assertEqual([g["open"] for g in groups], [True, False, False])
        self.assertEqual((groups[0]["have"], groups[0]["total"], groups[0]["wanted"]), (0, 2, 2))
        self.assertEqual([c["number"] for c in groups[2]["chapters"]], [5])

    def test_one_plain_name_means_blocks(self):
        rows = [_row(1, "have", "S1 - Episode 1"), _row(2, "have", "Chapter 2")]
        groups = views.group_chapters(rows)
        self.assertEqual([g["name"] for g in groups], ["Chapters 1-20"])

    def test_sqlite_rows_work_too(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE chapter (number REAL, status TEXT, name TEXT)")
        con.executemany("INSERT INTO chapter VALUES (?,?,?)", [(1, "have", None), (25, "wanted", "Ch. 25")])
        groups = views.group_chapters(con.execute("SELECT * FROM chapter").fetchall())
        self.assertEqual([g["name"] for g in groups], ["Chapters 21-40", "Chapters 1-20"])


class HelpersTest(unittest.TestCase):
    def test_status_label(self):
        self.assertEqual(views.status_label("RELEASING"), "Continuing")
        self.assertEqual(views.status_label("FINISHED"), "Ended")
        self.assertEqual(views.status_label("HIATUS"), "Hiatus")
        self.assertEqual(views.status_label("CANCELLED"), "Cancelled")
        self.assertEqual(views.status_label(None), "Unknown")
        self.assertEqual(views.status_class("finished"), "ended")

    def test_plain_description(self):
        self.assertEqual(views.plain_description("Hello <i>world</i>.<br><br>Second &amp; last."),
                         "Hello world.\n\nSecond & last.")
        self.assertEqual(views.plain_description(None), "")

    def test_human_size_and_ref_url(self):
        self.assertEqual(views.human_size(0), "0 B")
        self.assertEqual(views.human_size(1536), "1.5 KB")
        self.assertEqual(views.human_size(1.2 * 1024 ** 3), "1.2 GB")
        self.assertEqual(views.ref_url({"anilist_id": 5, "mangadex_id": None}), "https://anilist.co/manga/5")
        self.assertEqual(views.ref_url({"anilist_id": None, "mangadex_id": "u-u"}), "https://mangadex.org/title/u-u")
        self.assertIsNone(views.ref_url({"anilist_id": None, "mangadex_id": None}))

    def test_nav(self):
        self.assertEqual(views.nav_section("/series/3"), "Series")
        self.assertEqual(views.nav_current("/series/3"), "/")
        self.assertEqual(views.nav_section("/activity/history"), "Activity")
        self.assertEqual(views.nav_current("/activity/history"), "/activity/history")
        self.assertEqual(views.nav_current("/activity"), "/activity")
        self.assertEqual(views.nav_current("/system/logs"), "/system/logs")
        self.assertEqual(views.nav_current("/system"), "/system")
        self.assertEqual(views.nav_current("/lists"), "/lists")


class _FakeSource:
    def __init__(self, name):
        self.id, self.name, self.lang, self.unusable, self.throttled = "1", name, "en", False, False


def _fake_gq(query, **kw):
    if "downloadStatus" in query:
        return {"downloadStatus": {"state": "STARTED", "queue": [
            {"state": "DOWNLOADING", "progress": 0.4, "tries": 1, "manga": {"title": "T"}, "chapter": {"name": "Ch 3"}}]}}
    return {"aboutServer": {"version": "test"}, "sources": {"totalCount": 1}}


@unittest.skipIf(TestClient is None, "web extras not installed")
class PagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        log_file = os.path.join(self.tmp.name, "mangarr.log")
        with open(log_file, "w") as f:
            f.write("2026-01-01 00:00:00 INFO    mangarr.core: hello\n2026-01-01 00:00:01 WARNING mangarr.core: careful\n")
        from mangarr import health
        self.patches = [mock.patch("mangarr.config.DATA_DIR", self.tmp.name),
                        mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch("mangarr.config.STAGING_ROOT", self.tmp.name),
                        mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp.name),
                        mock.patch("mangarr.config.LOG_FILE", log_file),
                        mock.patch("mangarr.web.app.client.sources", lambda *a, **k: [_FakeSource("Weeb Central")]),
                        mock.patch("mangarr.web.app.client.gq", _fake_gq),
                        mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "stub")),
                        mock.patch.dict(health._cache, {"at": 0.0, "checks": []})]
        for p in self.patches:
            p.start()
        from mangarr import db, settings
        settings._cache.clear()
        with db.connect() as con:
            self.sid = db.upsert_series(con, model.Series(
                anilist_id=1, english="Alpha Manga", romaji="Arufa", synonyms=["Alpha"], status="RELEASING",
                format="MANGA", country="JP", chapters=None, authors=["Someone", "Else"],
                description="Line one.<br><br>Line <i>two</i>."))
            self.season_id = db.upsert_series(con, model.Series(
                mangadex_id="12345678-1234-1234-1234-123456789abc", english="Season Webtoon", status="FINISHED",
                format="WEBTOON", country="KR"))
            db.upsert_series(con, model.manual("Plain Title"))
            rows = [(self.sid, 1, "have", "Chapter 1", "2024-01-01T00:00:00", None, "/lib/Alpha Manga/Chapter 001.0.cbz"),
                    (self.sid, 2, "have", "Chapter 2", "2024-01-08T00:00:00", None, None),
                    (self.sid, 2.5, "junk", "Chapter 2.5", None, "1 page(s) on X: a notice image, not a chapter", None),
                    (self.sid, 3, "wanted", "Chapter 3", "2024-01-15", None, None),
                    (self.sid, 4, "failed", "Chapter 4", None, "download failed: 404", None),
                    (self.sid, 5, "unavailable", None, None, "no trusted source lists this chapter any more", None),
                    (self.sid, 6, "ignored", None, None, None, None),
                    (self.sid, 25, "wanted", "Chapter 25", None, None, None),
                    (self.season_id, 1, "have", "S1 - Episode 1", None, None, None),
                    (self.season_id, 2, "wanted", "S2 - Episode 1", None, None, None)]
            con.executemany(
                "INSERT INTO chapter (series_id, number, status, name, uploaded, reason, library_path, source_name,"
                " updated_at) VALUES (?,?,?,?,?,?,?,'Weeb Central','2026-01-01 00:00:00')", rows)
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, author, match_level,"
                        " author_level, chapter_count, max_chapter, note, is_primary, seen_at)"
                        " VALUES (?,1,'Weeb Central','Alpha Manga','Someone',3,1,8,25,NULL,1,'2026-01-01 00:00:00')",
                        (self.sid,))
            db.event(con, "added", "added by test", self.sid)
            db.event(con, "failed", "boom", self.sid)
        from mangarr.web import app as web
        from mangarr.web import lists_routes
        self.web = web
        self.patches.append(mock.patch.object(lists_routes, "_app", web))
        self.patches[-1].start()
        self.client = TestClient(web.app)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        from mangarr import settings
        settings._cache.clear()
        self.tmp.cleanup()

    def _ok(self, path, *needles):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.status_code} {r.text[:300]}")
        for n in needles:
            self.assertIn(n, r.text, f"{path}: missing {n!r}")
        return r.text

    def test_index_both_views(self):
        html = self._ok("/", "Alpha Manga", "Season Webtoon", 'data-view="posters"', "view-table", "Continuing", "Ended")
        self.assertIn(f'action="/series/{self.sid}/monitor"', html)
        self.assertIn("2/6", html)                                   # have/listed like Sonarr's 16/16
        self._ok("/?q=alpha", "Alpha Manga")
        self.assertNotIn("Season Webtoon", self.client.get("/?q=alpha").text)

    def test_series_page(self):
        html = self._ok(f"/series/{self.sid}", "Alpha Manga", "Chapters 21-40", "Chapters 1-20", "Continuing",
                        "Line one.", "Line two.", "Why chapters failed", "download failed: 404",
                        f'action="/series/{self.sid}/chapter/3.0/ignore"', f'action="/series/{self.sid}/chapter/6.0/unignore"',
                        'data-title="Alpha Manga"', 'name="files"', 'name="exclude"', "Weeb Central",
                        "https://anilist.co/manga/1", "right to left", "Someone", "Size on disk")
        self.assertIn('id="group-block-21"', html)
        self.assertIn("Search missing", html)
        self.assertIn(f'action="/series/{self.sid}/chapter/3.0/search"', html)             # per-chapter automatic search
        self.assertIn(f'data-manual="/api/v1/series/{self.sid}/chapter/3.0/releases"', html)
        self.assertIn(f'data-download="/series/{self.sid}/chapter/3.0/download"', html)
        self.assertIn(f'action="/series/{self.sid}/chapter/6.0/search"', html)             # ignored rows get it too
        self.assertNotIn(f'action="/series/{self.sid}/chapter/1.0/search"', html)          # have rows do not
        self.assertIn("no trusted source lists this chapter any more", html)               # reason on unavailable
        self.assertIn("a notice image, not a chapter", html)                                # reason on junk
        self.assertIn("waiting for a download pass", html)                                  # wanted without a reason yet
        self.assertIn('aria-controls="group-block-21"', html)
        self.assertNotIn("<i>two</i>", html)
        self._ok(f"/series/{self.season_id}", "Season 1", "Season 2", "Ended", "left to right",
                 "https://mangadex.org/title/12345678-1234-1234-1234-123456789abc")
        self.assertEqual(self.client.get("/series/999").status_code, 404)

    def test_add_pages(self):
        self._ok("/add", 'name="ref" value="manual"', 'name="alias"')
        pick = model.Series(anilist_id=1, english="Alpha Manga", status="RELEASING", format="MANGA", country="JP")
        other = model.Series(anilist_id=2, english="Beta Manga", romaji="Beeta", status="FINISHED", chapters=12)
        with mock.patch("mangarr.web.app.metadata.lookup", lambda term: (pick, [pick, other])):
            html = self._ok("/add?term=alpha", "already tracked", "Beta Manga", "exact match",
                            'name="download" value="1"', "Beeta")
            self.assertIn('value="anilist:2"', html)
        with mock.patch("mangarr.web.app.metadata.lookup", lambda term: (None, [])):
            self._ok("/add?term=zzz", "Nothing on AniList")

        def boom(term):
            raise RuntimeError("down")
        with mock.patch("mangarr.web.app.metadata.lookup", boom):
            self._ok("/add?term=x", "lookup failed")

    def test_other_pages(self):
        self._ok("/import", "Scan staging folders", "No scan yet")
        self._ok("/lists", "Add a list", 'id="addlist"', 'name="sync_hours"', "Exclusions")
        self._ok("/wanted", "Alpha Manga", 'action="/wanted/search"')
        self._ok("/activity", 'action="/activity/refresh-all"', "Ch 3", "40%", 'data-reload="10000"')
        self._ok("/activity/history", "added by test", "boom")
        self._ok("/settings", 'id="sources"', 'id="scheduling"', 'id="komga"', 'id="notifications"', 'id="security"',
                 "Weeb Central", 'name="refresh_hours"', 'name="api_key"', 'value="test-komga"')
        self._ok("/system", 'id="tasks"', 'id="backups"', "Suwayomi", "/system/logs", 'action="/system/backups/create"')
        html = self._ok("/system/logs", 'id="log"', "careful", 'class="ln WARNING"', 'id="log-level"', 'id="log-auto"')
        self.assertIn('class="ln INFO"', html)
        self._ok("/login?next=/")                              # no login configured: redirects home (followed)

    def test_shell(self):
        html = self._ok("/wanted", 'id="health"', 'id="status"', 'id="sidebar"', "/static/app.js",
                        'href="/activity/history"', 'href="/settings#komga"', 'href="/system/logs"')
        self.assertIn('class="navsec open current" data-section="wanted"', html)
        self.assertIn('href="/wanted" class="on"', html)
        html = self.client.get(f"/series/{self.sid}").text
        self.assertIn('data-section="series"', html)
        self.assertIn('href="/" class="on"', html)
        self.assertIn('data-section="settings"', html)
        self.assertNotIn('class="navsec open current" data-section="settings"', html)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_login_page_with_auth(self):
        from mangarr import db, settings
        with db.connect() as con:
            settings.set_many(con, {"auth_user": "andy", "auth_password": "pw", "auth_method": "forms"})
        r = self.client.get("/login?next=/wanted")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="password"', r.text)
        r = self.client.post("/login", data={"username": "andy", "password": "pw", "next": "/wanted"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign out", r.text)


if __name__ == "__main__":
    unittest.main()
