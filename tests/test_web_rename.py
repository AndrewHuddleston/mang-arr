"""Settings -> Media Management and renaming existing files from the web UI
and the API: the settings sub-pages and Show Advanced (hidden fields are
still posted, so hiding never resets a value), the live naming preview, the
rename preview, the rename with its reading-progress confirmation, and the
undo. Everything happens in a temporary folder; Komga is faked."""
import os
import sys
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = None

from test_rename_apply import NO_DECIMALS, ApplyBase  # noqa: E402

from mangarr import db, health, jobs, renamer, settings  # noqa: E402

PLAIN = [(1.0, None, None), (2.0, None, None), (2.5, "Chapter 2.5: Extra: Side", "Chapter 2.5: Extra: Side")]


def _flash(r) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["m"][0]


@unittest.skipIf(TestClient is None, "web extras not installed")
class WebBase(ApplyBase):
    def setUp(self):
        super().setUp()
        from mangarr.web import app as web
        self.web = web
        self.ran = []

        def submit(kind, title, fn, series_id=None, key=None):
            job = jobs.Job(len(self.ran) + 1, kind, title, series_id)
            job.message = fn(job)                   # at once: the tests look at the result
            self.ran.append(job)
            return job
        for p in (mock.patch.object(web.runner, "submit", submit),
                  mock.patch("mangarr.web.app.client.sources", lambda *a, **k: []),
                  mock.patch("mangarr.web.app.client.gq", lambda q, **kw: {"settings": {"maxSourcesInParallel": 3}}),
                  mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "stub")),
                  mock.patch.dict(health._cache, {"at": 0.0, "checks": []}),
                  mock.patch("mangarr.notify.send", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.sid = self.add(PLAIN)

    def files(self) -> list:
        return sorted(os.listdir(self.folder(self.sid)))

    def use(self, **values):
        with db.connect() as con:
            settings.set_many(con, values)


class SettingsPagesTest(WebBase):
    def test_every_setting_is_on_exactly_one_page(self):
        seen = {}
        for slug in self.web.SETTINGS_PAGES:
            html = self.client.get(f"/settings/{slug}").text
            for key in settings.DEFAULTS:
                if f'name="{key}"' in html:
                    seen.setdefault(key, []).append(slug)
        self.assertEqual({k: v for k, v in seen.items() if len(v) != 1}, {})
        never = settings.INTERNAL_KEYS | {"api_key", "unusable_sources", "throttled_sources", "page_warm_sources"}
        self.assertEqual(sorted(set(settings.DEFAULTS) - set(seen) - never), [])

    def test_advanced_fields_are_in_the_form_and_a_save_keeps_them(self):
        self.use(min_pages=5, chapter_title_max_chars=60)
        html = self.client.get("/settings/downloading").text
        self.assertRegex(html, r'<div class="form-group advanced">\s*<label class="form-label" for="min_pages">')
        self.assertIn('name="min_pages" value="5"', html)             # hidden by CSS only: still posted
        self.assertIn("Default: 8", html)
        self.assertIn("1 advanced setting is changed from its default", html)
        self.assertIn("1 advanced setting is changed", self.client.get("/settings/media-management").text)
        self.assertNotIn("advanced-hint", self.client.get("/settings/komga").text)
        # a page posts only its own fields: the others keep their values
        r = self.client.post("/settings", data={"page": "downloading", "refresh_hours": "3", "min_pages": "5"},
                             follow_redirects=False)
        self.assertEqual((r.status_code, _flash(r)), (303, "saved"))
        self.assertTrue(r.headers["location"].startswith("/settings/downloading?"))
        v = settings.all_values()
        self.assertEqual((v["refresh_hours"], v["min_pages"], v["chapter_title_max_chars"]), (3.0, 5, 60))

    def test_a_format_that_cannot_be_used_is_refused_and_the_page_comes_back_on_it(self):
        for key, value in (("chapter_file_format", "Chapter {Nope}"),
                           ("chapter_file_format", "{Series Title}"),
                           ("series_folder_format", "{Series Title}/x"),
                           ("chapter_title_max_chars", "0")):
            with self.subTest(key=key, value=value):
                r = self.client.post("/settings", data={"page": "media-management", key: value},
                                     follow_redirects=False)
                self.assertTrue(_flash(r).startswith("invalid value, nothing saved"), _flash(r))
                self.assertEqual(settings.all_values()[key], settings.DEFAULTS[key])
                self.assertTrue(r.headers["location"].startswith("/settings/media-management?"))
        r = self.client.post("/settings", data={"page": "media-management", "chapter_title_max_chars": "many"},
                             follow_redirects=False)
        self.assertIn("focus=chapter_title_max_chars", r.headers["location"])
        html = self.client.get(r.headers["location"]).text
        self.assertIn('data-force-advanced="1"', html)
        self.assertIn('data-focus="chapter_title_max_chars"', html)

    def test_changing_a_format_renames_nothing(self):
        before = self.snapshot()
        r = self.client.post("/settings", data={"page": "media-management", **NO_DECIMALS}, follow_redirects=False)
        self.assertIn("keep their names until you rename them", _flash(r))
        self.assertEqual(settings.get("chapter_file_format"), NO_DECIMALS["chapter_file_format"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ran, [])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM rename_run").fetchone()[0], 0)

    def test_the_live_preview(self):
        for path in ("/settings/naming/examples", "/api/v1/config/naming/examples"):
            d = self.client.get(path).json()
            self.assertEqual(d["folder"], os.path.basename(self.folder(self.sid)))
            self.assertIn("Chapter 001.0.cbz", d["chapters"])
            self.assertIn("Chapter 002.5 - Extra_ Side.cbz", d["chapters"])
            self.assertEqual((d["errors"], d["warnings"]), ([], []))
        d = self.client.get("/settings/naming/examples", params={
            "chapter_file_format": "Ch {Chapter:00}{ - Chapter Title}", "colon_replacement": "smart",
            "series_folder_format": "{Series Romaji}{ (Year)}"}).json()
        self.assertEqual(d["folder"], "Raise wa Tanin ga Ii (2017)")
        self.assertIn("Ch 02.5 - Extra - Side.cbz", d["chapters"])
        self.assertEqual(len(d["warnings"]), 1)                     # 02.5 sorts before 02
        d = self.client.get("/settings/naming/examples", params={"chapter_file_format": "{Nope}"}).json()
        self.assertEqual((d["folder"], d["chapters"]), (None, []))
        self.assertTrue(d["errors"])
        d = self.client.get("/settings/naming/examples", params={"chapter_title_max_chars": "x"}).json()
        self.assertTrue(d["errors"])
        self.assertEqual(settings.all_values()["chapter_file_format"], settings.DEFAULTS["chapter_file_format"])


class RenameWebTest(WebBase):
    def test_the_preview_renames_nothing(self):
        self.use(**NO_DECIMALS)
        before = self.snapshot()
        d = self.client.get("/api/v1/rename", params={"seriesId": self.sid}).json()
        self.assertEqual((d["renames"], d["files"]), (2, 3))
        self.assertEqual([(c["oldName"], c["newName"]) for c in d["chapters"]],
                         [("Chapter 001.0.cbz", "Chapter 001.cbz"), ("Chapter 002.0.cbz", "Chapter 002.cbz")])
        self.assertEqual((d["komga"]["state"], d["komga"]["needs_confirmation"]), ("not_configured", True))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.get("/api/v1/rename", params={"seriesId": 999}).status_code, 404)
        html = self.client.get(f"/series/{self.sid}").text
        for needle in ('id="rename-open"', 'id="rename-modal"', f'action="/series/{self.sid}/rename"',
                       'id="rename-submit" disabled', "reading progress in your reader app may be lost"):
            self.assertIn(needle, html)

    def test_without_komga_the_rename_needs_the_tick(self):
        self.use(**NO_DECIMALS)
        before = self.snapshot()
        r = self.client.post(f"/series/{self.sid}/rename", data={"latest_titles": "0"}, follow_redirects=False)
        self.assertIn("reading progress in your reader app may be lost", _flash(r))
        self.assertEqual((self.snapshot(), self.ran), (before, []))
        r = self.client.post(f"/api/v1/series/{self.sid}/rename", json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["needsConfirmation"][0]["state"], "not_configured")
        self.assertEqual(self.snapshot(), before)
        r = self.client.post(f"/series/{self.sid}/rename", data={"confirmed": "1"}, follow_redirects=False)
        self.assertIn("rename queued", _flash(r))
        self.assertEqual(self.files(), ["Chapter 001.cbz", "Chapter 002.5 - Extra_ Side.cbz", "Chapter 002.cbz"])
        self.assertEqual(self.ran[0].message, "2 file(s) and folder(s) renamed in 1 series")
        self.assertEqual(sorted(self.snapshot()["staging"].items()), sorted(before["staging"].items()))

    def test_with_komga_hashing_no_tick_is_needed(self):
        self.use(**NO_DECIMALS)
        self.komga_on()
        self.komga.add_series("S1", os.path.basename(self.folder(self.sid)), self.files())
        d = self.client.get("/api/v1/rename", params={"seriesId": self.sid}).json()
        self.assertEqual(d["komga"]["needs_confirmation"], False)
        r = self.client.post(f"/api/v1/series/{self.sid}/rename", json={"only": [2]})
        self.assertEqual((r.status_code, r.json()["queued"], r.json()["renames"]), (200, True, 1))
        self.assertEqual(self.files(), ["Chapter 001.0.cbz", "Chapter 002.5 - Extra_ Side.cbz", "Chapter 002.cbz"])
        self.assertEqual(self.scans, 1)

    def test_nothing_to_rename(self):
        r = self.client.post(f"/series/{self.sid}/rename", data={"confirmed": "1"}, follow_redirects=False)
        self.assertTrue(_flash(r).startswith("nothing to rename"))
        self.assertEqual(self.client.post(f"/api/v1/series/{self.sid}/rename", json={}).json()["queued"], False)
        self.assertIn("Nothing to rename", self.client.get("/rename").text)
        self.assertEqual(self.ran, [])

    def test_several_series_and_the_undo(self):
        other = self.add([(1.0, None, None)], anilist_id=2, english="Berserk", romaji="Berserk")
        self.use(**NO_DECIMALS)
        before = self.snapshot()
        html = self.client.get("/rename").text
        for needle in ("Berserk", f'name="series" value="{self.sid}"', f'name="series" value="{other}"',
                       "Chapter 001.0.cbz", "Chapter 001.cbz", 'name="confirmed"'):
            self.assertIn(needle, html)
        r = self.client.post("/rename", data={"series": [str(self.sid), str(other)]}, follow_redirects=False)
        self.assertIn("reading progress in your reader app may be lost", _flash(r))
        self.assertEqual(self.snapshot(), before)
        r = self.client.post("/rename", data={"series": [str(self.sid), str(other)], "confirmed": "1"},
                             follow_redirects=False)
        self.assertIn("3 file(s) in 2 series", _flash(r))
        self.assertEqual(self.files(), ["Chapter 001.cbz", "Chapter 002.5 - Extra_ Side.cbz", "Chapter 002.cbz"])
        self.assertEqual(os.listdir(self.folder(other)), ["Chapter 001.cbz"])
        runs = self.client.get("/api/v1/rename/history").json()
        self.assertEqual([(x["renamed"], x["can_undo"]) for x in runs], [(3, True)])
        for path in ("/rename", "/activity/history"):
            self.assertIn(f'action="/rename/{runs[0]["id"]}/undo"', self.client.get(path).text)
        # the undo asks for the same confirmation, then gives every name back
        r = self.client.post(f"/rename/{runs[0]['id']}/undo", data={"back": "/activity/history"},
                             follow_redirects=False)
        self.assertIn("not undone", _flash(r))
        self.assertIn(f"confirm={runs[0]['id']}", r.headers["location"])
        self.assertIn('name="confirmed"', self.client.get(r.headers["location"]).text)
        self.assertEqual(os.listdir(self.folder(other)), ["Chapter 001.cbz"])
        r = self.client.post(f"/api/v1/rename/{runs[0]['id']}/undo", json={"confirmed": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.post(f"/api/v1/rename/{runs[0]['id']}/undo", json={"confirmed": True}).status_code,
                         409)
        self.assertEqual(self.client.post("/api/v1/rename/999/undo", json={}).status_code, 404)

    def test_the_stored_formats_are_the_only_ones_a_rename_uses(self):
        """No format, path or name comes from the request."""
        r = self.client.post(f"/series/{self.sid}/rename",
                             data={"confirmed": "1", "chapter_file_format": "../../x {Chapter}", "only": "1,abc"},
                             follow_redirects=False)
        self.assertIn("could not be read", _flash(r))
        r = self.client.post(f"/series/{self.sid}/rename",
                             data={"confirmed": "1", "chapter_file_format": "../../x {Chapter}"},
                             follow_redirects=False)
        self.assertTrue(_flash(r).startswith("nothing to rename"))
        self.assertEqual(self.files(), ["Chapter 001.0.cbz", "Chapter 002.0.cbz", "Chapter 002.5 - Extra_ Side.cbz"])
        self.assertEqual(renamer.PROGRESS_LOST, "reading progress in your reader app may be lost")


if __name__ == "__main__":
    unittest.main()
