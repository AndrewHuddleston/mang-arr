"""Text from scraped sites, metadata providers and users: every function
that runs a pattern over it stays fast on a 50,000-character adversarial
string, and text headed for logs is flattened to one line and capped."""
import time
import unittest
from unittest import mock

from mangarr import anilist, library, mangadex, matching, metadata
from mangarr.matching import disambiguator, match_level, oneline, strip_disambiguator
from mangarr.web import views

N = 50_000
ADVERSARIAL = [
    " " * N + "x",
    "　" * N + "x",
    "(" * N,
    "a" + "(" * N,
    "[" * N + "]",
    "x (" + " " * N,
    "(" * N + ")" + " " * N,
    "0" * N + "x",
    "1a" * (N // 2),
    "<" * N,
    " \t" * N + "x",
    "Chapter " + "0" * N + " - x",
]
BUDGET = 0.5        # seconds per call; the old patterns took 2 to 40 s on these


class LinearTimeTest(unittest.TestCase):
    def assertFast(self, fn, *args):
        for s in ADVERSARIAL:
            t0 = time.perf_counter()
            fn(s, *args)
            took = time.perf_counter() - t0
            self.assertLess(took, BUDGET, f"{fn.__name__} took {took:.2f}s on {s[:12]!r}... ({len(s)} chars)")

    def test_matching(self):
        self.assertFast(strip_disambiguator)
        self.assertFast(disambiguator)
        self.assertFast(match_level, ["x", "Wind Breaker"])
        self.assertFast(matching.norm)

    def test_library_names(self):
        self.assertFast(library.parse_number)
        self.assertFast(library.parse_season)
        self.assertFast(library.safe_title)
        self.assertFast(lambda s: library.chapter_label(1.0, s))
        self.assertFast(lambda s: library.chapter_filename(1.0, s))

    def test_plain_description(self):
        self.assertFast(views.plain_description)

    def test_lookup_query(self):
        with mock.patch.object(metadata.anilist, "search", lambda q: []), \
                mock.patch.object(metadata.mangadex, "search", lambda q: []):
            self.assertFast(metadata.lookup)

    def test_lookup_query_is_capped_before_the_providers(self):
        seen = []
        with mock.patch.object(metadata.anilist, "search", lambda q: seen.append(q) or []), \
                mock.patch.object(metadata.mangadex, "search", lambda q: []):
            metadata.lookup("x" * N)
        self.assertTrue(seen and all(len(q) <= matching.MAX_TITLE for q in seen))


class SameResultsTest(unittest.TestCase):
    """The linear rewrites give what the old regexes gave."""

    def test_disambiguator(self):
        self.assertEqual(disambiguator("Wind Breaker (NII Satoru)"), "NII Satoru")
        self.assertEqual(disambiguator("Wind Breaker [Nii]  "), "Nii")
        self.assertIsNone(disambiguator("Wind Breaker"))
        self.assertIsNone(disambiguator("Empty ()"))
        self.assertEqual(disambiguator("Foo ((x)"), "(x")

    def test_strip_disambiguator(self):
        self.assertEqual(strip_disambiguator("Perfect World (Rie Aruga)"), "Perfect World")
        self.assertEqual(strip_disambiguator("Perfect World"), "Perfect World")
        self.assertEqual(strip_disambiguator("Foo (a (b)"), "Foo")      # from the first opener, like the regex
        self.assertEqual(strip_disambiguator("A (b) c (d)"), "A (b) c")
        self.assertEqual(strip_disambiguator("(Only)"), "")

    def test_plain_description(self):
        self.assertEqual(views.plain_description("a <b> c  \t\n\n\n\nd<br>e"), "a  c\n\nd\ne")
        self.assertEqual(views.plain_description("x < y <i>z</i>"), "x < y z")
        self.assertLessEqual(len(views.plain_description("y" * 100_000)), views.MAX_DESCRIPTION)


class OnelineTest(unittest.TestCase):
    def test_newlines_cannot_forge_log_lines(self):
        forged = "x\n2026-09-27 10:00:00 ERROR   mangarr.web.app: login: admin\r\x00"
        out = oneline(forged)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x00", out)

    def test_capped(self):
        self.assertEqual(len(oneline("y" * 10_000, 200)), 200)
        self.assertEqual(oneline(None), "")
        self.assertEqual(oneline("short"), "short")


class RetryAfterTest(unittest.TestCase):
    def test_parsing_and_cap(self):
        self.assertEqual(anilist.retry_after({"Retry-After": "60"}, 10), 60)
        self.assertEqual(anilist.retry_after({}, 10), 10)
        self.assertEqual(anilist.retry_after({"Retry-After": "soon"}, 7), 7)
        self.assertEqual(anilist.retry_after({"Retry-After": "-5"}, 7), 1)
        self.assertEqual(anilist.retry_after({"Retry-After": "nan"}, 7), 7)
        with self.assertLogs("mangarr.anilist", "WARNING"), self.assertRaises(RuntimeError):
            anilist.retry_after({"Retry-After": "86400"}, 10)
        with self.assertLogs("mangarr.anilist", "WARNING"), self.assertRaises(RuntimeError):
            anilist.retry_after({"Retry-After": "1790000000"}, 10)
        with self.assertLogs("mangarr.anilist", "WARNING"), self.assertRaises(RuntimeError):
            anilist.retry_after({"Retry-After": "Fri, 31 Dec 2100 23:59:59 GMT"}, 10)

    def _http_429(self, value):
        import email.message
        import urllib.error
        h = email.message.Message()
        h["Retry-After"] = value
        return urllib.error.HTTPError("https://x", 429, "Too Many Requests", h, None)

    def test_huge_retry_after_does_not_sleep(self):
        for mod, call in ((anilist, lambda: anilist._post("q", {})), (mangadex, lambda: mangadex._get("/m", []))):
            with self.subTest(mod.__name__), \
                    mock.patch.object(mod.urllib.request, "urlopen", side_effect=self._http_429("86400")), \
                    mock.patch.object(mod.time, "sleep") as sleep, self.assertRaises(RuntimeError), \
                    self.assertLogs("mangarr.anilist", "WARNING"):
                call()
            sleep.assert_not_called()

    def test_normal_retry_after_is_honoured(self):
        with mock.patch.object(mangadex.urllib.request, "urlopen", side_effect=self._http_429("3")), \
                mock.patch.object(mangadex.time, "sleep") as sleep, self.assertRaises(RuntimeError) as cm:
            mangadex._get("/m", [])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3.0, 3.0, 3.0])
        self.assertIn("429", str(cm.exception))          # the last error is reported, not None


if __name__ == "__main__":
    unittest.main()
