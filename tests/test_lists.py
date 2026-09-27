"""Import lists with the network stubbed out: text-list parsing, the
AniList response readers, sync bookkeeping (tracked / excluded / cap /
last_result), storage and the due check."""
import os
import tempfile
import time
import unittest
from unittest import mock

from mangarr import db, lists
from mangarr.model import Series


def S(i, title, **kw):
    return Series(anilist_id=i, english=title, format=kw.pop("format", "MANGA"), **kw)


def anilist_media(i, title, **kw):
    return {"id": i, "title": {"romaji": title, "english": None, "native": None}, "synonyms": [],
            "format": kw.get("format", "MANGA"), "countryOfOrigin": "JP", "status": "RELEASING",
            "chapters": kw.get("chapters"), "popularity": 10, "coverImage": {"large": None}, "staff": {"edges": []}}


class ListsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "l.db")

    def tearDown(self):
        self.tmp.cleanup()

    # -- fetchers -----------------------------------------------------------

    def test_parse_titles(self):
        text = "﻿# my list\n\nOne Piece\n  Berserk  \n#not this\nanilist:30013\n"
        self.assertEqual(lists.parse_titles(text), ["One Piece", "Berserk", "anilist:30013"])

    def test_url_text_confident_and_review(self):
        picks = {"One Piece": S(30013, "One Piece"), "Berserk": None}
        with mock.patch.object(lists, "_get_text", lambda url, **kw: "One Piece\n\n# c\nBerserk\nanilist:30002\n"), \
                mock.patch.object(lists.metadata, "lookup",
                                  lambda t, **kw: (picks[t], [] if picks[t] else [S(1, "Berserk Z")])), \
                mock.patch.object(lists.metadata, "by_ref", lambda r: S(30002, "Berserk (by ref)")):
            series, review, skipped, quoted, unchecked = lists.fetch_url_text({"url": "http://x/list.txt"})
        self.assertEqual([s.ref for s in series], ["anilist:30013", "anilist:30002"])
        self.assertEqual(review, ["Berserk"])
        self.assertEqual((skipped, unchecked), (0, 0))
        self.assertTrue(quoted)                         # two of three lines are known series: a title list

    def test_url_text_lookup_failure_is_an_error_not_a_crash(self):
        def boom(t, **kw):
            raise RuntimeError("AniList unreachable")
        with mock.patch.object(lists, "_get_text", lambda url, **kw: "Something\n"), \
                mock.patch.object(lists.metadata, "lookup", boom), self.assertLogs("mangarr.lists", "WARNING"), \
                self.assertRaises(lists.ListFetchError) as cm:
            lists.fetch_url_text({"url": "http://x"})
        self.assertEqual(str(cm.exception), "AniList or MangaDex could not be reached, so the list's titles could not "
                                            "be checked; try again later")

    def test_user_entries_filters_status_novels_and_duplicates(self):
        data = {"data": {"MediaListCollection": {"lists": [
            {"name": "Reading", "status": "CURRENT", "entries": [
                {"media": anilist_media(1, "A")}, {"media": anilist_media(2, "Novel", format="NOVEL")}]},
            {"name": "Planning", "status": "PLANNING", "entries": [{"media": anilist_media(3, "C")}]},
            {"name": "Custom", "status": None, "entries": [{"media": anilist_media(1, "A")}]},
            {"name": "Dropped", "status": "DROPPED", "entries": [{"media": anilist_media(4, "D")}]},
        ]}}}
        got = lists.user_entries(data, ["CURRENT", "PLANNING"])
        self.assertEqual(sorted(s.anilist_id for s in got), [1, 3])

    def test_anilist_user_private_raises(self):
        with mock.patch.object(lists.anilist, "_post", lambda q, v: {}), self.assertRaises(ValueError):
            lists.fetch_anilist_user({"username": "nobody", "statuses": ["CURRENT"]})

    def test_anilist_top_pages_and_min_chapters(self):
        calls = []

        def post(q, v):
            calls.append(v)
            page = v["page"]
            media = [anilist_media(page * 100 + i, f"T{page}-{i}", chapters=(5 if i % 2 else 50)) for i in range(50)]
            return {"data": {"Page": {"pageInfo": {"hasNextPage": page < 3}, "media": media}}}
        with mock.patch.object(lists.anilist, "_post", post):
            series, review, *_ = lists.fetch_anilist_top({"sort": "SCORE_DESC", "limit": 60, "min_chapters": 10})
        self.assertEqual(len(series), 60)
        self.assertEqual(review, [])
        self.assertEqual([c["page"] for c in calls], [1, 2, 3])       # 25 usable per page -> 3 pages
        self.assertNotIn("country", calls[0])                          # omitted, never null
        with mock.patch.object(lists.anilist, "_post", post):
            lists.fetch_anilist_top({"sort": "SCORE_DESC", "limit": 10, "country": "KR"})
        self.assertEqual(calls[-1]["country"], "KR")

    # -- params -----------------------------------------------------------------

    def test_validate_params(self):
        self.assertEqual(lists.validate_params("anilist_user", {"username": " bob ", "statuses": ["current"]}),
                         {"username": "bob", "statuses": ["CURRENT"]})
        self.assertEqual(lists.validate_params("anilist_top", {"limit": "20", "country": "kr"}),
                         {"sort": "TRENDING_DESC", "limit": 20, "country": "KR", "min_chapters": 0})
        self.assertEqual(lists.validate_params("url_text", {"url": "https://x/y.txt "}), {"url": "https://x/y.txt"})
        for kind, raw in [("anilist_user", {}), ("anilist_top", {"limit": 500}), ("anilist_top", {"sort": "X"}),
                          ("url_text", {"url": "ftp://x"}), ("nope", {})]:
            with self.assertRaises(ValueError):
                lists.validate_params(kind, raw)

    # -- storage / sync ------------------------------------------------------------

    def test_crud_and_exclusions(self):
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "", "anilist_top", {"sort": "SCORE_DESC", "limit": 10, "country": "", "min_chapters": 0})
            row = lists.get_list(con, lid)
            self.assertIn("score", row["name"])
            self.assertTrue(lists.is_due(row))                        # never synced
            lists.set_enabled(con, lid, False)
            self.assertFalse(lists.is_due(lists.get_list(con, lid)))
            lists.add_exclusion(con, "anilist:5", "Five", "deleted by user")
            lists.add_exclusion(con, "anilist:5", None, None)          # keeps title/reason
            x = lists.exclusions(con)
            self.assertEqual((x[0]["ref"], x[0]["title"], x[0]["reason"]), ("anilist:5", "Five", "deleted by user"))
            with self.assertRaises(ValueError):
                lists.add_exclusion(con, "bogus")
            lists.remove_exclusion(con, "anilist:5")
            self.assertEqual(lists.excluded_refs(con), set())
            lists.delete_list(con, lid)
            self.assertIsNone(lists.get_list(con, lid))

    def test_due_after_interval(self):
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "x", "url_text", {"url": "http://x"}, sync_hours=2)
            lists.mark_synced(con, lid, "ok")
            row = lists.get_list(con, lid)
        self.assertFalse(lists.is_due(row, time.time() + 3600))
        self.assertTrue(lists.is_due(row, time.time() + 2 * 3600 + 5))

    def _sync(self, fetched, review=(), **listkw):
        submitted = []
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "test", "url_text", {"url": "http://x"}, **listkw)
            row = lists.get_list(con, lid)
            with mock.patch.dict(lists.FETCHERS, {"url_text": lambda p: (list(fetched), list(review))}):
                msg = lists.sync(con, row, lambda s, d, m: submitted.append((s.ref, d, m)))
            row = lists.get_list(con, lid)
        return msg, submitted, row

    def test_sync_skips_tracked_and_excluded(self):
        with db.connect(self.path) as con:
            db.upsert_series(con, S(1, "Tracked"))
            lists.add_exclusion(con, "anilist:2", "Gone", "deleted by user")
        fetched = [S(1, "Tracked"), S(2, "Gone"), S(3, "New")]
        msg, submitted, row = self._sync(fetched, review=["Mystery"], download=False, monitored=False)
        self.assertEqual(submitted, [("anilist:3", False, False)])
        self.assertEqual(msg, "3 fetched, 1 added, 1 review, 1 already tracked, 1 excluded; needs review: Mystery")
        self.assertEqual(row["last_result"], msg)
        self.assertIsNotNone(row["last_sync"])

    def test_sync_caps_adds(self):
        fetched = [S(i, f"T{i}") for i in range(1, 41)]
        msg, submitted, row = self._sync(fetched)
        self.assertEqual(len(submitted), lists.MAX_ADDS)
        self.assertEqual(msg, "40 fetched, 25 added, 15 deferred (cap 25 per sync; next sync continues)")

    def test_sync_fetch_error_recorded(self):
        def boom(p):
            raise RuntimeError("AniList unreachable: 503")
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "bad", "url_text", {"url": "http://x"})
            with mock.patch.dict(lists.FETCHERS, {"url_text": boom}), self.assertLogs("mangarr.lists", "ERROR") as cm:
                msg = lists.sync(con, lists.get_list(con, lid), lambda *a: self.fail("nothing should be added"))
            row = lists.get_list(con, lid)
        self.assertEqual(msg, "error: RuntimeError: AniList unreachable: 503")
        self.assertEqual(row["last_result"], msg)
        self.assertIn("list bad:", cm.output[0])

    def test_migration_number(self):
        with db.connect(self.path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("import_list", tables)


if __name__ == "__main__":
    unittest.main()
