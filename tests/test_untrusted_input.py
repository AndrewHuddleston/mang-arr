"""Input that anyone can make long: aliases typed on the Add page or sent to
the API (and synonyms from a provider) are capped wherever a series is made,
the title helpers stay linear, a plan cannot grow past what any real series
has, and one series page or API call never renders every chapter a source
lists."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mangarr import anilist, db, model, resolver
from mangarr.model import Series
from mangarr.resolver import SourceMatch, resolve
from mangarr.suwayomi import Chapter, Source
from mangarr.web import views

try:                                     # run by discover (-s tests) or as tests.test_untrusted_input
    from test_web_security import WebBase
except ImportError:
    from tests.test_web_security import WebBase


def chapters(numbers, named=False):
    return [Chapter(i + 1, float(n), f"Chapter {n:g}" if named else None, None, False) for i, n in enumerate(numbers)]


# -- aliases ---------------------------------------------------------------------

class AliasCapTest(unittest.TestCase):
    def test_cap_aliases(self):
        aliases = ["  Alpha ", "alpha", "", "   ", "x" * 301, "y" * 300, 7, *[f"A{i}" for i in range(100)]]
        with self.assertLogs("mangarr.model", "DEBUG") as cm:
            got = model.cap_aliases(aliases, "T")
        self.assertEqual(len(got), model.MAX_ALIASES)
        self.assertEqual(got[:3], ["Alpha", "y" * 300, "A0"])             # order kept, repeats (any case) once
        self.assertIn("dropped 1 longer than 300 characters and 52 over the cap of 50", cm.output[0])

    def test_every_series_is_capped(self):
        many = [f"Alias {i}" for i in range(5000)]
        self.assertEqual(len(Series(anilist_id=1, english="T", synonyms=many).synonyms), model.MAX_ALIASES)
        self.assertEqual(len(model.manual("T", *many).synonyms), model.MAX_ALIASES)
        media = {"id": 1, "title": {"romaji": "T"}, "synonyms": many}               # provider records, import lists
        self.assertEqual(len(anilist._to_series(media).synonyms), model.MAX_ALIASES)
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "t.db")) as con:
            sid = db.upsert_series(con, Series(anilist_id=1, english="T"))
            con.execute("UPDATE series SET synonyms=? WHERE id=?", (json.dumps(many), sid))  # stored before the cap
            row = db.get_series(con, sid)
            self.assertEqual(len(db.series_to_model(row).synonyms), model.MAX_ALIASES)

    def test_title_helpers_are_linear(self):
        # search_titles was quadratic (list membership): 50k titles took ~110 s
        s = Series(anilist_id=1, english="T", native="テ")
        s.synonyms = [f"Alias {i}" for i in range(50_000)] + [f"別名{i}" for i in range(50_000)]   # past the cap on purpose
        t0 = time.perf_counter()
        titles = s.search_titles
        capped = resolver.capped_search_titles(s)
        self.assertLess(time.perf_counter() - t0, 2)
        self.assertEqual(len(titles), 100_002)
        self.assertEqual(titles[1:3], ["Alias 0", "Alias 1"])
        self.assertEqual(titles[-1], "別名49999")
        self.assertEqual(len(capped), resolver.MAX_SEARCH_TITLES)
        self.assertEqual(capped[-1], "テ")
        with mock.patch.object(Series, "search_titles", new_callable=mock.PropertyMock,
                               return_value=titles) as prop:
            resolver.capped_search_titles(s)
        self.assertEqual(prop.call_count, 1)                   # not again for the log line


class AliasWebTest(WebBase):
    def _queued(self):
        got = []
        return got, mock.patch.object(self.web, "_queue_add", lambda series, *a, **k: got.append(series) or "stub")

    def test_add_form_caps_aliases(self):
        # the review repro: a form field of 100,000 aliases, under the 1 MB body limit
        got, patch = self._queued()
        with patch:
            r = self.client.post("/add", data={"ref": "manual", "title": "zzz", "alias": "|".join(
                str(i) for i in range(100_000))}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(got[0].synonyms), model.MAX_ALIASES)

    def test_api_caps_aliases(self):
        got, patch = self._queued()
        with patch:
            r = self.client.post("/api/v1/series", headers={"X-Api-Key": self.key}, json={
                "ref": "manual", "title": "zzz", "aliases": [f"b{i}" for i in range(20_000)] + ["c" * 5000]})
        self.assertEqual(r.status_code, 409, r.text[:200])                 # the stubbed queue answers "stub"
        self.assertEqual(len(got[0].synonyms), model.MAX_ALIASES)
        self.assertTrue(all(len(a) <= model.MAX_ALIAS_LEN for a in got[0].synonyms))


# -- the plan ---------------------------------------------------------------------

class ByIdClient:
    """Each source lists its own chapter numbers."""
    def __init__(self, per_source: dict):
        self.per_source = per_source

    def search(self, src, q):
        return [{"id": int(src.id), "title": "Title", "author": None}]

    def manga(self, manga_id):
        return {"title": "Title", "author": None}, chapters(self.per_source[str(manga_id)])

    def page_count(self, chapter_id):
        return 20


class PlanCapTest(unittest.TestCase):
    def setUp(self):
        resolver._unreachable.clear()
        p = mock.patch("mangarr.settings.get", lambda k: 3)
        p.start()
        self.addCleanup(p.stop)

    def test_sources_adding_up_past_any_real_series(self):
        # the review repro: two sources of 10,000 chapters each (1-10000 and
        # 10001-20000) on an ongoing series - each under the per-source cap,
        # and no length to judge them by
        a, b = Source("1", "A", "en"), Source("2", "B", "en")
        client = ByIdClient({"1": range(1, 10_001), "2": range(10_001, 20_001)})
        series = Series(anilist_id=1, english="Title", status="RELEASING")
        with self.assertLogs("mangarr.resolver", "WARNING") as cm:
            plan = resolve(client, series, sources=[b, a])
        self.assertEqual(len(plan.chapters), 10_000)
        self.assertEqual(plan.chapters[0], 1.0)                  # the source that reaches least far is kept
        note = next(m for m in plan.matches if m.source is b).note
        self.assertIn("would make the plan 20000 chapters", note)
        self.assertTrue(any("B: would make the plan" in line for line in cm.output))

    def test_real_sources_are_untouched(self):
        a, b, c = Source("1", "A", "en"), Source("2", "B", "en"), Source("3", "C", "en")
        client = ByIdClient({"1": range(1, 3801), "2": [*range(1, 3790), 12.5], "3": range(1, 3805)})
        plan = resolve(client, Series(anilist_id=1, english="Title", status="RELEASING"), sources=[a, b, c])
        self.assertEqual([m.note for m in plan.matches], ["", "", ""])
        self.assertEqual(len(plan.chapters), 3805)

    def test_saving_a_long_plan_is_linear(self):
        src = Source("1", "Src", "en")
        m = SourceMatch(src, 1, "T", None, 0, "T", 1, chapters(range(1, 10_001), named=True))
        plan = resolver.Plan(Series(english="T"), [m], [], [], {c.number: m for c in m.chapters},
                             candidates={c.number: [m] for c in m.chapters})
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "t.db")) as con:
            sid = db.upsert_series(con, Series(anilist_id=1, english="T"))
            t0 = time.perf_counter()
            db.save_plan(con, sid, plan, 1)
            took = time.perf_counter() - t0
            self.assertEqual(con.execute("SELECT name FROM chapter WHERE series_id=? AND number=9999",
                                         (sid,)).fetchone()[0], "Chapter 9999")
        self.assertLess(took, 2.5)                               # was about 5.5 s: one scan of the list per chapter


# -- the series page and API --------------------------------------------------------

def rows(numbers, status="wanted"):
    return [{"number": float(n), "status": status, "name": None} for n in numbers]


class PageGroupsTest(unittest.TestCase):
    def test_every_row_once_within_the_bounds(self):
        groups = views.group_chapters(rows(range(1, 101)))
        seen, pages = [], None
        for p in range(1, 5):
            out, info = views.page_groups(groups, p, rows=30, max_groups=100)
            pages = info["pages"]
            self.assertLessEqual(sum(len(g["chapters"]) for g in out), 30)
            self.assertEqual(info["last"] - info["first"] + 1, sum(len(g["chapters"]) for g in out))
            self.assertTrue(out[0]["open"])                       # each page opens its first group
            seen += [c["number"] for g in out for c in g["chapters"]]
        self.assertEqual(pages, 4)
        self.assertEqual(sorted(seen), [float(n) for n in range(1, 101)])
        out, info = views.page_groups(groups, 1, rows=30, max_groups=100)
        self.assertEqual([(g["key"], len(g["chapters"]), g["part"], g["size"]) for g in out],
                         [("block-81", 20, False, 20), ("block-61", 10, True, 20)])
        self.assertEqual(out[1]["total"], 20)                     # the header counts the whole group

    def test_group_count_is_bounded_and_pages_clamp(self):
        groups = views.group_chapters(rows(range(1, 20 * 500, 20)))     # one chapter per block: 500 groups
        out, info = views.page_groups(groups, 1)
        self.assertEqual((len(out), info["pages"]), (views.PAGE_GROUPS, 3))
        out, info = views.page_groups(groups, 99)
        self.assertEqual((info["page"], len(out), info["last"]), (3, 100, 500))
        self.assertEqual(views.page_groups([], 5), ([], {"page": 1, "pages": 1, "total": 0, "first": 0, "last": 0}))

    def test_short_ranges(self):
        self.assertEqual(views.short_ranges([1, 2, 3, 5]), "1-3, 5")
        self.assertEqual(views.short_ranges(range(1, 200, 2), 3), "1, 3, 5, ... (97 more)")


class SeriesPagingWebTest(WebBase):
    def setUp(self):
        super().setUp()
        with db.connect() as con:
            self.sid = db.upsert_series(con, Series(anilist_id=1, english="Huge", status="RELEASING"))
            con.executemany("INSERT INTO chapter (series_id, number, status, name, reason, source_name, updated_at)"
                            " VALUES (?,?,'wanted',?,'waiting','Src','2026-01-01 00:00:00')",
                            [(self.sid, float(n), f"Chapter {n}") for n in range(1, 4101)])

    def test_series_page_renders_one_page_of_rows(self):
        r = self.client.get(f"/series/{self.sid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count('<tr class="episode-row'), views.PAGE_CHAPTERS)
        self.assertLess(len(r.content), 5_000_000)
        self.assertIn("4100 chapters are listed, more than one page shows: rows 1-2000, newest first "
                      "(page 1 of 3)", r.text)
        self.assertIn('href="?page=2">Older</a>', r.text)
        self.assertIn('data-number="4100"', r.text)
        self.assertIn("4100 missing", r.text)                       # the counts cover every chapter
        last = self.client.get(f"/series/{self.sid}?page=3")
        self.assertEqual(last.text.count('<tr class="episode-row'), 100)
        self.assertIn('data-number="1"', last.text)
        self.assertNotIn('data-number="4100"', last.text)
        self.assertIn('href="?page=2">Newer</a>', last.text)

    def test_short_series_has_no_paging(self):
        with db.connect() as con:
            con.execute("DELETE FROM chapter WHERE number > 30")
        r = self.client.get(f"/series/{self.sid}")
        self.assertEqual(r.text.count('<tr class="episode-row'), 30)
        self.assertNotIn("chapter-paging", r.text)

    def test_api_pages_chapters(self):
        h = {"X-Api-Key": self.key}
        d = self.client.get(f"/api/v1/series/{self.sid}", headers=h).json()
        self.assertEqual((len(d["chapters"]), d["chapterTotal"], d["limit"], d["offset"]), (4100, 4100, 5000, 0))
        d = self.client.get(f"/api/v1/series/{self.sid}?limit=10&offset=5", headers=h).json()
        self.assertEqual([c["number"] for c in d["chapters"]], [float(n) for n in range(6, 16)])
        self.assertEqual(d["series"]["title"], "Huge")
        d = self.client.get(f"/api/v1/series/{self.sid}?limit=999999&offset=-3", headers=h).json()
        self.assertEqual((d["limit"], d["offset"], len(d["chapters"])), (5000, 0, 4100))
        d = self.client.get(f"/api/v1/series/{self.sid}?limit=0&offset=4099", headers=h).json()
        self.assertEqual((d["limit"], [c["number"] for c in d["chapters"]]), (1, [4100.0]))



# -- import list URLs ---------------------------------------------------------------

class ImportListWebTest(WebBase):
    def test_internal_urls_are_refused_when_added(self):
        from mangarr import lists
        h = {"X-Api-Key": self.key}
        for url, why in (("http://169.254.169.254/latest/meta-data/", "link-local"),
                         ("http://127.0.0.1:6789/api/v1/settings", "loopback"), ("http://localhost:4567/", "this machine")):
            r = self.client.post("/api/v1/importlist", headers=h, json={"kind": "url_text", "params": {"url": url}})
            self.assertEqual(r.status_code, 400, url)
            self.assertIn(why, r.json()["detail"])
            r = self.client.post("/lists/add", data={"kind": "url_text", "url": url, "sync_now": "1"},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)
        with db.connect() as con:
            self.assertEqual(lists.all_lists(con), [])
        r = self.client.post("/api/v1/importlist", headers=h,
                             json={"kind": "url_text", "params": {"url": "http://192.168.1.10/manga.txt"}})
        self.assertEqual(r.status_code, 200)                                     # the LAN is fine


if __name__ == "__main__":
    unittest.main()
