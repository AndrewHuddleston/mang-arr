"""Input that anyone can make long: aliases typed on the Add page or sent to
the API (and synonyms from a provider) are capped wherever a series is made,
and the title helpers stay linear."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mangarr import anilist, db, model, resolver
from mangarr.model import Series

try:                                     # run by discover (-s tests) or as tests.test_untrusted_input
    from test_web_security import WebBase
except ImportError:
    from tests.test_web_security import WebBase


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



if __name__ == "__main__":
    unittest.main()
